from rest_framework import serializers
from .models import Game, Player

class GameSerializer(serializers.ModelSerializer):
    class Model:
        model = Game
        fields = ['id', 'name', 'created_at']

    class Meta:
        model = Game
        fields = '__all__'


class PlayerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Player
        fields = '__all__'