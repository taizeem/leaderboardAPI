from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import GameViewSet, PlayerViewSet

router = DefaultRouter()
router.register(r'players', PlayerViewSet, basename='player')
router.register(r'', GameViewSet, basename='game')

urlpatterns = [
    path('', include(router.urls)),
]