"""Matrix profile access for COMMONS, through MAS (Synapse trial).

Matrix is the source of truth for a member's display name; COMMONS keeps a copy
in `member.display_name` so the name is the same in COMMONS and in Element.

- Reading a name uses the public profile endpoint (no token needed).
- Changing a name is done **as the member**, never with server-admin rights:
  1. COMMONS gets a short-lived MAS admin token (its own OAuth client,
     client-credentials grant, scope `urn:mas:admin`).
  2. With it, COMMONS opens a personal session that acts for that one member,
     with the Matrix client-API scope only and a 60-second lifetime.
  3. COMMONS sets the display name with that session (Synapse also updates it
     in every room), then revokes the session.

Accounts and invites use the MAS admin API with the same admin token:

- an invite is a MAS registration token (single use, with an expiry); MAS
  checks it on its own sign-up page, so passwords never pass through COMMONS;
- deactivating a member locks their MAS account (no sign-in, sessions stop
  working); reactivating unlocks it.

Tokens are kept in memory only and never logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from urllib.parse import quote

import httpx

from app.config import Settings

ADMIN_SCOPE = "urn:mas:admin"
MEMBER_SCOPE = "urn:matrix:client:api:*"
SESSION_SECONDS = 60


class MatrixAdminError(Exception):
    """MAS or Synapse refused or failed; the message never contains a token."""


class MatrixAdmin:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        client_secret: Callable[[], str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._http = http
        self._secret = client_secret
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    # --- helpers --------------------------------------------------------------

    async def _call(self, method: str, url: str, what: str, **kw) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kw)
        except httpx.HTTPError as e:
            raise MatrixAdminError(f"{what} failed: {type(e).__name__}") from e

    async def _admin_token(self) -> str:
        if self._token and self._clock() < self._expires:
            return self._token
        resp = await self._call(
            "POST",
            f"{self._s.matrix_auth_internal_url}/oauth2/token",
            "admin token request",
            data={"grant_type": "client_credentials", "scope": ADMIN_SCOPE},
            auth=(self._s.matrix_admin_client_id, self._secret()),
        )
        if resp.status_code != 200:
            raise MatrixAdminError(f"admin token refused: {resp.status_code}")
        body = resp.json()
        token = body.get("access_token")
        if not isinstance(token, str) or ADMIN_SCOPE not in str(body.get("scope", "")).split():
            raise MatrixAdminError("admin token without the admin scope")
        lifetime = body.get("expires_in", 300)
        lifetime = lifetime if isinstance(lifetime, int) else 300
        self._token = token
        self._expires = self._clock() + max(lifetime - 30, 0)
        return token

    def _localpart(self, user_id: str) -> str:
        localpart, _, server = user_id.removeprefix("@").partition(":")
        if not localpart or server != self._s.matrix_server_name:
            raise MatrixAdminError("not a user on this homeserver")
        return localpart

    async def _api(self, method: str, path: str, what: str, **kw) -> httpx.Response:
        admin = {"Authorization": f"Bearer {await self._admin_token()}"}
        return await self._call(
            method, f"{self._s.matrix_admin_api_url}/api/admin/v1{path}", what, headers=admin, **kw
        )

    async def _mas_user_id(self, user_id: str) -> str | None:
        """MAS' internal id for a Matrix user on this server; None if MAS has no such user."""
        localpart = self._localpart(user_id)
        resp = await self._api(
            "GET", f"/users/by-username/{quote(localpart, safe='')}", "user lookup"
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise MatrixAdminError(f"user lookup refused: {resp.status_code}")
        return resp.json()["data"]["id"]

    def _profile_url(self, user_id: str) -> str:
        return (
            f"{self._s.matrix_internal_url}/_matrix/client/v3/profile/"
            f"{quote(user_id, safe='')}/displayname"
        )

    # --- profile --------------------------------------------------------------

    async def get_display_name(self, user_id: str) -> str | None:
        self._localpart(user_id)
        resp = await self._call("GET", self._profile_url(user_id), "profile request")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise MatrixAdminError(f"profile request refused: {resp.status_code}")
        name = resp.json().get("displayname")
        return name if isinstance(name, str) and name else None

    async def set_display_name(self, user_id: str, name: str) -> None:
        """Set the name as the member (profile and every room they are in)."""
        mas_user_id = await self._mas_user_id(user_id)
        if mas_user_id is None:
            raise MatrixAdminError("user lookup refused: 404")
        resp = await self._api(
            "POST",
            "/personal-sessions",
            "session request",
            json={
                "actor_user_id": mas_user_id,
                "human_name": "COMMONS: change display name",
                "scope": MEMBER_SCOPE,
                "expires_in": SESSION_SECONDS,
            },
        )
        if resp.status_code not in (200, 201):
            raise MatrixAdminError(f"session request refused: {resp.status_code}")
        session = resp.json()["data"]
        session_id, member_token = session["id"], session["attributes"]["access_token"]
        try:
            resp = await self._call(
                "PUT",
                self._profile_url(user_id),
                "profile update",
                headers={"Authorization": f"Bearer {member_token}"},
                json={"displayname": name},
            )
            if resp.status_code != 200:
                raise MatrixAdminError(f"profile update refused: {resp.status_code}")
        finally:
            # Always end the session; it would also expire on its own after 60 s.
            await self._api("POST", f"/personal-sessions/{session_id}/revoke", "session revoke")

    # --- invites (registration tokens) ---------------------------------------

    async def create_registration_token(self, code: str, expires_at: datetime) -> str:
        """Create a single-use registration token; returns MAS' token id."""
        resp = await self._api(
            "POST",
            "/user-registration-tokens",
            "invite request",
            json={"token": code, "usage_limit": 1, "expires_at": expires_at.isoformat()},
        )
        if resp.status_code not in (200, 201):
            raise MatrixAdminError(f"invite request refused: {resp.status_code}")
        return resp.json()["data"]["id"]

    async def registration_token_usable(self, token_id: str) -> bool:
        """True while the token can still be used (not used up, expired or revoked)."""
        resp = await self._api(
            "GET", f"/user-registration-tokens/{quote(token_id)}", "invite lookup"
        )
        if resp.status_code == 404:
            return False
        if resp.status_code != 200:
            raise MatrixAdminError(f"invite lookup refused: {resp.status_code}")
        return resp.json()["data"]["attributes"].get("valid") is True

    async def revoke_registration_token(self, token_id: str) -> None:
        resp = await self._api(
            "POST", f"/user-registration-tokens/{quote(token_id)}/revoke", "invite revoke"
        )
        # 400: already revoked. 404: gone. Either way the token cannot be used.
        if resp.status_code not in (200, 400, 404):
            raise MatrixAdminError(f"invite revoke refused: {resp.status_code}")

    # --- account lock ---------------------------------------------------------

    async def set_locked(self, user_id: str, locked: bool) -> None:
        """Lock or unlock a member's Matrix account. No MAS account: nothing to do."""
        mas_user_id = await self._mas_user_id(user_id)
        if mas_user_id is None:
            return
        action = "lock" if locked else "unlock"
        resp = await self._api("POST", f"/users/{quote(mas_user_id)}/{action}", f"account {action}")
        if resp.status_code != 200:
            raise MatrixAdminError(f"account {action} refused: {resp.status_code}")

    async def deactivate_and_erase(self, user_id: str) -> None:
        """Permanently deactivate a Matrix account and ask Synapse to erase it.

        MAS has no "delete": this ends all sessions, removes the account from all
        rooms and erases its profile and messages. The username can never be used
        again (Matrix ids are not reused). No MAS account: nothing to do.
        """
        mas_user_id = await self._mas_user_id(user_id)
        if mas_user_id is None:
            return
        resp = await self._api(
            "POST",
            f"/users/{quote(mas_user_id)}/deactivate",
            "account deactivation",
            json={"skip_erase": False},
        )
        if resp.status_code != 200:
            raise MatrixAdminError(f"account deactivation refused: {resp.status_code}")
