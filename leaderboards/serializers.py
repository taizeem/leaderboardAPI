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