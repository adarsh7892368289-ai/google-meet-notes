# app/google/oauth_client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"


@dataclass
class TokenBundle:
    access_token: str
    expires_in: int
    scope: str
    refresh_token: str | None = None


@dataclass
class UserInfo:
    email: str
    sub: str


class OAuthClient(Protocol):
    def build_authorization_url(self, state: str) -> str: ...
    async def exchange_code(self, code: str) -> TokenBundle: ...
    async def refresh(self, refresh_token: str) -> TokenBundle: ...
    async def fetch_userinfo(self, access_token: str) -> UserInfo: ...
    async def revoke(self, token: str) -> None: ...


class GoogleOAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = scopes
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=30.0)

    def build_authorization_url(self, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": self._scopes,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URI}?{urlencode(params)}"

    @staticmethod
    def _to_bundle(data: dict) -> TokenBundle:
        return TokenBundle(
            access_token=data["access_token"],
            expires_in=int(data.get("expires_in", 0)),
            scope=data.get("scope", ""),
            refresh_token=data.get("refresh_token"),
        )

    async def exchange_code(self, code: str) -> TokenBundle:
        form = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
        }
        async with self._http() as http:
            resp = await http.post(TOKEN_URI, data=form)
            resp.raise_for_status()
            return self._to_bundle(resp.json())

    async def refresh(self, refresh_token: str) -> TokenBundle:
        form = {
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
        }
        async with self._http() as http:
            resp = await http.post(TOKEN_URI, data=form)
            resp.raise_for_status()
            return self._to_bundle(resp.json())

    async def fetch_userinfo(self, access_token: str) -> UserInfo:
        async with self._http() as http:
            resp = await http.get(
                USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            data = resp.json()
            return UserInfo(email=data["email"], sub=data["sub"])

    async def revoke(self, token: str) -> None:
        async with self._http() as http:
            await http.post(REVOKE_URI, data={"token": token})
