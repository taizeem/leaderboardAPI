from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(
        r'^ws/leaderboards/(?P<game_id>[0-9a-fA-F-]+)/(?P<period>daily|weekly|all_time)/$',
        consumers.LeaderboardConsumer.as_asgi()
    ),
]