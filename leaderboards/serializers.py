from rest_framework import serializers
from .models import ScoreSubmission

class ScoreSubmissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScoreSubmission
        fields = ['id', 'game', 'player', 'score', 'created_at']
        read_only_fields = ['id', 'created_at']


class LeaderboardEntrySerializer(serializers.Serializer):
    player_id = serializers.CharField()
    score = serializers.FloatField()
    rank = serializers.IntegerField()
    percentile = serializers.FloatField()


class PlayerContextResponseSerializer(serializers.Serializer):
    player = LeaderboardEntrySerializer()
    above = LeaderboardEntrySerializer(allow_null=True)
    below = LeaderboardEntrySerializer(allow_null=True)

class SubsetLeaderboardEntrySerializer(serializers.Serializer):
    player_id = serializers.CharField()
    score = serializers.FloatField()
    local_rank = serializers.IntegerField()
    global_rank = serializers.IntegerField()
    local_percentile = serializers.FloatField()
    is_target_player = serializers.BooleanField()


class FilteredLeaderboardResponseSerializer(serializers.Serializer):
    game_id = serializers.CharField()
    filter_type = serializers.CharField()  # "friends" or "clan"
    filter_entity_id = serializers.CharField()
    period = serializers.CharField()
    total_active_members = serializers.IntegerField()
    leaderboard = SubsetLeaderboardEntrySerializer(many=True)