from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db.models import Q
from games.models import Clan, Friendship, Player
from .security import HasValidHMACSignature, PlayerScoreThrottle


from .serializers import (
    ScoreSubmissionSerializer,
    LeaderboardEntrySerializer,
    PlayerContextResponseSerializer
)
from .services import LeaderboardRedisService, get_redis_client

def enrich_player_metadata(entries: list) -> list:
    """
    Enriches a list of leaderboard entries with player username, clan tag,
    and clan name from the database in a single batched query.
    """
    if not entries:
        return entries

    player_ids = [entry['player_id'] for entry in entries if 'player_id' in entry]
    players = Player.objects.filter(id__in=player_ids).select_related('clan')

    player_map = {
        p.id: {
            'username': p.username,
            'clan_tag': p.clan.tag if p.clan else None,
            'clan_name': p.clan.name if p.clan else None,
        }
        for p in players
    }

    for entry in entries:
        p_id = entry.get('player_id')
        meta = player_map.get(p_id, {'username': p_id, 'clan_tag': None, 'clan_name': None})
        entry['username'] = meta['username']
        entry['clan_tag'] = meta['clan_tag']
        entry['clan_name'] = meta['clan_name']

    return entries
class SubmitScoreView(APIView):
    """
    Ingests score, updates Redis real-time indices, broadcasts live updates
    over WebSockets, and returns the player's updated standings.
    Protected by HMAC-SHA256 signature verification and scoped rate limiting.
    """
    permission_classes = [HasValidHMACSignature]
    throttle_classes = [PlayerScoreThrottle]

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

        # 2. Gather standings & broadcast WebSocket updates in a single loop
        channel_layer = get_channel_layer()
        standings_summary = {}
        periods = ['daily', 'weekly', 'all_time']

        for period in periods:
            key = LeaderboardRedisService.get_key_for_period(game_id, period)
            rank = LeaderboardRedisService.calculate_rank(r, key, score)
            percentile = LeaderboardRedisService.calculate_percentile(r, key, score)
            
            standings_summary[period] = {
                'rank': rank,
                'percentile': percentile
            }

            if channel_layer:
                top_10 = LeaderboardRedisService.get_top_n(r, key, 10)
                enrich_player_metadata(top_10)

                payload = {
                    "player_id": player_id,
                    "score": score,
                    "rank": rank,
                    "percentile": percentile,
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

        # 3. Return response with current standings
        response_data = serializer.data
        response_data['current_standings'] = standings_summary
        return Response(response_data, status=status.HTTP_201_CREATED)


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

class FriendsLeaderboardView(APIView):
    """
    Returns the leaderboard filtered to only the player's friends + the player themselves.
    """
    def get(self, request, game_id, player_id):
        period = request.query_params.get('period', 'all_time')

        # 1. Fetch friend IDs (bidirectional: player is in 'player' or 'friend' column)
        friend_ids = list(Friendship.objects.filter(player_id=player_id).values_list('friend_id', flat=True))
        reverse_friend_ids = list(Friendship.objects.filter(friend_id=player_id).values_list('player_id', flat=True))
        
        # Include all unique friends + the requesting player
        all_ids = list(set(friend_ids + reverse_friend_ids + [player_id]))

        # 2. Redis lookup
        r = get_redis_client()
        try:
            key = LeaderboardRedisService.get_key_for_period(game_id, period)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        entries = LeaderboardRedisService.get_subset_leaderboard(
            r=r,
            key=key,
            player_ids=all_ids,
            target_player_id=player_id
        )

        response_payload = {
            'game_id': str(game_id),
            'filter_type': 'friends',
            'filter_entity_id': player_id,
            'period': period,
            'total_active_members': len(entries),
            'leaderboard': entries
        }
        return Response(response_payload)


class ClanLeaderboardView(APIView):
    """
    Returns the leaderboard filtered to members of a specific clan.
    """
    def get(self, request, game_id, clan_id):
        period = request.query_params.get('period', 'all_time')

        # Verify clan exists
        try:
            clan = Clan.objects.get(id=clan_id)
        except Clan.DoesNotExist:
            return Response({'error': f"Clan '{clan_id}' not found."}, status=status.HTTP_404_NOT_FOUND)

        # Fetch all player IDs in this clan
        member_ids = list(clan.members.values_list('id', flat=True))

        # Redis lookup
        r = get_redis_client()
        try:
            key = LeaderboardRedisService.get_key_for_period(game_id, period)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        entries = LeaderboardRedisService.get_subset_leaderboard(
            r=r,
            key=key,
            player_ids=member_ids
        )

        response_payload = {
            'game_id': str(game_id),
            'filter_type': 'clan',
            'filter_entity_id': clan.id,
            'period': period,
            'total_active_members': len(entries),
            'leaderboard': entries
        }
        return Response(response_payload)