from django.urls import path
from . import consumers

websocket_urlpatterns = [
    path('ws/leaderboards/<str:game_id>/<str:period>/', consumers.LeaderboardConsumer.as_asgi()),
]