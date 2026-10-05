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

    @classmethod
    def get_period_keys(cls, game_id: str) -> dict:
        now = datetime.now(timezone.utc)
        today = now.strftime('%Y-%m-%d')
        year_week = now.strftime('%Y-%W')
        return {
            'daily': f"lb:{game_id}:daily:{today}",
            'weekly': f"lb:{game_id}:weekly:{year_week}",
            'all_time': f"lb:{game_id}:all_time",
        }

    @classmethod
    def get_key_for_period(cls, game_id: str, period: str) -> str:
        keys = cls.get_period_keys(game_id)
        if period in keys:
            return keys[period]
        if period == 'week':
            return keys['weekly']
        raise ValueError(f"Invalid period: '{period}'. Must be 'daily', 'weekly', or 'all_time'.")

    @classmethod
    def record_score(cls, r: redis.Redis, game_id: str, player_id: str, score: float):
        """
        Updates the player's score across daily, weekly, and all-time boards 
        only if the new score is strictly greater than their current score.
        Fully compatible with Redis 5.0+.
        """
        keys = cls.get_period_keys(game_id)

        # 1. Pipeline check of current scores across all periods
        pipe = r.pipeline()
        for key in keys.values():
            pipe.zscore(key, player_id)
        current_scores = pipe.execute()

        # 2. Update boards where new score is higher or player has no score yet
        update_pipe = r.pipeline()
        has_updates = False

        for (period_type, key), current in zip(keys.items(), current_scores):
            if current is None or score > float(current):
                update_pipe.zadd(key, {player_id: score})
                ttl = cls.RETENTION_CONFIG.get(period_type)
                if ttl:
                    update_pipe.expire(key, ttl)
                has_updates = True

        if has_updates:
            update_pipe.execute()

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
    @classmethod
    def get_subset_leaderboard(cls, r: redis.Redis, key: str, player_ids: list, target_player_id: str = None) -> list:
        """
        Calculates a relative leaderboard for an arbitrary group of players (e.g. friends, clan).
        
        - Uses a single pipeline to extract scores and global ranks in O(M).
        - Computes local competition ranking with tie handling (1224 ties).
        - Computes local percentile among the subset.
        """
        if not player_ids:
            return []

        # 1. Pipelined score fetch
        pipe = r.pipeline()
        for pid in player_ids:
            pipe.zscore(key, pid)
        raw_scores = pipe.execute()

        # 2. Filter out unranked members (score is None)
        active_entries = []
        for pid, score in zip(player_ids, raw_scores):
            if score is not None:
                active_entries.append((pid, float(score)))

        if not active_entries:
            return []

        # 3. Sort by score descending
        active_entries.sort(key=lambda x: x[1], reverse=True)

        total_in_subset = len(active_entries)

        # 4. Pipelined global rank calculation for active subset members
        rank_pipe = r.pipeline()
        for pid, score in active_entries:
            rank_pipe.zcount(key, f"({score}", "+inf")
        global_higher_counts = rank_pipe.execute()

        # 5. Build results with local 1224 tie handling
        results = []
        for idx, ((pid, score), higher_count) in enumerate(zip(active_entries, global_higher_counts)):
            # Local tie handling
            if idx > 0 and score == active_entries[idx - 1][1]:
                local_rank = results[idx - 1]['local_rank']
            else:
                local_rank = idx + 1

            # Local percentile: count of players in subset with score <= this score
            le_count = sum(1 for _, s in active_entries if s <= score)
            local_percentile = round((le_count / total_in_subset) * 100.0, 2)

            results.append({
                'player_id': pid,
                'score': score,
                'local_rank': local_rank,
                'global_rank': higher_count + 1,
                'local_percentile': local_percentile,
                'is_target_player': (pid == target_player_id) if target_player_id else False
            })

        return results