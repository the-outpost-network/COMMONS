"""Runtime configuration.

Secrets come from files, never from environment values. Plain settings come from
environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Compose mounts each secret read-only at /run/secrets/<name> (created by `make init`).
SECRETS_DIR = Path(os.environ.get("COMMONS_SECRETS_DIR", "/run/secrets"))

# Secrets the app container needs; must match `services.app.secrets` in docker-compose.yml.
APP_SECRETS = ("internal_api_token", "llm_api_key", "database_password")
# Read only when the matching feature is configured (Synapse trial: Matrix admin client).
OPTIONAL_SECRETS = ("matrix_admin_client_secret",)


class SecretError(RuntimeError):
    """A required secret file is missing, unreadable or empty."""


def read_secret(name: str, secrets_dir: Path | None = None) -> str:
    """Return the stripped secret value. Error messages name the file, never the value."""
    if name not in APP_SECRETS + OPTIONAL_SECRETS:
        raise SecretError(f"unknown secret {name!r}")
    path = (secrets_dir or SECRETS_DIR) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretError(f"cannot read secret file {path} ({exc.strerror})") from None
    if not value:
        raise SecretError(f"secret file {path} is empty; run `make init`")
    return value


def check_secrets(secrets_dir: Path | None = None) -> None:
    """Fail fast at startup if any required secret is missing."""
    for name in APP_SECRETS:
        read_secret(name, secrets_dir)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # App state on a mounted volume (e.g. the OAuth client registration).
    data_dir: Path
    # PostgreSQL URL without the password, e.g. postgresql+psycopg://commons@postgres/commons.
    # The password comes from the database_password secret.
    database_url: str
    # Public URL of the dashboard, used for the OAuth redirect URI.
    public_url: str
    # Matrix homeserver: the public URL members' browsers are sent to, and the
    # internal URL the app uses for server-to-server calls on the Docker network.
    matrix_public_url: str
    matrix_internal_url: str
    # Where the OAuth endpoints live. Same as the homeserver for Continuwuity;
    # Synapse delegates them to the Matrix Authentication Service (auth.<domain>).
    matrix_auth_public_url: str
    matrix_auth_internal_url: str
    # Only accounts on this server may sign in, e.g. "commons.theoutpost.network".
    matrix_server_name: str
    # Fixed device id for dashboard sign-ins, so each member gets at most one
    # dashboard entry in their Matrix device list.
    matrix_device_id: str
    session_idle_days: int
    cookie_secure: bool
    # Requests per minute and client IP on /auth/login and /auth/callback.
    login_rate_per_minute: int
    # Local model server (llama.cpp llama-server) on the internal network.
    llm_url: str = "http://llm:8080"
    # COMMONS' OAuth client at MAS (admin API; keeps display names in step).
    # Empty = not configured (Continuwuity has no equivalent).
    matrix_admin_client_id: str = ""
    # MAS admin API (an internal-only listener).
    matrix_admin_api_url: str = "http://mas:8081"
    # Chat assistant limits: messages per minute and member, characters per message.
    assistant_rate_per_minute: int = 6
    # Taps on vote messages per member and minute.
    tap_rate_per_minute: int = 20
    assistant_max_chars: int = 2000
    # How long a sign-up invite (MAS registration token) stays valid.
    invite_hours: int = 24
    # community.yaml, read once to seed the database (app/community.py).
    # None = don't seed (tests).
    community_file: Path | None = None
    timezone: str = "Europe/Stockholm"
    # "production", or "demo" to run on the synthetic demo database (scripts/seed_demo.py).
    environment: str = "production"
    # Weather from SMHI open data (documented runtime exception). No coordinates = off.
    weather_latitude: float | None = None
    weather_longitude: float | None = None
    weather_place: str = "the Outpost"
    weather_refresh_minutes: int = 30
    # Outbound requests go through the allowlist egress proxy; empty = direct (tests).
    egress_proxy: str = ""
    # The bot's event stream (/internal/events) ends after this long; the bot
    # reconnects and resumes, so no connection lives forever.
    events_stream_seconds: float = 600.0
    # How often the app looks for things kept past their end (0: never, e.g. in tests).
    reminder_check_seconds: float = 60.0
    # How often the forecasts check for new logs (and rerun daily); 0 = off.
    forecast_check_seconds: float = 900.0
    # How often the app checks for the daily ledger digest and anchor (0: never, tests).
    anchor_check_seconds: float = 60.0

    @property
    def key_dir(self) -> Path:
        """Decision-log signing key (generated on first start, mode 0600)."""
        return self.data_dir / "keys"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"


def load_settings() -> Settings:
    data_dir = Path(_env("COMMONS_DATA_DIR", "/data"))
    public_url = _env("COMMONS_PUBLIC_URL", "https://localhost").rstrip("/")
    matrix_public = _env("COMMONS_MATRIX_PUBLIC_URL", "https://matrix.localhost").rstrip("/")
    matrix_internal = _env("COMMONS_MATRIX_INTERNAL_URL", "http://matrix:8008").rstrip("/")
    return Settings(
        data_dir=data_dir,
        database_url=_env(
            "COMMONS_DATABASE_URL", "postgresql+psycopg://commons@postgres:5432/commons"
        ),
        public_url=public_url,
        matrix_public_url=matrix_public,
        matrix_internal_url=matrix_internal,
        matrix_auth_public_url=_env("COMMONS_MATRIX_AUTH_PUBLIC_URL", matrix_public).rstrip("/"),
        matrix_auth_internal_url=_env("COMMONS_MATRIX_AUTH_INTERNAL_URL", matrix_internal).rstrip(
            "/"
        ),
        matrix_server_name=_env("COMMONS_MATRIX_SERVER_NAME", "localhost"),
        matrix_device_id=_env("COMMONS_MATRIX_DEVICE_ID", "COMMONS_DASHBOARD"),
        session_idle_days=int(_env("COMMONS_SESSION_IDLE_DAYS", "14")),
        cookie_secure=_env("COMMONS_COOKIE_SECURE", "true").lower() != "false",
        login_rate_per_minute=int(_env("COMMONS_LOGIN_RATE_PER_MINUTE", "10")),
        llm_url=_env("COMMONS_LLM_URL", "http://llm:8080").rstrip("/"),
        matrix_admin_client_id=_env("COMMONS_MATRIX_ADMIN_CLIENT_ID", ""),
        matrix_admin_api_url=_env("COMMONS_MATRIX_ADMIN_API_URL", "http://mas:8081").rstrip("/"),
        assistant_rate_per_minute=int(_env("COMMONS_ASSISTANT_RATE_PER_MINUTE", "6")),
        tap_rate_per_minute=int(_env("COMMONS_TAP_RATE_PER_MINUTE", "20")),
        invite_hours=int(_env("COMMONS_INVITE_HOURS", "24")),
        community_file=Path(_env("COMMONS_COMMUNITY_FILE", "/config/community.yaml")),
        environment=_env("COMMONS_ENV", "production").lower(),
        weather_latitude=_float_or_none(_env("COMMONS_WEATHER_LATITUDE", "")),
        weather_longitude=_float_or_none(_env("COMMONS_WEATHER_LONGITUDE", "")),
        weather_place=_env("COMMONS_WEATHER_PLACE", "the Outpost"),
        weather_refresh_minutes=int(_env("COMMONS_WEATHER_REFRESH_MINUTES", "30")),
        egress_proxy=_env("COMMONS_EGRESS_PROXY", ""),
        events_stream_seconds=float(_env("COMMONS_EVENTS_STREAM_SECONDS", "600")),
        reminder_check_seconds=float(_env("COMMONS_REMINDER_CHECK_SECONDS", "60")),
        forecast_check_seconds=float(_env("COMMONS_FORECAST_CHECK_SECONDS", "900")),
        anchor_check_seconds=float(_env("COMMONS_ANCHOR_CHECK_SECONDS", "60")),
    )


def _float_or_none(value: str) -> float | None:
    return float(value) if value else None
