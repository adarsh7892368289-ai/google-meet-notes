# tests/test_oauth_client.py
import httpx
import pytest

from app.google.oauth_client import GoogleOAuthClient, TokenBundle


def _client_with_handler(handler) -> GoogleOAuthClient:
    transport = httpx.MockTransport(handler)
    return GoogleOAuthClient(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="https://app/cb",
        scopes="openid email",
        transport=transport,
    )


def test_build_authorization_url_contains_required_params():
    client = _client_with_handler(lambda req: httpx.Response(200))
    url = client.build_authorization_url(state="xyz")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=xyz" in url
    assert "scope=openid+email" in url or "scope=openid%20email" in url


async def test_exchange_code_returns_token_bundle():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert b"grant_type=authorization_code" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3599,
                "scope": "openid email",
                "token_type": "Bearer",
            },
        )

    client = _client_with_handler(handler)
    bundle = await client.exchange_code("the-code")
    assert isinstance(bundle, TokenBundle)
    assert bundle.access_token == "at-1"
    assert bundle.refresh_token == "rt-1"
    assert bundle.expires_in == 3599


async def test_refresh_returns_token_bundle():
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            200,
            json={"access_token": "at-2", "expires_in": 3599, "scope": "openid email"},
        )

    client = _client_with_handler(handler)
    bundle = await client.refresh("rt-1")
    assert bundle.access_token == "at-2"
    assert bundle.refresh_token is None


async def test_fetch_userinfo_returns_email():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"email": "user@acme.com"})

    client = _client_with_handler(handler)
    email = await client.fetch_userinfo("at-1")
    assert email == "user@acme.com"
