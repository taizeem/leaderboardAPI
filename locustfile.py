import hmac
import hashlib
import time
import uuid
import random
from locust import HttpUser, task, between, events



GAME_ID = "your game id"
API_SECRET = "abc"

# Number of distinct players in the pool.
# A pool of 20,000 players ensures virtual users don't exceed 10 req/min per player.
PLAYER_POOL_SIZE = 20000


def generate_signed_headers(game_id: str, secret: str, player_id: str, score: float):
    """
    Builds canonical message: "<game_id>:<player_id>:<score:.2f>:<timestamp>:<nonce>"
    and produces the HMAC-SHA256 signature and security headers.
    """
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    formatted_score = f"{float(score):.2f}"
    
    message = f"{game_id}:{player_id}:{formatted_score}:{timestamp}:{nonce}"
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return {
        "Content-Type": "application/json",
        "X-Signature": signature,
        "X-Timestamp": str(timestamp),
        "X-Nonce": nonce,
    }


class LeaderboardLoadTestUser(HttpUser):
    """
    Simulates active players playing matches, submitting new high scores,
    and fetching leaderboard standings.
    """
    # Wait between 0.5s and 2.0s between actions per simulated user
    wait_time = between(0.5, 2.0)

    def on_start(self):
        """Pick a primary player ID for this virtual worker thread."""
        self.primary_player_id = f"player_{random.randint(1, PLAYER_POOL_SIZE)}"

    @task(5)
    def submit_score(self):
        """
        Primary Hot Path: Validated score submission.
        Executes: Rate limiter -> Anti-replay SETNX -> HMAC check -> Redis ZADD -> DB Insert.
        """
        # Assign a random score with random variations
        score = round(random.uniform(100.0, 50000.0), 2)
        
        # Pick from player pool to respect individual player rate limits
        player_id = f"player_{random.randint(1, PLAYER_POOL_SIZE)}"
        
        headers = generate_signed_headers(
            game_id=GAME_ID,
            secret=API_SECRET,
            player_id=player_id,
            score=score
        )
        
        payload = {
            "game": GAME_ID,
            "player": player_id,
            "score": score
        }

        with self.client.post(
            "/api/leaderboards/scores/",
            json=payload,
            headers=headers,
            catch_response=True,
            name="POST /api/leaderboards/scores/"
        ) as response:
            if response.status_code == 201:
                response.success()
            elif response.status_code == 429:
                response.failure("Rate limit (429) hit on player ID")
            elif response.status_code == 403:
                response.failure(f"HMAC/Security failure (403): {response.text}")
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(3)
    def view_top_leaderboard(self):
        """Read Path: High-frequency Top-10 queries."""
        period = random.choice(["daily", "weekly", "all_time"])
        self.client.get(
            f"/api/leaderboards/{GAME_ID}/top/?limit=10&period={period}",
            name="GET /api/leaderboards/[id]/top/"
        )

    @task(1)
    def view_player_context(self):
        """Read Path: Contextual rank + neighbors around a player."""
        self.client.get(
            f"/api/leaderboards/{GAME_ID}/player/{self.primary_player_id}/context/?period=all_time",
            name="GET /api/leaderboards/[id]/player/[id]/context/"
        )


# ============================================================================
# SUMMARY REPORTER: Prints exact p95 and p99 statistics when test concludes
# ============================================================================
@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    print("\n" + "=" * 75)
    print("               BENCHMARK LATENCY REPORT (p95 & p99)")
    print("=" * 75)
    print(f"{'Endpoint':<42} | {'Reqs':<6} | {'Avg':<6} | {'p95':<6} | {'p99':<6}")
    print("-" * 75)
    
    for entry in environment.runner.stats.entries.values():
        name = f"{entry.method} {entry.name}"
        p95 = entry.get_response_time_percentile(0.95)
        p99 = entry.get_response_time_percentile(0.99)
        avg = round(entry.avg_response_time, 1)
        reqs = entry.num_requests
        print(f"{name:<42} | {reqs:<6} | {avg:<6} | {p95:<6} | {p99:<6}")
        
    print("=" * 75 + "\n")