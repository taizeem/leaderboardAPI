from django.db.models import Q
from rest_framework import viewsets
from games.models import Clan, Friendship, Player
from .models import Game, Player
from .serializers import GameSerializer, PlayerSerializer, FilteredLeaderboardResponseSerializer

class GameViewSet(viewsets.ModelViewSet):
    queryset = Game.objects.all()
    serializer_class = GameSerializer


class PlayerViewSet(viewsets.ModelViewSet):
    queryset = Player.objects.all()
    serializer_class = PlayerSerializer

