import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from .services import LeaderboardRedisService, get_redis_client

class LeaderboardConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.game_id = self.scope['url_route']['kwargs']['game_id']
        self.period = self.scope['url_route']['kwargs']['period']
        
        # Redis Pub/Sub channel group name
        self.group_name = f"leaderboard_{self.game_id}_{self.period}"

        # Join the channel group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )
        await self.accept()

        # Send initial standings immediately upon connect
        initial_data = await self.get_top_leaderboard()
        await self.send(text_data=json.dumps({
            "type": "initial_state",
            "period": self.period,
            "data": initial_data
        }))

    async def disconnect(self, close_code):
        # Leave channel group
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

    async def leaderboard_update(self, event):
        """
        Handler for events sent via channel_layer.group_send
        """
        await self.send(text_data=json.dumps({
            "type": "rank_update",
            "payload": event["payload"]
        }))

    @database_sync_to_async
    def get_top_leaderboard(self):
        r = get_redis_client()
        try:
            key = LeaderboardRedisService.get_key_for_period(self.game_id, self.period)
            return LeaderboardRedisService.get_top_n(r, key, 10)
        except Exception:
            return []