from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from .models import Game, Player

class GamesAppTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_create_and_retrieve_game(self):
        response = self.client.post('/api/games/', {'name': 'CyberDash'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        game_id = response.data['id']

        get_resp = self.client.get(f'/api/games/{game_id}/')
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp.data['name'], 'CyberDash')

    def test_create_and_retrieve_player(self):
        payload = {'id': 'usr_998', 'username': 'AceShooter'}
        response = self.client.post('/api/games/players/', payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        get_resp = self.client.get('/api/games/players/usr_998/')
        self.assertEqual(get_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(get_resp.data['username'], 'AceShooter')