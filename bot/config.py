"""Bot settings from COMMONS_BOT_* environment variables, secrets from files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SECRETS_DIR = Path(os.environ.get("COMMONS_SECRETS_DIR", "/run/secrets"))


def read_secret(name: str) -> str:
    """Read a Docker secret. Never log the value."""
    value = (SECRETS_DIR / name).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"secret {name} is empty")
    return value


@dataclass(frozen=True)
class BotSettings:
    homeserver_url: str = "http://matrix:8008"
    user_id: str = "@commons:localhost"
    device_id: str = "COMMONSBOT"
    # The COMMONS app's internal API (internal Docker network only).
    app_url: str = "http://app:8000"
    data_dir: Path = Path("/data")

    @property
    def server_name(self) -> str:
        return self.user_id.split(":", 1)[1]


def load_settings() -> BotSettings:
    env = os.environ
    defaults = BotSettings()
    return BotSettings(
        homeserver_url=env.get("COMMONS_BOT_HOMESERVER_URL", defaults.homeserver_url),
        user_id=env.get("COMMONS_BOT_USER_ID", defaults.user_id),
        device_id=env.get("COMMONS_BOT_DEVICE_ID", defaults.device_id),
        app_url=env.get("COMMONS_BOT_APP_URL", defaults.app_url),
        data_dir=Path(env.get("COMMONS_BOT_DATA_DIR", str(defaults.data_dir))),
    )
