from django.db import models
import uuid

class Game(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name
class Clan(models.Model):
    id = models.CharField(max_length=32, primary_key=True)  # e.g. "clan_vanguard"
    name = models.CharField(max_length=100)
    tag = models.CharField(max_length=8)  # e.g. "[VNG]"
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tag} {self.name}"

class Player(models.Model):
    id = models.CharField(max_length=64, primary_key=True)  # Game-assigned external ID or username
    username = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.username} ({self.id})"

class Friendship(models.Model):
    """
    Bidirectional relationship between two players.
    """
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='friendships')
    friend = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='friended_by')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('player', 'friend')

    def __str__(self):
        return f"{self.player_id} <-> {self.friend_id}"