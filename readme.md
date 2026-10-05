# Real-Time Distributed Leaderboard Engine

A high-throughput, low-latency leaderboard API built with **Django REST Framework**, **Redis 5.0+**, and **Django Channels (ASGI/Daphne)**.

Designed for high-concurrency multiplayer gaming services, this system processes scores in real time with sub-millisecond in-memory calculations, enforces mathematically sound **Standard Competition Ranking (1224 ties)**, computes dynamic percentiles, streams live updates over WebSockets, filters isolated social/clan subsets, and prevents cheating via **HMAC-SHA256 cryptographic request signing** and **atomic anti-replay protection**.

---

## Table of Contents

1. [System Architecture](https://www.google.com/search?q=%231-system-architecture)
2. [Algorithmic Core & Mathematics](https://www.google.com/search?q=%232-algorithmic-core--mathematics)
3. [Redis Key Topology & Memory Lifecycle](https://www.google.com/search?q=%233-redis-key-topology--memory-lifecycle)
4. [Anti-Cheat & Security Pipeline](https://www.google.com/search?q=%234-anti-cheat--security-pipeline)
5. [API Specification](https://www.google.com/search?q=%235-api-specification)
* [REST Endpoints](https://www.google.com/search?q=%23rest-endpoints)
* [WebSocket Interface](https://www.google.com/search?q=%23websocket-interface)


6. [Load Testing & Benchmarks (Locust)](https://www.google.com/search?q=%236-load-testing--benchmarks-locust)
7. [Local Setup & Installation](https://www.google.com/search?q=%237-local-setup--installation)
8. [Automated Test Suite](https://www.google.com/search?q=%238-automated-test-suite)
9. [Production Deployment Considerations](https://www.google.com/search?q=%239-production-deployment-considerations)

---

## 1. System Architecture

The architecture decouples the **high-frequency hot path** (in-memory ranking and live event fan-out) from the **durable cold storage path** (database auditing and offline analytics).

```
                                +---------------------------------------------+
                                |  Game Client / Dedicated Game Server (DGS)  |
                                +---------------------------------------------+
                                      |                              ^
                1. Signed Score POST  |                              | 4. Live WebSocket
             (X-Signature, Nonce, TS) |                              |    Top-10 Broadcast
                                      v                              |
                    +------------------------------------+           |
                    |  Daphne ASGI Layer / Django DRF    |           |
                    +------------------------------------+           |
                       |              |                |             |
         Rate Limit &  |    Audit Log |      Real-Time |             |
         Anti-Replay   |    Insert    |      ZSET Pipe |             |
                       v              v                v             |
             +-------------+  +---------------+  +-----------------+ |
             | LocMemCache |  |  PostgreSQL / |  |  Redis 5.0+     |-+
             | Nonce Store |  |  SQLite Audit |  |  - ZADD / ZCOUNT|
             |  (TTL: 60s) |  |  (Cold Store) |  |  - Pub/Sub Room |
             +-------------+  +---------------+  +-----------------+

```

### The Ingestion Hot Path

1. **Security & Throttling**: The request hits `PlayerScoreThrottle` (limiting clients to 10 submissions/min per player) and `HasValidHMACSignature` (validating clock drift, atomic Redis nonces, and cryptographic integrity).
2. **Atomic In-Memory Ranking**: `LeaderboardRedisService` inspects the player's personal best and conditionally executes `ZADD` across `daily`, `weekly`, and `all_time` sorted sets inside an atomic pipeline ($O(\log N)$ write).
3. **Audit Persistence**: Score submissions are recorded in PostgreSQL/SQLite for cold archival and auditing.
4. **WebSocket Fan-Out**: Updated standings and refreshed Top-10 rosters are broadcast through the ASGI channel layer to active WebSocket subscribers in $< 10\text{ ms}$.

---

## 2. Algorithmic Core & Mathematics

### Standard Competition Ranking ("1224" Ties)

Standard Redis `ZREVRANK` assigns arbitrary sequential indices to identical scores, which breaks leaderboard fairness. This engine computes mathematically correct **Standard Competition Ranking** (where tied scores share the same rank and subsequent ranks skip):

$$\text{Rank}(S) = 1 + \text{ZCOUNT}(\text{key}, (S, +\infty)$$

*Example Distribution:*

| Player | Score | Standard Competition Rank | Standard Dense Rank (Flawed) |
| --- | --- | --- | --- |
| Alice | 1000 | **1** | 1 |
| Bob | 800 | **2** | 2 |
| Charlie | 800 | **2** (Tied) | 2 |
| Dave | 500 | **4** (Skipped 3) | 3 |

### Real-Time Percentiles

Exact player percentiles are evaluated dynamically using cardinality bounds:

$$\text{Percentile}(S) = \left( \frac{\text{ZCOUNT}(\text{key}, -\infty, S)}{\text{ZCARD}(\text{key})} \right) \times 100$$

This represents the percentage of total participants whose score is less than or equal to the player's personal best.

### Pipelined Subset Extraction (Social & Clan Boards)

Rather than executing expensive set intersections (`ZINTERSTORE`) that allocate transient keys in Redis memory, the engine uses **Pipelined Subset Extraction**:

* Queries arbitrary player subsets (friends or guild members) using pipelined `ZSCORE` in $O(M)$ time.
* Calculates local relative ranks (handling 1224 ties) and group percentiles in application memory.
* Resolves global positions via pipelined `ZCOUNT` in the same operation.

---

## 3. Redis Key Topology & Memory Lifecycle

All leaderboard keys are partitioned by `game_id` and time windows to avoid key contention and enable deterministic memory eviction.

| Key Pattern | Data Structure | TTL | Purpose |
| --- | --- | --- | --- |
| `lb::daily:` | `ZSET` | 7 Days (604,800s) | Rolling daily competition window. |
| `lb::weekly:` | `ZSET` | 30 Days (2,592,000s) | ISO week competition window. |
| `lb::all_time` | `ZSET` | Permanent | Global historical standings. |
| `lb_snap:` | `ZSET` | 300 Seconds (5m) | Pinned snapshot set for drift-proof pagination. |
| `nonce::` | `STRING` | 60 Seconds | Atomic anti-replay defense token. |

### Automated Archival Pipeline

To prevent memory exhaustion from expired time windows, the automated archival command dumps finished windows into cold SQL storage:

```bash
python manage.py archive_leaderboards --days-old 7

```

1. Identifies expired keys matching `lb:*:daily:`.
2. Reads rankings and serializes them into the SQL `ArchivedLeaderboardRecord` table.
3. Evicts the `ZSET` from Redis memory via `UNLINK`.

---

## 4. Anti-Cheat & Security Pipeline

Every score submission requires cryptographic proof of authenticity to prevent spoofing, automated spam, and replay attacks.

```
Incoming Request
       │
       ├──▶ 1. Scoped Rate Limiter: Max 10 requests / minute per player ID (HTTP 429)
       │
       ├──▶ 2. Replay Window Check: |server_time - X-Timestamp| <= 30 seconds (HTTP 403)
       │
       ├──▶ 3. Atomic Replay Defense: Redis SET nonce:: 1 NX EX 60 (HTTP 403)
       │
       └──▶ 4. HMAC-SHA256 Verification: Constant-time digest comparison (HTTP 403)

```

### Canonical Signing Construction

The client and server construct identical canonical strings:

```
message = f"{game_id}:{player_id}:{score:.2f}:{timestamp}:{nonce}"

```

```python
import hmac
import hashlib

signature = hmac.new(
    api_secret.encode('utf-8'),
    message.encode('utf-8'),
    hashlib.sha256
).hexdigest()

```

---

## 5. API Specification

### REST Endpoints

#### 1. Ingest Signed Score

* **`POST /api/leaderboards/scores/`**
* **Headers**:
* `X-Signature`: HMAC-SHA256 hex digest.
* `X-Timestamp`: Current Unix timestamp (`int`).
* `X-Nonce`: 32-character random UUID string.
* `Content-Type: application/json`


* **Request Body:**

```json
{
  "game": "325add08-5073-43ce-b7fe-35563c82e2de",
  "player": "soldier_76",
  "score": 1850.0
}

```

* **Response (`201 Created`):**

```json
{
  "id": 42,
  "game": "325add08-5073-43ce-b7fe-35563c82e2de",
  "player": "soldier_76",
  "score": 1850.0,
  "created_at": "2026-10-05T10:45:00.123456Z",
  "current_standings": {
    "daily": { "rank": 1, "percentile": 100.0 },
    "weekly": { "rank": 1, "percentile": 100.0 },
    "all_time": { "rank": 4, "percentile": 98.25 }
  }
}

```

---

#### 2. Get Top-N Standings

* **`GET /api/leaderboards//top/?limit=10&period=all_time`**
* **Query Parameters**:
* `limit` (*int, optional*): Default `10`, maximum `100`.
* `period` (*string, optional*): `daily` | `weekly` | `all_time` (Default: `all_time`).


* **Response (`200 OK`):**

```json
{
  "game_id": "325add08-5073-43ce-b7fe-35563c82e2de",
  "period": "all_time",
  "total_entries": 2,
  "leaderboard": [
    {
      "player_id": "soldier_76",
      "username": "JackMorrison",
      "clan_tag": "[OW]",
      "clan_name": "Overwatch",
      "score": 1850.0,
      "rank": 1,
      "percentile": 100.0
    }
  ]
}

```

---

#### 3. Get Player Rank Context & Immediate Neighbors

* **`GET /api/leaderboards//player//context/?period=all_time`**
* Returns target player standings alongside immediate neighbors above and below.
* **Response (`200 OK`):**

```json
{
  "player": {
    "player_id": "soldier_76",
    "username": "JackMorrison",
    "clan_tag": "[OW]",
    "score": 1850.0,
    "rank": 2,
    "percentile": 95.0
  },
  "above": {
    "player_id": "reaper_01",
    "username": "GabrielReyes",
    "clan_tag": "[TLN]",
    "score": 1900.0,
    "rank": 1,
    "percentile": 100.0
  },
  "below": {
    "player_id": "tracer_99",
    "username": "LenaOxton",
    "clan_tag": "[OW]",
    "score": 1700.0,
    "rank": 3,
    "percentile": 90.0
  }
}

```

---

#### 4. Drift-Proof Paginated Leaderboard

* **`GET /api/leaderboards//full/?page=1&page_size=50&snapshot_id=`**
* **Snapshot Mechanism**: On the initial call (omitting `snapshot_id`), the engine clones the active ZSET into a pinned snapshot (`lb_snap:`) with a 5-minute TTL. Returning this `snapshot_id` guarantees consistent, drift-free pagination across subsequent pages.
* **Response (`200 OK`):**

```json
{
  "snapshot_id": "6a978f8d-45f8-48b8-b4b6-7917fdf9a39d",
  "page": 1,
  "page_size": 50,
  "total_players": 15420,
  "total_pages": 309,
  "has_next": true,
  "has_previous": false,
  "results": [ ... ]
}

```

---

#### 5. Social & Clan Filtered Boards

* **`GET /api/leaderboards//friends//?period=all_time`**
* **`GET /api/leaderboards//clan//?period=all_time`**
* **Response (`200 OK`):**

```json
{
  "game_id": "325add08-5073-43ce-b7fe-35563c82e2de",
  "filter_type": "friends",
  "filter_entity_id": "soldier_76",
  "period": "all_time",
  "total_active_members": 2,
  "leaderboard": [
    {
      "player_id": "soldier_76",
      "username": "JackMorrison",
      "clan_tag": "[OW]",
      "clan_name": "Overwatch",
      "score": 1850.0,
      "local_rank": 1,
      "global_rank": 4,
      "local_percentile": 100.0,
      "is_target_player": true
    }
  ]
}

```

---

### WebSocket Interface

* **Connection Endpoint**: `ws://:/ws/leaderboards///`
* **Supported Periods**: `daily`, `weekly`, `all_time`

#### Handshake Event (`initial_state`)

Sent immediately to the connected client:

```json
{
  "type": "initial_state",
  "period": "all_time",
  "data": [
    {
      "player_id": "soldier_76",
      "username": "JackMorrison",
      "clan_tag": "[OW]",
      "score": 1850.0,
      "rank": 1,
      "percentile": 100.0
    }
  ]
}

```

#### Broadcast Event (`rank_update`)

Pushed to all active room subscribers when a player sets a new record:

```json
{
  "type": "rank_update",
  "payload": {
    "player_id": "soldier_76",
    "score": 1850.0,
    "rank": 1,
    "percentile": 100.0,
    "period": "all_time",
    "top_10": [ ... ]
  }
}

```

---

## 6. Load Testing & Benchmarks (Locust)

The repository includes a production load-testing suite (`locustfile.py`) designed to simulate real player populations under heavy read/write concurrency.

### Running the Load Test

1. Generate or retrieve benchmark game credentials:

```bash
python manage.py shell -c "from games.models import Game; g, _ = Game.objects.get_or_create(name='BenchmarkArena'); print('GAME_ID =', g.id); print('API_SECRET =', g.api_secret)"

```

2. Run Locust headless (200 concurrent users, 20 ramp-up/sec, 60-second test):

**PowerShell (Windows):**

```powershell
$env:LOCUST_GAME_ID=""
$env:LOCUST_API_SECRET=""
locust -f locustfile.py --headless -u 200 -r 20 --run-time 60s --host http://127.0.0.1:8000 --html benchmark_report.html

```

**Bash (Linux / macOS):**

```bash
LOCUST_GAME_ID="" LOCUST_API_SECRET="" locust -f locustfile.py --headless -u 200 -r 20 --run-time 60s --host http://127.0.0.1:8000 --html benchmark_report.html

```

### Latency Targets

```text
===========================================================================
               BENCHMARK LATENCY REPORT (p95 & p99)
===========================================================================
Endpoint                                   | Reqs   | Avg    | p95    | p99   

```

---

# POST /api/leaderboards/scores/             | 4120   | 14.2ms | 28.0ms | 45.0ms
GET /api/leaderboards/[id]/top/            | 2450   | 3.1ms  | 6.0ms  | 11.0ms
GET /api/leaderboards/[id]/player/[id]/... | 820    | 4.8ms  | 9.0ms  | 16.0ms

```
* **Read Endpoints**: Sub-10ms \(p_{95}\) served directly from in-memory sorted sets.
* **Write Endpoints**: Sub-30ms \(p_{95}\) including HMAC cryptographic validation, rate checks, Redis updates, and database audit inserts.

```

---

## 7. Local Setup & Installation

### Prerequisites

* **Python**: 3.11+
* **Redis**: 5.0+ (RESP2 protocol compliant)
* **PostgreSQL** or **SQLite** (SQLite is configured by default for development)

### Installation Steps

1. **Clone the repository:**

```bash
git clone https://github.com/taizeem/leaderboardAPI.git
cd leaderboardAPI/api

```

2. **Create and activate a virtual environment:**

```bash
python -m venv .venv

# Windows PowerShell:
.venv\Scripts\Activate.ps1

# Linux / macOS:
source .venv/bin/activate

```

3. **Install dependencies:**

```bash
pip install -r requirements.txt

```

4. **Configure Environment Variables:**
Create a `.env` file in the project root:

```ini
DJANGO_SECRET_KEY=production-secret-key-change-this
DEBUG=True
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_DB=0

```

5. **Run database migrations:**

```bash
python manage.py makemigrations games leaderboards
python manage.py migrate

```

6. **Start the ASGI server (Daphne):**

```bash
python manage.py runserver

```

---

## 8. Automated Test Suite

The test suite covers algorithmic tie resolution, race conditions, WebSocket broadcasts, social group isolation, and HMAC security verification.

Run all tests:

```bash
python manage.py test

```

### Test Coverage Highlights

* **`RealTimeLeaderboardTests`**: Validates competition ranking ($1, 2, 2, 4$), percentile math, and multi-period keys.
* **`LeaderboardWebSocketTests`**: Verifies real-time ASGI broadcasts using `TransactionTestCase` and `WebsocketCommunicator`.
* **`SocialLeaderboardTests`**: Validates isolated relative rankings, clan member filtering, and group percentiles.
* **`LeaderboardSecurityTests`**: Validates HMAC-SHA256 signature enforcement, timestamp expiration ($\pm 30\text{s}$), nonce replay protection, and `PlayerScoreThrottle` limits.

---

## 9. Production Deployment Considerations

1. **ASGI Process Management**: Deploy with **Daphne** managed by systemd or run within Docker containers behind an **Nginx** reverse proxy configured for WebSocket upgrades:
```nginx
location /ws/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}

```


2. **Channel Layer Scaling**: In multi-server environments, switch `CHANNEL_LAYERS` in `config/settings.py` from `InMemoryChannelLayer` to `channels_redis.core.RedisChannelLayer`.
3. **Scheduled Archival**: Add a daily cron job to run the archival command at midnight UTC:
```cron
5 0 * * * /path/to/venv/bin/python /path/to/manage.py archive_leaderboards --days-old 7

```