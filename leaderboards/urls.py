from django.urls import path
from .views import (
    SubmitScoreView,
    TopNLeaderboardView,
    PlayerRankContextView,
    PaginatedLeaderboardView,
    FriendsLeaderboardView,
    ClanLeaderboardView,
)

urlpatterns = [
    path('scores/', SubmitScoreView.as_view(), name='submit-score'),
    path('<uuid:game_id>/top/', TopNLeaderboardView.as_view(), name='top-leaderboard'),
    path('<uuid:game_id>/player/<str:player_id>/context/', PlayerRankContextView.as_view(), name='player-context'),
    path('<uuid:game_id>/full/', PaginatedLeaderboardView.as_view(), name='paginated-leaderboard'),

    path('<uuid:game_id>/friends/<str:player_id>/', FriendsLeaderboardView.as_view(), name='friends-leaderboard'),
    path('<uuid:game_id>/clan/<str:clan_id>/', ClanLeaderboardView.as_view(), name='clan-leaderboard'),
]