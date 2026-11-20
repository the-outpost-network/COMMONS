"""Sign in with Matrix: the OAuth 2.0 client side (Matrix "next-gen auth").

COMMONS only needs to learn *who* signed in. Continuwuity always issues a token
with full account access (it does not enforce scopes), so the token is used for
exactly one ``whoami`` call and revoked right away. It is never stored or logged.

Browser-facing URLs (the login page) use the homeserver's public URL; the
server-to-server calls (register, token, whoami, revoke) go to its internal
address on the Docker network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from app.config import Settings

CLIENT_FILE = "matrix_oauth_client.json"
# Matrix user ids, see "User Identifiers" in the Matrix spec appendices.
USER_ID_RE = re.compile(r"^@[a-z0-9._=\-/+]{1,200}:[A-Za-z0-9.\-]{1,200}(:[0-9]{1,5})?$")


class MatrixAuthError(Exception):
    """Sign-in could not be completed. The message is for logs, not for users."""


@dataclass(frozen=True)
class PKCE:
    verifier: str
    challenge: str

    @classmethod
    def new(cls) -> PKCE:
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode()).digest()
        return cls(verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())


class MatrixOAuth:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.s = settings
        self.http = http
        self._metadata: dict | None = None
        self._client_id: str | None = None

    # -- helpers ----------------------------------------------------------

    def _internal(self, public_endpoint: str) -> str:
        """Map a public homeserver/auth-service endpoint to its internal address.

        Refuses endpoints on any other host, so a bad metadata document can't
        make the app send codes or tokens elsewhere.
        """
        ep = urlsplit(public_endpoint)
        for public, internal in (
            (self.s.matrix_auth_public_url, self.s.matrix_auth_internal_url),
            (self.s.matrix_public_url, self.s.matrix_internal_url),
        ):
            pub = urlsplit(public)
            if (ep.scheme, ep.netloc) == (pub.scheme, pub.netloc):
                query = f"?{ep.query}" if ep.query else ""
                return f"{internal}{ep.path}{query}"
        raise MatrixAuthError(f"endpoint not on the homeserver: {public_endpoint}")

    async def metadata(self) -> dict:
        if self._metadata is None:
            url = f"{self.s.matrix_internal_url}/_matrix/client/v1/auth_metadata"
            r = await self.http.get(url)
            if r.status_code != 200:
                raise MatrixAuthError(f"auth_metadata: HTTP {r.status_code}")
            meta = r.json()
            for key in (
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
                "revocation_endpoint",
            ):
                if not isinstance(meta.get(key), str):
                    raise MatrixAuthError(f"auth_metadata: missing {key}")
                self._internal(meta[key])  # host check
            if "S256" not in meta.get("code_challenge_methods_supported", []):
                raise MatrixAuthError("auth_metadata: S256 PKCE not supported")
            self._metadata = meta
        return self._metadata

    async def client_id(self) -> str:
        """Register COMMONS as an OAuth client once; reuse the stored id afterwards."""
        if self._client_id:
            return self._client_id
        path: Path = self.s.data_dir / CLIENT_FILE
        if path.exists():
            stored = json.loads(path.read_text())
            if stored.get("redirect_uri") == self.s.redirect_uri and stored.get("issuer") == (
                await self.metadata()
            ).get("issuer"):
                self._client_id = stored["client_id"]
                return self._client_id
        meta = await self.metadata()
        r = await self.http.post(
            self._internal(meta["registration_endpoint"]),
            json={
                "client_name": "COMMONS dashboard",
                "client_uri": f"{self.s.public_url}/",
                "redirect_uris": [self.s.redirect_uri],
                "application_type": "web",
                # Public client: PKCE instead of a client secret. "none" is the
                # OAuth method name (RFC 7591), not a password.
                "token_endpoint_auth_method": "none",  # nosec B105
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        if r.status_code not in (200, 201) or "client_id" not in r.json():
            raise MatrixAuthError(f"client registration: HTTP {r.status_code}")
        client_id = r.json()["client_id"]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(
                {
                    "client_id": client_id,
                    "redirect_uri": self.s.redirect_uri,
                    "issuer": meta.get("issuer"),
                },
                f,
            )
        self._client_id = client_id
        return client_id

    # -- the flow ---------------------------------------------------------
    #
    # Two ways to learn who signed in, picked from the server's metadata:
    # - OpenID userinfo (Synapse + MAS): scope "openid" only. The token has no
    #   Matrix API access and no device; MAS answers userinfo directly, so a
    #   revoked token is rejected immediately.
    # - whoami (Continuwuity, no userinfo endpoint): the server requires a
    #   device scope and issues a full-access token.

    @staticmethod
    def _uses_userinfo(meta: dict) -> bool:
        return isinstance(meta.get("userinfo_endpoint"), str) and "openid" in meta.get(
            "scopes_supported", []
        )

    async def scope(self) -> str:
        if self._uses_userinfo(await self.metadata()):
            return "openid"
        return f"urn:matrix:client:api:* urn:matrix:client:device:{self.s.matrix_device_id}"

    async def authorization_url(self, state: str, pkce: PKCE, signup: bool = False) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code",
            "response_mode": "query",
            "client_id": await self.client_id(),
            "redirect_uri": self.s.redirect_uri,
            "scope": await self.scope(),
            "state": state,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
        }
        # OpenID "prompt=create": start on MAS' sign-up page (invite links). Not
        # gated on Synapse's copy of MAS' metadata, which it caches until restart.
        if signup:
            params["prompt"] = "create"
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def _who(self, meta: dict, access: str) -> tuple[int, str | None]:
        """Ask who owns ``access``. Returns (HTTP status, Matrix user id or None)."""
        auth = {"Authorization": f"Bearer {access}"}
        if self._uses_userinfo(meta):
            r = await self.http.get(self._internal(meta["userinfo_endpoint"]), headers=auth)
            username = r.json().get("username") if r.status_code == 200 else None
            user_id = f"@{username}:{self.s.matrix_server_name}" if username else None
            return r.status_code, user_id
        r = await self.http.get(
            f"{self.s.matrix_internal_url}/_matrix/client/v3/account/whoami", headers=auth
        )
        return r.status_code, (r.json().get("user_id") if r.status_code == 200 else None)

    async def identify(self, code: str, verifier: str) -> str:
        """Exchange the code, learn the Matrix user id, revoke the tokens.

        Returns the verified Matrix user id. The tokens are revoked whatever
        happens after the exchange.
        """
        meta = await self.metadata()
        client_id = await self.client_id()
        r = await self.http.post(
            self._internal(meta["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.s.redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        if r.status_code != 200:
            raise MatrixAuthError(f"token exchange: HTTP {r.status_code}")
        tokens = r.json()
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        if not isinstance(access, str):
            raise MatrixAuthError("token exchange: no access token")
        try:
            status, user_id = await self._who(meta, access)
            if status != 200:
                raise MatrixAuthError(f"identity lookup: HTTP {status}")
        finally:
            await self._revoke(meta, client_id, access, refresh)
        if not isinstance(user_id, str) or not USER_ID_RE.match(user_id):
            raise MatrixAuthError("identity lookup: malformed user id")
        if user_id.split(":", 1)[1] != self.s.matrix_server_name:
            raise MatrixAuthError("identity lookup: user from another server")
        return user_id

    async def _revoke(self, meta: dict, client_id: str, access: str, refresh: str | None) -> None:
        """Revoke both tokens, then prove the access token is dead.

        Continuwuity ends the whole OAuth session when the access token is
        revoked, so the refresh token is usually gone by then and the server
        answers 400 (MAS answers 200 for unknown tokens); both are accepted.
        What counts is the final check: the identity lookup with the access
        token must fail.
        """
        url = self._internal(meta["revocation_endpoint"])

        async def revoke(token: str, hint: str) -> int:
            r = await self.http.post(
                url, data={"token": token, "token_type_hint": hint, "client_id": client_id}
            )
            return r.status_code

        problems = []
        try:
            status = await revoke(access, "access_token")
            if status != 200:
                problems.append(f"access_token: HTTP {status}")
            if refresh:
                status = await revoke(refresh, "refresh_token")
                if status not in (200, 400):  # 400: already revoked with the session
                    problems.append(f"refresh_token: HTTP {status}")
            status, _ = await self._who(meta, access)
            if status == 200:
                problems.append("access token still valid after revocation")
        except httpx.HTTPError as e:
            problems.append(type(e).__name__)
        if problems:
            # Fail the sign-in: never hand out a session while a token may still be live.
            raise MatrixAuthError("revocation failed: " + ", ".join(problems))
