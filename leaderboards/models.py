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