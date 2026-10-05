from django.db import models
from games.models import Game, Player

class ScoreSubmission(models.Model):
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='scores')
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='scores')
    score = models.FloatField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['game', 'player', '-score']),
        ]

    def __str__(self):
        return f"{self.player_id} -> {self.game.name}: {self.score}"

class ArchivedLeaderboardEntry(models.Model):
    """
    Stores historical snapshots of closed competition periods (e.g., past days, past weeks).
    """
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name='archived_leaderboards')
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='archived_ranks')
    period_type = models.CharField(max_length=20)  # 'daily' or 'weekly'
    period_identifier = models.CharField(max_length=50)  # e.g., '2026-10-03' or '2026-W39'
    rank = models.PositiveIntegerField()
    score = models.FloatField()
    percentile = models.FloatField()
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['period_identifier', 'rank']
        unique_together = ('game', 'period_type', 'period_identifier', 'player')
        indexes = [
            models.Index(fields=['game', 'period_type', 'period_identifier', 'rank']),
        ]

    def __str__(self):
        return f"[{self.period_type}:{self.period_identifier}] Rank {self.rank}: {self.player_id} ({self.score})"