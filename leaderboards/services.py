import uuid
from datetime import datetime, timezone
import redis
from django.conf import settings

def get_redis_client():
    return redis.Redis(
        host=getattr(settings, 'REDIS_HOST', '127.0.0.1'),
        port=getattr(settings, 'REDIS_PORT', 6379),
        db=getattr(settings, 'REDIS_DB', 1),
        decode_responses=True
    )

class LeaderboardRedisService:
    SNAPSHOT_TTL = 300  # 5 minutes snapshot stability

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
        Atomically updates the player's best score across daily, weekly, and all-time boards.
        Uses Redis ZADD with GT=True to retain the player's highest score.
        """
        keys = cls.get_period_keys(game_id)
        pipe = r.pipeline()
        for key in keys.values():
            # GT=True ensures score only updates if the new score is strictly greater
            pipe.zadd(key, {player_id: score}, gt=True)
        pipe.execute()

    @classmethod
    def calculate_rank(cls, r: redis.Redis, key: str, score: float) -> int:
        """
        Calculates Competition Rank (1224 ties):
        Rank = 1 + count of players with score strictly greater than this score.
        """
        higher_count = r.zcount(key, f"({score}", "+inf")
        return higher_count + 1

    @classmethod
    def calculate_percentile(cls, r: redis.Redis, key: str, score: float) -> float:
        """
        Calculates Percentile:
        (Number of players with score <= current score / total players) * 100
        """
        total = r.zcard(key)
        if total == 0:
            return 100.0
        le_count = r.zcount(key, "-inf", score)
        return round((le_count / total) * 100.0, 2)

    @classmethod
    def get_top_n(cls, r: redis.Redis, key: str, n: int) -> list:
        """
        Fetches top N entries, assigning equal ranks for tied scores and skipping ranks accordingly.
        """
        raw_entries = r.zrevrange(key, 0, n - 1, withscores=True)
        if not raw_entries:
            return []

        total_players = r.zcard(key)
        results = []

        current_rank = 1
        for idx, (player_id, score) in enumerate(raw_entries):
            if idx > 0 and score == raw_entries[idx - 1][1]:
                rank = results[idx - 1]['rank']
            else:
                rank = idx + 1
                current_rank = rank

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
        """
        Retrieves player rank, percentile, and the immediate player above and below.
        """
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

        # Player above
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

        # Player below
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

        return {
            'player': player_data,
            'above': above,
            'below': below
        }

    @classmethod
    def paginate_with_snapshot(cls, r: redis.Redis, source_key: str, snapshot_id: str, page: int, page_size: int):
        """
        Provides stable pagination. Copies source_key into a temporary key on page 1 request.
        Subsequent pages read from the exact snapshot, ensuring no shifting ranks.
        """
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

        for idx, (p_id, score) in enumerate(raw_items):
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