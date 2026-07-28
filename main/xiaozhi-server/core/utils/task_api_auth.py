import re
import threading
import time
from typing import Any, Callable, Optional

from google.auth import jwt
from google.auth.transport.requests import Request
from google.oauth2.id_token import fetch_id_token


TASK_API_AUDIENCE = (
    "https://us-central1-composed-augury-469200-g6.cloudfunctions.net/"
    "tasks-api-miffy-dev"
)
TASK_API_CALLER_SERVICE_ACCOUNT = (
    "babymilu-production-server@composed-augury-469200-g6.iam.gserviceaccount.com"
)
TASK_API_AUTH_MODE = "google-oidc"
_DEFAULT_REFRESH_SKEW_SECONDS = 300.0
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"(?![A-Za-z0-9_-])"
)


class GoogleOidcTokenProvider:
    """Thread-safe Google identity-token cache for the exact task API."""

    def __init__(
        self,
        audience: str = TASK_API_AUDIENCE,
        *,
        refresh_skew_seconds: float = _DEFAULT_REFRESH_SKEW_SECONDS,
        fetcher: Optional[Callable[[Any, str], str]] = None,
        request_factory: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        if audience != TASK_API_AUDIENCE:
            raise ValueError("task API OIDC audience must match the miffy-dev endpoint")
        self.audience = audience
        self.refresh_skew_seconds = max(0.0, float(refresh_skew_seconds))
        self._fetcher = fetcher or fetch_id_token
        self._request_factory = request_factory or Request
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def get_token(self) -> str:
        now = self._clock()
        with self._lock:
            if self._token and self._expires_at - self.refresh_skew_seconds > now:
                return self._token

            token = self._fetcher(self._request_factory(), self.audience)
            expires_at = _validated_token_expiration(token, audience=self.audience)
            if expires_at <= now:
                raise RuntimeError("Google OIDC identity token is already expired")
            self._token = token
            self._expires_at = expires_at
            return token


def _validated_token_expiration(token: str, audience: str) -> float:
    if not isinstance(token, str) or not token:
        raise RuntimeError("Google OIDC identity token fetch returned no token")
    claims = jwt.decode(token, verify=False)
    if claims.get("aud") != audience:
        raise RuntimeError("Google OIDC identity token has the wrong audience")
    if claims.get("email") != TASK_API_CALLER_SERVICE_ACCOUNT:
        raise RuntimeError("Google OIDC identity token has the wrong caller identity")
    if claims.get("email_verified") is not True:
        raise RuntimeError("Google OIDC identity token caller email is not verified")
    try:
        return float(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Google OIDC identity token is missing a valid expiry") from exc


def redact_auth_secrets(value: Any, limit: int = 300) -> str:
    text = str(value or "")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED_ID_TOKEN]", text)
    return text[: max(0, limit)]
