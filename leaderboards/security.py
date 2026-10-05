import hmac
import hashlib
import time
from rest_framework.permissions import BasePermission
from rest_framework.exceptions import PermissionDenied
from rest_framework.throttling import SimpleRateThrottle
from games.models import Game
from .services import get_redis_client


def generate_hmac_signature(secret: str, game_id: str, player_id: str, score: float, timestamp: int, nonce: str) -> str:
    """
    Constructs the canonical signature string:
    "<game_id>:<player_id>:<score>:<timestamp>:<nonce>"
    and computes its HMAC-SHA256 digest.
    """
    # Normalize score to float string to ensure identical float formatting
    formatted_score = f"{float(score):.2f}"
    message = f"{game_id}:{player_id}:{formatted_score}:{timestamp}:{nonce}"
    return hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()


class HasValidHMACSignature(BasePermission):
    """
    Enforces HMAC-SHA256 signature verification, replay attack prevention,
    and request expiration.
    """
    MAX_CLOCK_DRIFT_SECONDS = 30  # Allow +- 30s clock skew
    NONCE_TTL_SECONDS = 60         # Retain used nonces in Redis for 60s

    def has_permission(self, request, view):
        # 1. Read required headers
        received_signature = request.headers.get('X-Signature')
        timestamp_header = request.headers.get('X-Timestamp')
        nonce = request.headers.get('X-Nonce')

        if not all([received_signature, timestamp_header, nonce]):
            raise PermissionDenied(
                "Missing security headers. 'X-Signature', 'X-Timestamp', and 'X-Nonce' are required."
            )

        # 2. Validate timestamp format and clock drift (Replay Window)
        try:
            timestamp = int(timestamp_header)
        except ValueError:
            raise PermissionDenied("Invalid 'X-Timestamp' header. Must be an integer Unix timestamp.")

        now = int(time.time())
        if abs(now - timestamp) > self.MAX_CLOCK_DRIFT_SECONDS:
            raise PermissionDenied(
                f"Request timestamp expired or skewed beyond allowable window ({self.MAX_CLOCK_DRIFT_SECONDS}s)."
            )

        # 3. Extract required payload fields
        game_id = request.data.get('game')
        player_id = request.data.get('player')
        score = request.data.get('score')

        if not all([game_id, player_id, score is not None]):
            raise PermissionDenied("Missing 'game', 'player', or 'score' in request payload.")

        # 4. Fetch the Game's api_secret
        try:
            game = Game.objects.get(id=game_id)
        except (Game.DoesNotExist, ValueError):
            raise PermissionDenied(f"Game '{game_id}' not found.")

        # 5. Prevent Replay Attacks using Redis SETNX
        r = get_redis_client()
        nonce_key = f"nonce:{game.id}:{nonce}"
        # set(..., nx=True, ex=...) returns True if new, None/False if already present
        is_fresh = r.set(nonce_key, "1", nx=True, ex=self.NONCE_TTL_SECONDS)
        if not is_fresh:
            raise PermissionDenied(f"Replay attack detected. Nonce '{nonce}' has already been processed.")

        # 6. Verify HMAC-SHA256 digest in constant time
        expected_signature = generate_hmac_signature(
            secret=game.api_secret,
            game_id=str(game.id),
            player_id=str(player_id),
            score=score,
            timestamp=timestamp,
            nonce=nonce
        )

        if not hmac.compare_digest(received_signature, expected_signature):
            raise PermissionDenied("Invalid HMAC signature. Request payload has been tampered with or secret is wrong.")

        return True


class PlayerScoreThrottle(SimpleRateThrottle):
    """
    Throttles score submissions on a per-player basis (identified by 'player' field in JSON).
    Falls back to client IP if player is missing.
    """
    scope = 'score_submission'

    def get_cache_key(self, request, view):
        if request.data and 'player' in request.data:
            ident = str(request.data['player'])
        else:
            ident = self.get_ident(request)

        return self.cache_format % {
            'scope': self.scope,
            'ident': ident
        }