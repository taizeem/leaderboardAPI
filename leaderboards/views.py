from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import ScoreSubmission
from .serializers import (
    ScoreSubmissionSerializer,
    LeaderboardEntrySerializer,
    PlayerContextResponseSerializer
)
from .services import LeaderboardRedisService, get_redis_client

class SubmitScoreView(APIView):
    """Requirement 1 & 4: Ingests score and updates Redis in <1s."""
    def post(self, request):
        serializer = ScoreSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submission = serializer.save()

        # Update Redis real-time indices immediately
        r = get_redis_client()
        LeaderboardRedisService.record_score(
            r=r,
            game_id=str(submission.game_id),
            player_id=str(submission.player_id),
            score=submission.score
        )

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TopNLeaderboardView(APIView):
    """Requirement 2 & 5: Returns top N players with rank, score, percentile."""
    def get(self, request, game_id):
        limit = int(request.query_params.get('limit', 10))
        period = request.query_params.get('period', 'all_time')

        r = get_redis_client()
        try:
            key = LeaderboardRedisService.get_key_for_period(game_id, period)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        results = LeaderboardRedisService.get_top_n(r, key, limit)
        serializer = LeaderboardEntrySerializer(results, many=True)
        return Response({
            'game_id': game_id,
            'period': period,
            'leaderboard': serializer.data
        })


class PlayerRankContextView(APIView):
    """Requirement 3: Returns player's rank + neighbors directly above and below."""
    def get(self, request, game_id, player_id):
        period = request.query_params.get('period', 'all_time')
        r = get_redis_client()
        try:
            key = LeaderboardRedisService.get_key_for_period(game_id, period)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        context = LeaderboardRedisService.get_player_context(r, key, player_id)
        if not context:
            return Response({'detail': 'Player is not ranked yet.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = PlayerContextResponseSerializer(context)
        return Response(serializer.data)


class PaginatedLeaderboardView(APIView):
    """Requirement 6: Paginated leaderboard isolated by snapshot against score drift."""
    def get(self, request, game_id):
        period = request.query_params.get('period', 'all_time')
        page = max(1, int(request.query_params.get('page', 1)))
        page_size = max(1, min(100, int(request.query_params.get('page_size', 50))))
        snapshot_id = request.query_params.get('snapshot_id', None)

        r = get_redis_client()
        try:
            source_key = LeaderboardRedisService.get_key_for_period(game_id, period)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        data = LeaderboardRedisService.paginate_with_snapshot(
            r=r,
            source_key=source_key,
            snapshot_id=snapshot_id,
            page=page,
            page_size=page_size
        )

        return Response(data)