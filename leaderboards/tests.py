import fakeredis
from unittest.mock import patch
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from games.models import Game, Player
from leaderboards.services import LeaderboardRedisService

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