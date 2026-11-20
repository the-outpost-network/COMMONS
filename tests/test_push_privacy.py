"""Push notifications carry only event ids."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location(
        "check_pushers", ROOT / "scripts/check_pushers.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it loads
    spec.loader.exec_module(module)
    return module


def test_synapse_never_puts_message_text_in_a_push() -> None:
    config = yaml.safe_load((ROOT / "deploy/synapse/homeserver.yaml").read_text())
    assert config["push"]["include_content"] is False


def row(*, fmt="event_id_only", kind="http", enabled="t", user="@nora:example.org") -> str:
    url = "https://matrix.org/_matrix/push/v1/notify"
    return "\t".join(
        [user, "io.element.elementx.ios.prod", kind, fmt, url, enabled, "1763251200000"]
    )


def test_parse_reads_psql_rows_without_push_keys() -> None:
    check = _script()
    assert "pushkey" not in check.SQL
    [p] = check.parse(row() + "\n\n")
    assert (p.user, p.format, p.gateway, p.enabled, p.since) == (
        "@nora:example.org", "event_id_only", "matrix.org", True, "2025-11-16",
    )  # fmt: skip


def test_only_enabled_http_pushers_without_event_id_only_are_problems() -> None:
    check = _script()
    pushers = check.parse(
        "\n".join([
            row(),
            row(fmt="", user="@ana:example.org"),  # Element without the format
            row(fmt="full", enabled="f"),  # disabled: Synapse sends nothing
            row(fmt="", kind="email"),  # no mail server configured
        ])
    )  # fmt: skip
    assert [p.user for p in check.problems(pushers)] == ["@ana:example.org"]
