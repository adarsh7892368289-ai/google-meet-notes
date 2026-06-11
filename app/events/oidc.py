from __future__ import annotations

# SECURITY: GooglePushVerifier MUST be configured with a non-empty
# expected_audience and expected_email before production use. With either left
# empty, that check is skipped — an empty expected_audience disables `aud`
# validation entirely (google-auth does not check aud when audience=None), so
# any Google-signed OIDC token would be accepted. The Phase 4 defaults are
# empty for local dev; Phase 7 deployment hardening must set PUSH_AUDIENCE and
# PUSH_SERVICE_ACCOUNT_EMAIL and fail fast if they are missing in production.
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

_VALID_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class PushVerificationError(Exception):
    pass


@dataclass
class VerifiedPush:
    email: str | None


class PushVerifier(Protocol):
    def verify(self, authorization_header: str | None) -> VerifiedPush: ...


def _default_verify_fn(token: str, request, audience=None):
    from google.auth.transport import requests as ga_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, ga_requests.Request(), audience=audience)


class GooglePushVerifier:
    def __init__(
        self,
        *,
        expected_audience: str,
        expected_email: str,
        _verify_fn: Callable[..., dict] | None = None,
    ) -> None:
        self._expected_audience = expected_audience
        self._expected_email = expected_email
        self._verify_fn = _verify_fn or _default_verify_fn

    def verify(self, authorization_header: str | None) -> VerifiedPush:
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise PushVerificationError("missing bearer token")
        token = authorization_header[len("Bearer "):].strip()

        try:
            claims = self._verify_fn(
                token, None, audience=self._expected_audience or None
            )
        except Exception as exc:
            raise PushVerificationError(f"token verification failed: {exc}") from exc

        if claims.get("iss") not in _VALID_ISSUERS:
            raise PushVerificationError("invalid issuer")
        if claims.get("email_verified") is not True:
            raise PushVerificationError("email not verified")

        email = claims.get("email")
        if self._expected_email and email != self._expected_email:
            raise PushVerificationError("unexpected service account email")

        return VerifiedPush(email=email)
