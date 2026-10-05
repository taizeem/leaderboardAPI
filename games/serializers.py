from rest_framework import serializers
from .models import Game, Player, Clan

class ClanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Clan
        fields = ['id', 'name', 'tag', 'created_at']
class GameSerializer(serializers.ModelSerializer):
    class Meta:
        model = Game
        fields = ['id', 'name', 'created_at']


class PlayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Player
        fields = ['id', 'username', 'clan', 'created_at']