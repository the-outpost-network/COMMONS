"""Shared fixtures: a fake Matrix homeserver and an app wired to it."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app import decision_log, members
from app.config import Settings
from app.db import make_engine, make_sessionmaker, upgrade_database
from app.llm import LLMError
from app.main import create_app

# A throwaway PostgreSQL: `make test-db` locally, a service container in CI.
TEST_DATABASE_URL = os.environ.get(
    "COMMONS_TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:commons-test@127.0.0.1:55432/postgres",
)
PUBLIC = "https://matrix.example.test"
INTERNAL = "http://matrix:8008"
AUTH_PUBLIC = "https://auth.example.test"  # MAS, Synapse mode
AUTH_INTERNAL = "http://mas:8080"
ADMIN_API = "http://mas:8081"  # MAS admin API, internal listener
SERVER = "example.test"
INTERNAL_TOKEN = "internal-test-token"
ADMIN_CLIENT_ID = "01KCMMNS0APP0000000000000C"
ADMIN_SECRET = "admin-client-test-secret"

# Voting rules for tests, shaped like community.yaml's voting: section (no quorum,
# more than 50 % wins). "rules" runs 3 days with live counts; "events" hides the
# counts until the close; "spending" asks for an amount.
VOTING = {
    "quorum_pct": None,
    "threshold_pct": 50,
    "extra_options": 2,
    "currency": "SEK",
    "types": [
        {"slug": "spending", "name": "Spending", "duration_days": 1, "results": "live",
         "amount": True},
        {"slug": "rules", "name": "Rules", "duration_days": 3, "results": "live"},
        {"slug": "events", "name": "Events", "duration_days": 2, "results": "after_close"},
    ],
}  # fmt: skip


@dataclass
class FakeMatrix:
    """Just enough of Continuwuity's OAuth endpoints for the sign-in flow."""

    user_id: str = f"@nora:{SERVER}"
    registrations: int = 0
    issued: dict[str, str] = field(default_factory=dict)  # code -> verifier
    live_tokens: set[str] = field(default_factory=set)
    revoked: list[str] = field(default_factory=list)
    exchanges: int = 0
    fail_whoami: bool = False
    fail_revoke: bool = False
    ignore_revoke: bool = False  # answers 200 but keeps the token alive
    fail_token: bool = False
    endpoint_host: str = PUBLIC
    counter: int = 0
    # Synapse + MAS mode: OAuth on MAS with a userinfo endpoint; Synapse caches
    # token checks, so whoami keeps accepting a token for a while after revocation.
    mas: bool = False
    whoami_calls: int = 0
    scopes: dict[str, str] = field(default_factory=dict)  # access token -> scope
    last_scope: str = ""
    synapse_cache: set[str] = field(default_factory=set)
    # COMMONS' admin client at MAS (client credentials), personal sessions that act
    # for one member, and Synapse profiles.
    profiles: dict[str, str] = field(default_factory=lambda: {f"@nora:{SERVER}": "Nora"})
    admin_tokens: set[str] = field(default_factory=set)
    admin_token_requests: int = 0
    member_sessions: dict[str, dict] = field(default_factory=dict)  # token -> session
    revoked_sessions: list[str] = field(default_factory=list)
    profile_updates: list[tuple[str, str]] = field(default_factory=list)
    fail_profile: bool = False
    # MAS registration tokens (invites) and account locks.
    reg_tokens: dict[str, dict] = field(default_factory=dict)  # id -> token
    locked: set[str] = field(default_factory=set)  # MAS user ids
    erased: list[str] = field(default_factory=list)  # MAS user ids deactivated with erase
    unknown_users: set[str] = field(default_factory=set)  # usernames MAS answers 404 for
    fail_admin_api: bool = False

    def metadata(self) -> dict:
        if self.mas:
            h = AUTH_PUBLIC
            return {
                "issuer": f"{h}/",
                "authorization_endpoint": f"{h}/authorize",
                "token_endpoint": f"{h}/oauth2/token",
                "registration_endpoint": f"{h}/oauth2/registration",
                "revocation_endpoint": f"{h}/oauth2/revoke",
                "userinfo_endpoint": f"{h}/oauth2/userinfo",
                "scopes_supported": ["openid", "email"],
                "code_challenge_methods_supported": ["S256"],
                "prompt_values_supported": ["login", "create"],
            }
        h = self.endpoint_host
        return {
            "issuer": f"{PUBLIC}/",
            "authorization_endpoint": f"{h}/_continuwuity/oauth2/grant/authorization_code",
            "token_endpoint": f"{h}/_continuwuity/oauth2/grant/token",
            "registration_endpoint": f"{h}/_continuwuity/oauth2/client/register",
            "revocation_endpoint": f"{h}/_continuwuity/oauth2/client/revoke",
            "code_challenge_methods_supported": ["S256"],
            "prompt_values_supported": ["login", "create"],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert url.startswith((INTERNAL, AUTH_INTERNAL, ADMIN_API)), f"call left the network: {url}"
        path = request.url.path
        if url.startswith(ADMIN_API):
            return self.admin_api(request, path)
        if path == "/_matrix/client/v1/auth_metadata":
            return httpx.Response(200, json=self.metadata())
        if path.endswith(("/client/register", "/oauth2/registration")):
            self.registrations += 1
            body = json.loads(request.content)
            return httpx.Response(200, json={"client_id": "client-1", **body})
        if path.endswith(("/grant/token", "/oauth2/token")):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("grant_type") == "client_credentials":
                return self.admin_token(request, form)
            self.exchanges += 1
            if self.fail_token or self.issued.pop(form.get("code"), None) is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.counter += 1
            access, refresh = f"access-secret-{self.counter}", f"refresh-secret-{self.counter}"
            self.live_tokens |= {access, refresh}
            self.synapse_cache.add(access)
            self.scopes[access] = self.last_scope
            return httpx.Response(
                200,
                json={"access_token": access, "refresh_token": refresh, "token_type": "Bearer"},
            )
        if path == "/oauth2/userinfo":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if self.fail_whoami or token not in self.live_tokens:
                return httpx.Response(401, json={"error": "invalid_token"})
            if self.scopes.get(token) != "openid":
                return httpx.Response(403, json={"error": "insufficient_scope"})
            return httpx.Response(
                200, json={"sub": "01ULID", "username": self.user_id[1:].split(":")[0]}
            )
        if path.startswith("/_matrix/client/v3/profile/") and path.endswith("/displayname"):
            return self.profile(request, path)
        if path == "/_matrix/client/v3/account/whoami":
            self.whoami_calls += 1
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            cached = self.mas and token in self.synapse_cache
            if self.fail_whoami or (token not in self.live_tokens and not cached):
                return httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN"})
            return httpx.Response(200, json={"user_id": self.user_id, "device_id": "X"})
        if path.endswith(("/client/revoke", "/oauth2/revoke")):
            if self.fail_revoke:
                return httpx.Response(500)
            token = parse_qs(request.content.decode())["token"][0]
            if token not in self.live_tokens:
                # MAS answers 200 for unknown tokens (RFC 7009), Continuwuity 400.
                return httpx.Response(200 if self.mas else 400, json={"error": "invalid_token"})
            if self.ignore_revoke:
                return httpx.Response(200)
            # Like Continuwuity: revoking the access token ends the whole session,
            # so the matching refresh token is gone too.
            n = token.rsplit("-", 1)[1]
            gone = {f"access-secret-{n}", f"refresh-secret-{n}"} & self.live_tokens
            self.live_tokens -= gone
            self.revoked.extend(sorted(gone))
            return httpx.Response(200)
        return httpx.Response(404)

    def admin_token(self, request: httpx.Request, form: dict[str, str]) -> httpx.Response:
        expected = httpx.BasicAuth(ADMIN_CLIENT_ID, ADMIN_SECRET)._auth_header
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form.get("scope") != "urn:mas:admin":
            return httpx.Response(403, json={"error": "invalid_scope"})
        self.admin_token_requests += 1
        token = f"admin-secret-{self.admin_token_requests}"
        self.admin_tokens.add(token)
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": "urn:mas:admin",
            },
        )

    def admin_api(self, request: httpx.Request, path: str) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.admin_tokens:
            return httpx.Response(401, json={"errors": [{"title": "unauthorized"}]})
        if self.fail_admin_api:
            return httpx.Response(500, json={"errors": [{"title": "boom"}]})
        if path.startswith("/api/admin/v1/users/by-username/"):
            name = path.rsplit("/", 1)[1]
            if name in self.unknown_users:
                return httpx.Response(404, json={"errors": [{"title": "not found"}]})
            return httpx.Response(200, json={"data": {"type": "user", "id": f"ULID-{name}"}})
        if path.startswith("/api/admin/v1/users/") and path.endswith("/deactivate"):
            if json.loads(request.content) != {"skip_erase": False}:
                return httpx.Response(400)
            self.erased.append(path.split("/")[-2])
            return httpx.Response(200, json={"data": {"id": path.split("/")[-2]}})
        if path.startswith("/api/admin/v1/users/") and path.endswith(("/lock", "/unlock")):
            user = path.split("/")[-2]
            (self.locked.add if path.endswith("/lock") else self.locked.discard)(user)
            return httpx.Response(200, json={"data": {"id": user}})
        if path == "/api/admin/v1/user-registration-tokens" and request.method == "POST":
            body = json.loads(request.content)
            token_id = f"RT-{len(self.reg_tokens) + 1}"
            self.reg_tokens[token_id] = {**body, "times_used": 0, "revoked": False}
            return httpx.Response(201, json={"data": {"id": token_id, "attributes": body}})
        if path.startswith("/api/admin/v1/user-registration-tokens/"):
            token_id = path.split("/")[5]
            tok = self.reg_tokens.get(token_id)
            if tok is None:
                return httpx.Response(404, json={"errors": [{"title": "not found"}]})
            if path.endswith("/revoke"):
                if tok["revoked"]:
                    return httpx.Response(400, json={"errors": [{"title": "already revoked"}]})
                tok["revoked"] = True
                return httpx.Response(200, json={"data": {"id": token_id}})
            valid = not tok["revoked"] and tok["times_used"] < tok["usage_limit"]
            return httpx.Response(
                200, json={"data": {"id": token_id, "attributes": {"valid": valid}}}
            )
        if path == "/api/admin/v1/personal-sessions" and request.method == "POST":
            body = json.loads(request.content)
            n = len(self.member_sessions) + 1
            session = {"id": f"PS-{n}", "user": body["actor_user_id"], **body}
            token = f"member-secret-{n}"
            self.member_sessions[token] = session
            return httpx.Response(
                201,
                json={"data": {"id": session["id"], "attributes": {"access_token": token}}},
            )
        if path.endswith("/revoke"):
            self.revoked_sessions.append(path.split("/")[-2])
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(404)

    def profile(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.fail_profile:
            return httpx.Response(500, json={"errcode": "M_UNKNOWN"})
        # The raw path keeps the user id percent-encoded.
        raw = request.url.raw_path.decode().split("?")[0]
        user_id = unquote(raw.removeprefix("/_matrix/client/v3/profile/").split("/")[0])
        if request.method == "PUT":
            # Only the member themself may change their name (a personal session for them).
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            session = self.member_sessions.get(token)
            localpart = user_id[1:].split(":")[0]
            if (
                session is None
                or session["id"] in self.revoked_sessions
                or session["user"] != f"ULID-{localpart}"
                or session["scope"] != "urn:matrix:client:api:*"
            ):
                return httpx.Response(403, json={"errcode": "M_FORBIDDEN"})
            name = json.loads(request.content)["displayname"]
            self.profiles[user_id] = name
            self.profile_updates.append((user_id, name))
            return httpx.Response(200, json={})
        if user_id not in self.profiles:
            return httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        return httpx.Response(200, json={"displayname": self.profiles[user_id]})

    def grant(self, verifier: str = "unused") -> str:
        """Simulate the member logging in on the Matrix page: issue a code."""
        self.counter += 1
        code = f"code-{self.counter}"
        self.issued[code] = verifier
        return code


@pytest.fixture
def fake_matrix() -> FakeMatrix:
    return FakeMatrix()


@pytest.fixture(scope="session")
def pg_engine():
    engine = make_engine(TEST_DATABASE_URL)
    try:
        engine.connect().close()
    except Exception as e:
        pytest.fail(f"test PostgreSQL not reachable ({type(e).__name__}); run `make test-db`")
    yield engine
    engine.dispose()


# --- the per-test database reset -------------------------------------------------
#
# Running every migration before every test made the suite slow: about 200 ms
# a test, most of the run. So the schema is migrated once, and between tests only
# the rows go, with one TRUNCATE (about 10 ms). Tests that change the schema itself
# (downgrades, dropped tables, disabled triggers) need nothing special: an event
# trigger in the throwaway test database notes every DDL command, and the next
# reset then rebuilds the schema from scratch, as before. The migration version is
# compared too, for an `alembic stamp` (which changes rows, not the schema).

_WATCH_DDL = """
CREATE SCHEMA IF NOT EXISTS commons_test;
CREATE TABLE IF NOT EXISTS commons_test.schema_changed (changed boolean NOT NULL);
INSERT INTO commons_test.schema_changed
  SELECT true WHERE NOT EXISTS (SELECT 1 FROM commons_test.schema_changed);
CREATE OR REPLACE FUNCTION commons_test.note_ddl() RETURNS event_trigger
  LANGUAGE plpgsql AS $$ BEGIN UPDATE commons_test.schema_changed SET changed = true; END $$;
DROP EVENT TRIGGER IF EXISTS commons_test_ddl_end;
DROP EVENT TRIGGER IF EXISTS commons_test_ddl_drop;
CREATE EVENT TRIGGER commons_test_ddl_end ON ddl_command_end
  EXECUTE FUNCTION commons_test.note_ddl();
CREATE EVENT TRIGGER commons_test_ddl_drop ON sql_drop
  EXECUTE FUNCTION commons_test.note_ddl();
"""
_STATE = (
    "SELECT (SELECT changed FROM commons_test.schema_changed),"
    " (SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version)"
)


@dataclass
class _Migrated:
    versions: str  # alembic_version right after migrating
    truncate: str  # one TRUNCATE for every table that starts out empty


_migrated: dict[str, _Migrated] = {}


def _rebuild(engine) -> _Migrated:
    """Recreate the schema and apply every migration: the slow, exact path."""
    with engine.begin() as connection:
        connection.exec_driver_sql(_WATCH_DDL)
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    upgrade_database(engine)
    with engine.begin() as connection:
        tables = [
            r[0]
            for r in connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1"
            )
        ]
        # Tables with rows right after migrating (alembic_version, migration state)
        # belong to the migrations: kept, never truncated.
        emptied = [
            t
            for t in tables
            if not connection.exec_driver_sql(f'SELECT EXISTS (SELECT 1 FROM "{t}")').scalar()  # noqa: S608  # nosec B608
        ]
        connection.exec_driver_sql("UPDATE commons_test.schema_changed SET changed = false")
        state = _Migrated(
            connection.exec_driver_sql(_STATE).one()[1],
            "TRUNCATE " + ", ".join(f'"{t}"' for t in emptied) + " RESTART IDENTITY CASCADE",
        )
    _migrated[str(engine.url)] = state
    return state


def reset_database(engine, *, rebuild: bool = False) -> None:
    """An empty, fully migrated schema: truncate if it is unchanged, else rebuild."""
    state = _migrated.get(str(engine.url))
    if state is None or rebuild:
        _rebuild(engine)
        return
    try:
        with engine.connect() as connection:
            changed, versions = connection.exec_driver_sql(_STATE).one()
        unchanged = not changed and versions == state.versions
    except Exception:  # e.g. alembic_version was dropped
        unchanged = False
    if not unchanged:
        _rebuild(engine)
        return
    with engine.begin() as connection:
        # Test code only: bypass the append-only TRUNCATE triggers (decision_log,
        # booking, resource_entry) for this one reset. The test database runs as
        # a superuser; the app's own role could never do this.
        connection.exec_driver_sql("SET LOCAL session_replication_role = replica")
        connection.exec_driver_sql(state.truncate)


@pytest.fixture
def clean_db(pg_engine):
    """An empty, fully migrated test schema (see `reset_database`)."""
    reset_database(pg_engine)
    return pg_engine


@pytest.fixture
def log_signer(clean_db, tmp_path: Path) -> decision_log.Signer:
    """The decision-log signer, with its key where the app looks (settings.key_dir).

    The app loads the same key file when its lifespan starts, so entries written
    by a test and by a request chain and verify together.
    """
    with make_sessionmaker(clean_db)() as db, db.begin():
        return decision_log.init_signer(db, tmp_path / "keys")


@pytest.fixture
def settings(tmp_path: Path, clean_db) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=TEST_DATABASE_URL,
        public_url="https://commons.example.test",
        matrix_public_url=PUBLIC,
        matrix_internal_url=INTERNAL,
        matrix_auth_public_url=PUBLIC,
        matrix_auth_internal_url=INTERNAL,
        matrix_server_name=SERVER,
        matrix_device_id="COMMONS_DASHBOARD",
        session_idle_days=14,
        cookie_secure=True,
        login_rate_per_minute=100,
        matrix_admin_client_id=ADMIN_CLIENT_ID,
        matrix_admin_api_url=ADMIN_API,
        events_stream_seconds=0.2,  # streams end quickly, so a plain GET returns
        reminder_check_seconds=0,  # tests call app.reminders themselves
        forecast_check_seconds=0,  # tests call app.forecast themselves
        anchor_check_seconds=0,  # tests call app.anchors themselves
    )


@pytest.fixture
def app(settings: Settings, fake_matrix: FakeMatrix, fake_llm: FakeLLM):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_matrix.handler))
    app = create_app(
        settings,
        http=http,
        require_secrets=False,
        llm=fake_llm,
        internal_token=INTERNAL_TOKEN,
        admin_client_secret=ADMIN_SECRET,
    )
    yield app
    # Each app has its own connection pool; close it, or the test server runs out.
    app.state.engine.dispose()


class FakeLLM:
    """Stands in for llama-server: returns queued intent objects (or raises)."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.replies: list[dict | Exception] = []

    def queue(
        self,
        intent: str,
        display_name: str | None = None,
        reply: str | None = None,
        about: str = "me",
    ):
        self.replies.append(
            {"intent": intent, "about": about, "display_name": display_name, "reply": reply}
        )

    async def complete_json(self, messages: list[dict[str, str]], schema: dict) -> dict:
        self.calls.append(messages)
        # Nothing queued: the model server is down, so the rules answer.
        reply = self.replies.pop(0) if self.replies else LLMError("no reply queued")
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://commons.example.test") as c:
        yield c


@pytest.fixture
def db(app):
    with app.state.sessionmaker() as session:
        yield session


@pytest.fixture
def nora(db):
    m = members.add_member(db, f"@nora:{SERVER}", "Nora")
    db.commit()
    return m


def sign_in(client: TestClient, fake: FakeMatrix) -> httpx.Response:
    """Run the whole browser flow: /auth/login -> Matrix -> /auth/callback."""
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 303, r.text
    query = parse_qs(httpx.URL(r.headers["location"]).query.decode())
    state = query["state"][0]
    fake.last_scope = query["scope"][0]
    return client.get(
        "/auth/callback", params={"code": fake.grant(), "state": state}, follow_redirects=False
    )
