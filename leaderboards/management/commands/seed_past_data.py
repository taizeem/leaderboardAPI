from datetime import datetime, timezone, timedelta
from django.core.management.base import BaseCommand
from games.models import Game, Player
from leaderboards.services import get_redis_client

class Command(BaseCommand):
    help = "Seeds test games, players, and yesterday's closed scores into Redis."

    def handle(self, *args, **options):
        r = get_redis_client()

        # 1. Ensure a test game exists
        game, created = Game.objects.get_or_create(
            name="NeonRacer",
            defaults={"name": "NeonRacer"}
        )
        self.stdout.write(f"Using Game: {game.name} (UUID: {game.id})")

        # 2. Ensure test players exist in the database
        players_data = [
            ("player_alpha", "AlphaZero"),
            ("player_bravo", "BravoSniper"),
            ("player_charlie", "CharlieGhost"),
            ("player_delta", "DeltaV"),
            ("player_echo", "EchoPulse"),
        ]

        for p_id, uname in players_data:
            Player.objects.get_or_create(id=p_id, defaults={"username": uname})

        # 3. Calculate yesterday's date key (guaranteed to be closed)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        yesterday_str = yesterday.strftime('%Y-%m-%d')
        yesterday_key = f"lb:{game.id}:daily:{yesterday_str}"

        # 4. Define sample scores with an intentional tie:
        # Alpha: 950.0 (Rank 1)
        # Bravo: 800.0 (Rank 2)
        # Charlie: 800.0 (Rank 2 - Tied)
        # Delta: 620.0 (Rank 4 - Skipped Rank 3)
        # Echo: 450.0 (Rank 5)
        scores_payload = {
            "player_alpha": 950.0,
            "player_bravo": 800.0,
            "player_charlie": 800.0,
            "player_delta": 620.0,
            "player_echo": 450.0,
        }

        # Clear existing key if any and populate
        r.delete(yesterday_key)
        r.zadd(yesterday_key, scores_payload)

        self.stdout.write(self.style.SUCCESS(
            f"Successfully seeded {len(scores_payload)} scores into Redis key: {yesterday_key}"
        ))
        self.stdout.write("Scores seeded:")
        for player, score in scores_payload.items():
            self.stdout.write(f"  - {player}: {score} pts")