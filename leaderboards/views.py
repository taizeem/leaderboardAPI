from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .models import ScoreSubmission
from .serializers import (
    ScoreSubmissionSerializer,
    LeaderboardEntrySerializer,
    PlayerContextResponseSerializer
)
from .services import LeaderboardRedisService, get_redis_client

class SubmitScoreView(APIView):
    """
    Ingests score, updates Redis real-time indices, and broadcasts
    the rank update to WebSocket listeners via Redis Pub/Sub.
    """
    def post(self, request):
        serializer = ScoreSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        submission = serializer.save()

        game_id = str(submission.game_id)
        player_id = str(submission.player_id)
        score = submission.score

        # 1. Update Redis sorted sets
        r = get_redis_client()
        LeaderboardRedisService.record_score(
            r=r,
            game_id=game_id,
            player_id=player_id,
            score=score
        )

        # 2. Broadcast updates to active WebSocket subscribers
        channel_layer = get_channel_layer()
        periods = ['daily', 'weekly', 'all_time']

        for period in periods:
            key = LeaderboardRedisService.get_key_for_period(game_id, period)
            player_rank = LeaderboardRedisService.calculate_rank(r, key, score)
            top_10 = LeaderboardRedisService.get_top_n(r, key, 10)

            payload = {
                "player_id": player_id,
                "score": score,
                "rank": player_rank,
                "period": period,
                "top_10": top_10
            }

            group_name = f"leaderboard_{game_id}_{period}"
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "leaderboard_update",
                    "payload": payload,
                }
            )

        return Response(serializer.data, status=status.HTTP_201_CREATED)


class TopNLeaderboardView(APIView):
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