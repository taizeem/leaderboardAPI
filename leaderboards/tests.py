from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from io import StringIO
import fakeredis
from unittest.mock import patch
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from games.models import Game, Player, Clan, Friendship
from leaderboards.services import LeaderboardRedisService
from leaderboards.models import ArchivedLeaderboardEntry
from channels.testing import WebsocketCommunicator
from config.asgi import application 


class RealTimeLeaderboardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.fake_redis = fakeredis.FakeRedis(decode_responses=True)

        # Patch Redis client factory in views and services
        self.redis_patcher = patch(
            'leaderboards.views.get_redis_client',
            return_value=self.fake_redis
        )
        self.redis_patcher.start()

        # Create sample Game and Players
        self.game = Game.objects.create(name="ApexRacer")
        self.p1 = Player.objects.create(id="p1", username="Alice")
        self.p2 = Player.objects.create(id="p2", username="Bob")
        self.p3 = Player.objects.create(id="p3", username="Charlie")
        self.p4 = Player.objects.create(id="p4", username="David")

    def tearDown(self):
        self.redis_patcher.stop()

    def test_req1_and_req4_submit_score_realtime_reflection(self):
        """Req 1 & 4: Player can submit multiple scores and rank reflects instantly."""
        # First submission: 100
        resp1 = self.client.post('/api/leaderboards/scores/', {
            'game': str(self.game.id),
            'player': self.p1.id,
            'score': 100.0
        })
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        # Alice should be Rank 1
        top_resp = self.client.get(f'/api/leaderboards/{self.game.id}/top/?limit=10')
        self.assertEqual(top_resp.data['leaderboard'][0]['player_id'], 'p1')
        self.assertEqual(top_resp.data['leaderboard'][0]['score'], 100.0)

        # Second submission: 250 (higher score updates in place)
        self.client.post('/api/leaderboards/scores/', {
            'game': str(self.game.id),
            'player': self.p1.id,
            'score': 250.0
        })

        top_resp2 = self.client.get(f'/api/leaderboards/{self.game.id}/top/?limit=10')
        self.assertEqual(top_resp2.data['leaderboard'][0]['score'], 250.0)

    def test_req7_tie_handling_standard_competition_rank(self):
        """Req 7: Same scores share rank and next rank skips (e.g., 1, 2, 2, 4)."""
        # Alice = 300, Bob = 200, Charlie = 200, David = 100
        scores = [
            (self.p1.id, 300.0),
            (self.p2.id, 200.0),
            (self.p3.id, 200.0),
            (self.p4.id, 100.0),
        ]
        for pid, score in scores:
            self.client.post('/api/leaderboards/scores/', {
                'game': str(self.game.id),
                'player': pid,
                'score': score
            })

        resp = self.client.get(f'/api/leaderboards/{self.game.id}/top/?limit=10')
        leaderboard = resp.data['leaderboard']

        ranks = {entry['player_id']: entry['rank'] for entry in leaderboard}

        self.assertEqual(ranks['p1'], 1)
        self.assertEqual(ranks['p2'], 2)  # Tied at 200
        self.assertEqual(ranks['p3'], 2)  # Tied at 200
        self.assertEqual(ranks['p4'], 4)  # Rank 3 is skipped!

    def test_req2_top_n_with_percentile(self):
        """Req 2: Verify configurable Top N, score, rank, and percentile calculation."""
        scores = [
            (self.p1.id, 400.0),
            (self.p2.id, 300.0),
            (self.p3.id, 200.0),
            (self.p4.id, 100.0),
        ]
        for pid, score in scores:
            self.client.post('/api/leaderboards/scores/', {
                'game': str(self.game.id),
                'player': pid,
                'score': score
            })

        resp = self.client.get(f'/api/leaderboards/{self.game.id}/top/?limit=2')
        leaderboard = resp.data['leaderboard']

        self.assertEqual(len(leaderboard), 2)
        # p1 is in the 100th percentile (4/4 scores <= 400)
        self.assertEqual(leaderboard[0]['player_id'], 'p1')
        self.assertEqual(leaderboard[0]['percentile'], 100.0)

        # p2 has 3/4 scores <= 300 -> 75%
        self.assertEqual(leaderboard[1]['player_id'], 'p2')
        self.assertEqual(leaderboard[1]['percentile'], 75.0)

    def test_req3_player_rank_and_neighbors(self):
        """Req 3: Returns player's rank + players immediately above and below."""
        scores = [
            (self.p1.id, 500.0),
            (self.p2.id, 400.0),
            (self.p3.id, 300.0),
        ]
        for pid, score in scores:
            self.client.post('/api/leaderboards/scores/', {
                'game': str(self.game.id),
                'player': pid,
                'score': score
            })

        # Query Charlie (p3 - lowest)
        p3_resp = self.client.get(f'/api/leaderboards/{self.game.id}/player/p3/context/')
        self.assertEqual(p3_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(p3_resp.data['player']['rank'], 3)
        self.assertEqual(p3_resp.data['above']['player_id'], 'p2')
        self.assertIsNone(p3_resp.data['below'])

        # Query Bob (p2 - middle)
        p2_resp = self.client.get(f'/api/leaderboards/{self.game.id}/player/p2/context/')
        self.assertEqual(p2_resp.data['player']['rank'], 2)
        self.assertEqual(p2_resp.data['above']['player_id'], 'p1')
        self.assertEqual(p2_resp.data['below']['player_id'], 'p3')

    def test_req5_concurrent_periods(self):
        """Req 5: Verifies daily, weekly, and all_time leaderboards operate concurrently."""
        self.client.post('/api/leaderboards/scores/', {
            'game': str(self.game.id),
            'player': self.p1.id,
            'score': 777.0
        })

        for period in ['daily', 'weekly', 'all_time']:
            resp = self.client.get(f'/api/leaderboards/{self.game.id}/top/?period={period}')
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.data['leaderboard'][0]['player_id'], 'p1')

    def test_req6_snapshot_pagination_integrity(self):
        """
        Req 6: Rank 51 on Page 2 does not repeat Rank 50 on Page 1 even if scores change mid-request.
        """
        # Create 60 players with scores 60 down to 1
        for i in range(1, 61):
            pid = f"player_{i:02d}"
            Player.objects.create(id=pid, username=f"User{i}")
            # Score matches index (player_60 = 60 pts, player_01 = 1 pt)
            LeaderboardRedisService.record_score(
                r=self.fake_redis,
                game_id=str(self.game.id),
                player_id=pid,
                score=float(i)
            )

        # Page 1: Request items 1 to 50
        page1_resp = self.client.get(f'/api/leaderboards/{self.game.id}/full/?page=1&page_size=50')
        self.assertEqual(page1_resp.status_code, status.HTTP_200_OK)
        snapshot_id = page1_resp.data['snapshot_id']
        page1_items = page1_resp.data['results']

        self.assertEqual(len(page1_items), 50)
        self.assertEqual(page1_items[0]['rank'], 1)
        self.assertEqual(page1_items[0]['player_id'], 'player_60')
        self.assertEqual(page1_items[49]['rank'], 50)
        self.assertEqual(page1_items[49]['player_id'], 'player_11')  # Rank 50 is player_11

        # Simulate mid-request score changes in the live leaderboard:
        # A new player joins and gets a high score of 999.0
        Player.objects.create(id="intruder", username="Intruder")
        LeaderboardRedisService.record_score(
            r=self.fake_redis,
            game_id=str(self.game.id),
            player_id="intruder",
            score=999.0
        )

        # Page 2: Request next items using snapshot_id
        page2_resp = self.client.get(
            f'/api/leaderboards/{self.game.id}/full/?page=2&page_size=50&snapshot_id={snapshot_id}'
        )
        self.assertEqual(page2_resp.status_code, status.HTTP_200_OK)
        page2_items = page2_resp.data['results']

        self.assertEqual(len(page2_items), 10)
        # Rank 51 on Page 2 must be player_10
        self.assertEqual(page2_items[0]['rank'], 51)
        self.assertEqual(page2_items[0]['player_id'], 'player_10')

        # Verify no items overlap between Page 1 and Page 2
        p1_ids = {item['player_id'] for item in page1_items}
        p2_ids = {item['player_id'] for item in page2_items}
        self.assertTrue(p1_ids.isdisjoint(p2_ids))

class ArchivingAndTTLTests(TestCase):
    def setUp(self):
        self.fake_redis = fakeredis.FakeRedis(decode_responses=True)
        self.redis_patcher = patch(
            'leaderboards.views.get_redis_client',
            return_value=self.fake_redis
        )
        self.redis_service_patcher = patch(
            'leaderboards.management.commands.archive_leaderboards.get_redis_client',
            return_value=self.fake_redis
        )
        self.redis_patcher.start()
        self.redis_service_patcher.start()

        self.game = Game.objects.create(name="SpeedRunner")
        self.p1 = Player.objects.create(id="u1", username="P1")
        self.p2 = Player.objects.create(id="u2", username="P2")
        self.p3 = Player.objects.create(id="u3", username="P3")

    def tearDown(self):
        self.redis_patcher.stop()
        self.redis_service_patcher.stop()

    def test_automatic_ttl_on_score_submission(self):
        """Verify daily and weekly sets have TTLs set, while all_time has no TTL."""
        LeaderboardRedisService.record_score(
            r=self.fake_redis,
            game_id=str(self.game.id),
            player_id=self.p1.id,
            score=50.0
        )

        keys = LeaderboardRedisService.get_period_keys(str(self.game.id))

        # Daily key should have a TTL (~7 days = 604800s)
        daily_ttl = self.fake_redis.ttl(keys['daily'])
        self.assertGreater(daily_ttl, 0)
        self.assertLessEqual(daily_ttl, 604800)

        # Weekly key should have a TTL (~35 days = 3024000s)
        weekly_ttl = self.fake_redis.ttl(keys['weekly'])
        self.assertGreater(weekly_ttl, 0)
        self.assertLessEqual(weekly_ttl, 3024000)

        # All-time key must not expire (ttl == -1 in Redis)
        all_time_ttl = self.fake_redis.ttl(keys['all_time'])
        self.assertEqual(all_time_ttl, -1)

    def test_archive_command_archives_closed_periods_with_competition_ranks(self):
        """Past daily leaderboard is archived to PostgreSQL with tie handling and deleted from Redis."""
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        yesterday_str = yesterday.strftime('%Y-%m-%d')
        past_key = f"lb:{self.game.id}:daily:{yesterday_str}"

        # Seed past scores with a tie: u1=300, u2=300, u3=150
        self.fake_redis.zadd(past_key, {self.p1.id: 300.0, self.p2.id: 300.0, self.p3.id: 150.0})

        # Run command with --delete-redis
        out = StringIO()
        call_command('archive_leaderboards', '--delete-redis', stdout=out)

        # Assert PostgreSQL records exist
        archived_records = ArchivedLeaderboardEntry.objects.filter(
            game=self.game,
            period_type='daily',
            period_identifier=yesterday_str
        ).order_by('rank')

        self.assertEqual(archived_records.count(), 3)

        # Verify ties: u1 and u2 tied at rank 1, u3 skips to rank 3
        rank_map = {entry.player_id: entry.rank for entry in archived_records}
        self.assertEqual(rank_map['u1'], 1)
        self.assertEqual(rank_map['u2'], 1)
        self.assertEqual(rank_map['u3'], 3)

        # Redis key must be deleted
        self.assertFalse(self.fake_redis.exists(past_key))

    def test_archive_command_skips_active_today_period(self):
        """Current today's daily leaderboard must NOT be archived prematurely."""
        today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        today_key = f"lb:{self.game.id}:daily:{today_str}"
        self.fake_redis.zadd(today_key, {self.p1.id: 500.0})

        out = StringIO()
        call_command('archive_leaderboards', stdout=out)

        # Should not create any rows for today
        self.assertEqual(
            ArchivedLeaderboardEntry.objects.filter(period_identifier=today_str).count(),
            0
        )
        # Active key remains untouched in Redis
        self.assertTrue(self.fake_redis.exists(today_key))


class LeaderboardWebSocketTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.fake_redis = fakeredis.FakeRedis(decode_responses=True)

        self.redis_patcher = patch(
            'leaderboards.views.get_redis_client',
            return_value=self.fake_redis
        )
        self.consumer_redis_patcher = patch(
            'leaderboards.consumers.get_redis_client',
            return_value=self.fake_redis
        )
        self.redis_patcher.start()
        self.consumer_redis_patcher.start()

        self.game = Game.objects.create(name="CyberArena")
        self.player = Player.objects.create(id="neon_1", username="NeonRider")

    def tearDown(self):
        self.redis_patcher.stop()
        self.consumer_redis_patcher.stop()

    async def test_websocket_connect_and_receives_initial_state(self):
        """Verifies WebSocket accepts connection and delivers initial leaderboard data."""
        path = f"/ws/leaderboards/{self.game.id}/all_time/"
        communicator = WebsocketCommunicator(application, path)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # First message must be initial state
        response = await communicator.receive_json_from()
        self.assertEqual(response["type"], "initial_state")
        self.assertEqual(response["period"], "all_time")
        self.assertIsInstance(response["data"], list)

        await communicator.disconnect()

    async def test_score_submission_triggers_live_websocket_broadcast(self):
        """Posting a score via HTTP endpoint must push a live update down the WebSocket."""
        path = f"/ws/leaderboards/{self.game.id}/all_time/"
        communicator = WebsocketCommunicator(application, path)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Discard initial state message
        await communicator.receive_json_from()

        # Submit score via HTTP endpoint
        resp = self.client.post('/api/leaderboards/scores/', {
            'game': str(self.game.id),
            'player': self.player.id,
            'score': 850.0
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

        # Verify broadcast received on WebSocket
        broadcast = await communicator.receive_json_from()
        self.assertEqual(broadcast["type"], "rank_update")
        self.assertEqual(broadcast["payload"]["player_id"], "neon_1")
        self.assertEqual(broadcast["payload"]["score"], 850.0)
        self.assertEqual(broadcast["payload"]["rank"], 1)

        await communicator.disconnect()


class SocialLeaderboardTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.fake_redis = fakeredis.FakeRedis(decode_responses=True)

        self.redis_patcher = patch(
            'leaderboards.views.get_redis_client',
            return_value=self.fake_redis
        )
        self.redis_patcher.start()

        self.game = Game.objects.create(name="StrikeForce")

        # Create Clan
        self.clan_alpha = Clan.objects.create(id="alpha", name="Alpha Wolves", tag="[AW]")
        self.clan_bravo = Clan.objects.create(id="bravo", name="Bravo Guard", tag="[BG]")

        # Create Players
        self.p1 = Player.objects.create(id="p1", username="Alice", clan=self.clan_alpha)
        self.p2 = Player.objects.create(id="p2", username="Bob", clan=self.clan_alpha)
        self.p3 = Player.objects.create(id="p3", username="Charlie", clan=self.clan_alpha)
        self.outsider = Player.objects.create(id="outsider", username="Dave", clan=self.clan_bravo)

        # Establish Friendships: Alice is friends with Bob and Dave (outsider)
        Friendship.objects.create(player=self.p1, friend=self.p2)
        Friendship.objects.create(player=self.p1, friend=self.outsider)

        # Seed global scores in Redis:
        # Dave (outsider) = 1000.0 (Global Rank 1)
        # Alice (p1)      = 800.0  (Global Rank 2, tied)
        # Bob (p2)        = 800.0  (Global Rank 2, tied)
        # Charlie (p3)    = 500.0  (Global Rank 4)
        for pid, score in [("outsider", 1000.0), ("p1", 800.0), ("p2", 800.0), ("p3", 500.0)]:
            LeaderboardRedisService.record_score(
                r=self.fake_redis,
                game_id=str(self.game.id),
                player_id=pid,
                score=score
            )

    def tearDown(self):
        self.redis_patcher.stop()

    def test_friends_leaderboard_includes_only_friends_with_relative_and_global_ranks(self):
        """
        Alice queries her friends leaderboard.
        Expected members: Alice, Bob, and Dave (Charlie is excluded because not Alice's friend).
        """
        url = f"/api/leaderboards/{self.game.id}/friends/{self.p1.id}/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.data['leaderboard']
        self.assertEqual(len(data), 3)

        # Check members and ranks
        # Rank 1: Dave (score 1000.0) -> Local 1, Global 1
        self.assertEqual(data[0]['player_id'], 'outsider')
        self.assertEqual(data[0]['local_rank'], 1)
        self.assertEqual(data[0]['global_rank'], 1)

        # Rank 2 (Tied): Alice and Bob both have 800.0 -> Local 2, Global 2
        self.assertEqual(data[1]['score'], 800.0)
        self.assertEqual(data[1]['local_rank'], 2)
        self.assertEqual(data[1]['global_rank'], 2)

        self.assertEqual(data[2]['score'], 800.0)
        self.assertEqual(data[2]['local_rank'], 2)
        self.assertEqual(data[2]['global_rank'], 2)

        # Target player flag check
        alice_entry = next(entry for entry in data if entry['player_id'] == 'p1')
        self.assertTrue(alice_entry['is_target_player'])

    def test_clan_leaderboard_isolates_clan_members(self):
        """
        Query Alpha clan: Alice, Bob, Charlie must be included.
        Dave (Bravo clan) must be excluded even though he has the highest score globally.
        """
        url = f"/api/leaderboards/{self.game.id}/clan/{self.clan_alpha.id}/"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        data = resp.data['leaderboard']
        self.assertEqual(len(data), 3)

        # Local ranks in Alpha clan:
        # Alice (800) -> Local 1, Global 2
        # Bob (800)   -> Local 1, Global 2 (Tied for Local 1!)
        # Charlie (500) -> Local 3 (Skipped Local 2), Global 4
        pids = [entry['player_id'] for entry in data]
        self.assertIn('p1', pids)
        self.assertIn('p2', pids)
        self.assertIn('p3', pids)
        self.assertNotIn('outsider', pids)

        charlie_entry = next(entry for entry in data if entry['player_id'] == 'p3')
        self.assertEqual(charlie_entry['local_rank'], 3)
        self.assertEqual(charlie_entry['global_rank'], 4)