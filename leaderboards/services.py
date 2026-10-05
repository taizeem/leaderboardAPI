import uuid
import re
from datetime import datetime, timezone
import redis
from django.conf import settings

def get_redis_client():
    return redis.Redis(
        host=getattr(settings, 'REDIS_HOST', '127.0.0.1'),
        port=getattr(settings, 'REDIS_PORT', 6379),
        db=getattr(settings, 'REDIS_DB', 1),
        decode_responses=True,
        protocol=2
    )

class LeaderboardRedisService:
    SNAPSHOT_TTL = 300  # 5 minutes for stable pagination

    # Automatic Redis retention windows (in seconds)
    RETENTION_CONFIG = {
        'daily': 86400 * 7,      # Retain daily sets for 7 days
        'weekly': 86400 * 35,    # Retain weekly sets for 35 days (5 weeks)
        'all_time': None,        # All-time board does not expire
    }

    @staticmethod
    def get_period_keys(game_id: str, dt: datetime = None) -> dict:
        if dt is None:
            dt = datetime.now(timezone.utc)
        daily_str = dt.strftime('%Y-%m-%d')
        weekly_str = dt.strftime('%Y-W%W')
        return {
            'daily': f"lb:{game_id}:daily:{daily_str}",
            'weekly': f"lb:{game_id}:weekly:{weekly_str}",
            'all_time': f"lb:{game_id}:all_time",
        }

    @classmethod
    def get_key_for_period(cls, game_id: str, period: str, dt: datetime = None) -> str:
        keys = cls.get_period_keys(game_id, dt)
        if period not in keys:
            raise ValueError(f"Invalid period: {period}. Must be one of {list(keys.keys())}")
        return keys[period]

    @classmethod
    def record_score(cls, r: redis.Redis, game_id: str, player_id: str, score: float):
        """
        Atomically updates the player's best score and applies automatic TTL to transient sets.
        """
        keys = cls.get_period_keys(game_id)
        pipe = r.pipeline()
        for period_type, key in keys.items():
            pipe.zadd(key, {player_id: score}, gt=True)
            ttl = cls.RETENTION_CONFIG.get(period_type)
            if ttl:
                # nx=True sets expiration only if key does not have an active TTL already
                pipe.expire(key, ttl, nx=True)
        pipe.execute()

    @classmethod
    def calculate_rank(cls, r: redis.Redis, key: str, score: float) -> int:
        higher_count = r.zcount(key, f"({score}", "+inf")
        return higher_count + 1

    @classmethod
    def calculate_percentile(cls, r: redis.Redis, key: str, score: float) -> float:
        total = r.zcard(key)
        if total == 0:
            return 100.0
        le_count = r.zcount(key, "-inf", score)
        return round((le_count / total) * 100.0, 2)

    @classmethod
    def get_top_n(cls, r: redis.Redis, key: str, n: int) -> list:
        raw_entries = r.zrevrange(key, 0, n - 1, withscores=True)
        if not raw_entries:
            return []

        total_players = r.zcard(key)
        results = []

        for idx, (player_id, score) in enumerate(raw_entries):
            if idx > 0 and score == raw_entries[idx - 1][1]:
                rank = results[idx - 1]['rank']
            else:
                rank = idx + 1

            le_count = r.zcount(key, "-inf", score)
            percentile = round((le_count / total_players) * 100.0, 2) if total_players > 0 else 100.0

            results.append({
                'player_id': player_id,
                'score': score,
                'rank': rank,
                'percentile': percentile
            })
        return results

    @classmethod
    def get_player_context(cls, r: redis.Redis, key: str, player_id: str) -> dict:
        score = r.zscore(key, player_id)
        if score is None:
            return None

        rank = cls.calculate_rank(r, key, score)
        percentile = cls.calculate_percentile(r, key, score)
        idx = r.zrevrank(key, player_id)

        player_data = {
            'player_id': player_id,
            'score': score,
            'rank': rank,
            'percentile': percentile
        }

        above = None
        if idx > 0:
            above_raw = r.zrevrange(key, idx - 1, idx - 1, withscores=True)
            if above_raw:
                p_id, p_score = above_raw[0]
                above = {
                    'player_id': p_id,
                    'score': p_score,
                    'rank': cls.calculate_rank(r, key, p_score),
                    'percentile': cls.calculate_percentile(r, key, p_score)
                }

        below = None
        total = r.zcard(key)
        if idx + 1 < total:
            below_raw = r.zrevrange(key, idx + 1, idx + 1, withscores=True)
            if below_raw:
                p_id, p_score = below_raw[0]
                below = {
                    'player_id': p_id,
                    'score': p_score,
                    'rank': cls.calculate_rank(r, key, p_score),
                    'percentile': cls.calculate_percentile(r, key, p_score)
                }

        return {'player': player_data, 'above': above, 'below': below}

    @classmethod
    def paginate_with_snapshot(cls, r: redis.Redis, source_key: str, snapshot_id: str, page: int, page_size: int):
        if not snapshot_id:
            snapshot_id = str(uuid.uuid4())
            snapshot_key = f"lb:snapshot:{snapshot_id}"
            r.zunionstore(snapshot_key, [source_key])
            r.expire(snapshot_key, cls.SNAPSHOT_TTL)
        else:
            snapshot_key = f"lb:snapshot:{snapshot_id}"
            if not r.exists(snapshot_key):
                r.zunionstore(snapshot_key, [source_key])
                r.expire(snapshot_key, cls.SNAPSHOT_TTL)

        total_count = r.zcard(snapshot_key)
        start = (page - 1) * page_size
        end = start + page_size - 1

        raw_items = r.zrevrange(snapshot_key, start, end, withscores=True)
        items = []

        for p_id, score in raw_items:
            rank = cls.calculate_rank(r, snapshot_key, score)
            percentile = cls.calculate_percentile(r, snapshot_key, score)
            items.append({
                'player_id': p_id,
                'score': score,
                'rank': rank,
                'percentile': percentile
            })

        return {
            'snapshot_id': snapshot_id,
            'total_players': total_count,
            'page': page,
            'page_size': page_size,
            'results': items
        }

    # ================= ARCHIVING HELPERS ================= #

    @staticmethod
    def parse_key(key: str):
        """
        Parses keys like:
        - lb:<game_id>:daily:2026-10-03
        - lb:<game_id>:weekly:2026-W39
        """
        match = re.match(r"^lb:([0-9a-fA-F-]+):(daily|weekly):(.+)$", key)
        if not match:
            return None
        return {
            'game_id': match.group(1),
            'period_type': match.group(2),
            'period_identifier': match.group(3)
        }

    @classmethod
    def is_period_closed(cls, period_type: str, identifier: str, current_dt: datetime = None) -> bool:
        """
        Determines whether a daily or weekly period has finished based on current UTC time.
        """
        if current_dt is None:
            current_dt = datetime.now(timezone.utc)

        if period_type == 'daily':
            try:
                target_date = datetime.strptime(identifier, '%Y-%m-%d').date()
                return target_date < current_dt.date()
            except ValueError:
                return False

        elif period_type == 'weekly':
            try:
                # Format: YYYY-W%W
                year_str, week_str = identifier.split('-W')
                target_year, target_week = int(year_str), int(week_str)
                curr_year, curr_week = current_dt.year, int(current_dt.strftime('%W'))
                return (target_year, target_week) < (curr_year, curr_week)
            except (ValueError, IndexError):
                return False

        return False

    @classmethod
    def extract_full_ranked_dataset(cls, r: redis.Redis, key: str) -> list:
        """
        Retrieves all players in a key and computes standard competition ranks (1224 ties)
        and percentiles in memory.
        """
        raw_entries = r.zrevrange(key, 0, -1, withscores=True)
        if not raw_entries:
            return []

        total = len(raw_entries)
        results = []

        # In a sorted descending list, all players sharing score S share rank:
        # rank = first index of score S + 1
        # count of players <= S = total - first index of score S
        first_index_map = {}
        for idx, (_, score) in enumerate(raw_entries):
            if score not in first_index_map:
                first_index_map[score] = idx

        for player_id, score in raw_entries:
            first_idx = first_index_map[score]
            rank = first_idx + 1
            le_count = total - first_idx
            percentile = round((le_count / total) * 100.0, 2)
            results.append({
                'player_id': player_id,
                'score': score,
                'rank': rank,
                'percentile': percentile
            })

        return results