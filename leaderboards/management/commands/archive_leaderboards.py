from datetime import datetime, timezone
from django.core.management.base import BaseCommand
from django.db import transaction
from leaderboards.models import ArchivedLeaderboardEntry
from leaderboards.services import LeaderboardRedisService, get_redis_client
from games.models import Game, Player

class Command(BaseCommand):
    help = "Archives closed daily and weekly Redis leaderboards into PostgreSQL and cleans up Redis."

    def add_arguments(self, parser):
        parser.add_argument(
            '--period',
            type=str,
            choices=['daily', 'weekly', 'all'],
            default='all',
            help='Leaderboard window type to archive.'
        )
        parser.add_argument(
            '--game-id',
            type=str,
            default=None,
            help='Filter to archive only a specific Game UUID.'
        )
        parser.add_argument(
            '--delete-redis',
            action='store_true',
            help='Delete Redis sorted set key once persisted to PostgreSQL (otherwise keeps key with existing TTL).'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Scan and report eligible leaderboards without saving to database.'
        )

    def handle(self, *args, **options):
        r = get_redis_client()
        period_filter = options['period']
        game_id_filter = options['game_id']
        delete_redis = options['delete_redis']
        dry_run = options['dry_run']

        # Determine pattern match
        pattern = "lb:*"
        if game_id_filter:
            pattern = f"lb:{game_id_filter}:*"

        now = datetime.now(timezone.utc)
        keys_to_process = []

        # Discover matching candidate keys via SCAN
        for key in r.scan_iter(match=pattern):
            parsed = LeaderboardRedisService.parse_key(key)
            if not parsed:
                continue

            period_type = parsed['period_type']
            if period_filter != 'all' and period_type != period_filter:
                continue

            if LeaderboardRedisService.is_period_closed(period_type, parsed['period_identifier'], now):
                keys_to_process.append((key, parsed))

        if not keys_to_process:
            self.stdout.write(self.style.SUCCESS("No closed leaderboards found for archiving."))
            return

        self.stdout.write(f"Found {len(keys_to_process)} closed leaderboard set(s) to process.")

        for key, parsed in keys_to_process:
            game_id = parsed['game_id']
            p_type = parsed['period_type']
            p_ident = parsed['period_identifier']

            # Confirm Game exists in DB
            try:
                game = Game.objects.get(id=game_id)
            except Game.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"Skipping {key}: Game {game_id} not found in DB."))
                continue

            ranked_items = LeaderboardRedisService.extract_full_ranked_dataset(r, key)
            if not ranked_items:
                self.stdout.write(f"Skipping empty key: {key}")
                if delete_redis and not dry_run:
                    r.delete(key)
                continue

            self.stdout.write(f"Processing {key} -> {len(ranked_items)} entries (Game: {game.name})")

            if dry_run:
                self.stdout.write(self.style.NOTICE(f"[DRY-RUN] Would archive {len(ranked_items)} entries for {p_type} ({p_ident})."))
                continue

            # Ensure all players exist in PostgreSQL before foreign key assignment
            player_ids = [item['player_id'] for item in ranked_items]
            existing_player_ids = set(Player.objects.filter(id__in=player_ids).values_list('id', flat=True))
            missing_ids = set(player_ids) - existing_player_ids
            if missing_ids:
                Player.objects.bulk_create([
                    Player(id=pid, username=f"Player_{pid}") for pid in missing_ids
                ])

            # Prepare entries for database insertion
            entries_to_create = [
                ArchivedLeaderboardEntry(
                    game=game,
                    player_id=item['player_id'],
                    period_type=p_type,
                    period_identifier=p_ident,
                    rank=item['rank'],
                    score=item['score'],
                    percentile=item['percentile']
                )
                for item in ranked_items
            ]

            with transaction.atomic():
                # Upsert or ignore conflicts if rerun
                ArchivedLeaderboardEntry.objects.bulk_create(
                    entries_to_create,
                    ignore_conflicts=True,
                    batch_size=1000
                )

            self.stdout.write(self.style.SUCCESS(f"Successfully archived {key} to PostgreSQL."))

            if delete_redis:
                r.delete(key)
                self.stdout.write(f"Deleted Redis key: {key}")