"""`make init`, Compose secrets wiring and the app's secret reader."""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.config import APP_SECRETS, OPTIONAL_SECRETS, SecretError, check_secrets, read_secret

ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    re.search(r"^SECRET_NAMES\s*:=\s*(.+)$", (ROOT / "Makefile").read_text(), re.MULTILINE)
    .group(1)
    .split()
)


MAKE = shutil.which("make")
GIT = shutil.which("git")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def init_dir(tmp_path: Path) -> Path:
    """A minimal checkout: the real Makefile plus placeholder example files."""
    if not MAKE or not shutil.which("openssl"):
        pytest.skip("make and openssl are required")
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "community.yaml.example").write_text("community: {name: Test}\n")
    (tmp_path / ".env.example").write_text("COMMONS_HOST=localhost\n")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("synapse_trial_init.py", "commons_admin_client.py"):
        shutil.copy(ROOT / "scripts" / name, scripts / name)
    deploy = tmp_path / "deploy" / "synapse"
    deploy.mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "synapse" / "mas.yaml", deploy / "mas.yaml")
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_docker = fakebin / "docker"
    fake_docker.write_text("#!/bin/sh\nprintf 'secrets: {}\\n'\n")
    fake_docker.chmod(0o755)
    (tmp_path / "fakebin-path").write_text(str(fakebin))
    return tmp_path


def _make_init(cwd: Path) -> str:
    # Fixed argv, no shell, no untrusted input.
    fakebin = (cwd / "fakebin-path").read_text()
    result = subprocess.run(  # noqa: S603
        [MAKE, f"PYTHON={sys.executable}", "init"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": f"{fakebin}:{os.environ['PATH']}"},
    )
    return result.stdout


def test_make_init_creates_private_random_secrets(init_dir: Path) -> None:
    _make_init(init_dir)
    secrets = init_dir / "secrets"

    assert _mode(secrets) == 0o700
    values = []
    for name in SECRET_NAMES:
        path = secrets / name
        assert _mode(path) == 0o600, name
        value = path.read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", value), name
        values.append(value)
    assert len(set(values)) == len(values), "secrets must be independent"
    assert not list(secrets.glob("*.tmp"))


def test_make_init_never_overwrites(init_dir: Path) -> None:
    (init_dir / "community.yaml").write_text("edited: true\n")
    (init_dir / ".env").write_text("COMMONS_HOST=box.lan\n")
    _make_init(init_dir)
    before = {n: (init_dir / "secrets" / n).read_text() for n in SECRET_NAMES}

    out = _make_init(init_dir)

    after = {n: (init_dir / "secrets" / n).read_text() for n in SECRET_NAMES}
    assert after == before
    assert (init_dir / "community.yaml").read_text() == "edited: true\n"
    assert (init_dir / ".env").read_text() == "COMMONS_HOST=box.lan\n"
    assert "created" not in out


def test_make_init_copies_examples_when_missing(init_dir: Path) -> None:
    _make_init(init_dir)

    assert (init_dir / "community.yaml").read_text() == "community: {name: Test}\n"
    assert (init_dir / ".env").read_text() == "COMMONS_HOST=localhost\n"


def test_make_init_creates_matrix_bootstrap_files(init_dir: Path) -> None:
    _make_init(init_dir)
    matrix_secrets = init_dir / "secrets" / "matrix"

    for name in (
        "postgres_password",
        "synapse.yaml",
        "signing.key",
        "mas.yaml",
        "mas-commons-client.yaml",
        "matrix_admin_client_secret",
    ):
        assert (matrix_secrets / name).is_file()
        assert _mode(matrix_secrets / name) == 0o600


def test_make_init_migrates_existing_matrix_secrets(init_dir: Path) -> None:
    old_dir = init_dir / "secrets" / "synapse-trial"
    old_dir.mkdir(parents=True)
    names = (
        "postgres_password",
        "synapse.yaml",
        "signing.key",
        "mas.yaml",
        "mas-commons-client.yaml",
        "matrix_admin_client_secret",
    )
    for name in names:
        (old_dir / name).write_text(f"existing-{name}\n")

    _make_init(init_dir)

    new_dir = init_dir / "secrets" / "matrix"
    assert not old_dir.exists()
    for name in names:
        assert (new_dir / name).read_text() == f"existing-{name}\n"


def test_make_init_output_never_contains_secret_values(init_dir: Path) -> None:
    out = _make_init(init_dir)

    for name in SECRET_NAMES:
        assert (init_dir / "secrets" / name).read_text().strip() not in out


def test_make_init_repairs_loose_permissions(init_dir: Path) -> None:
    _make_init(init_dir)
    path = init_dir / "secrets" / SECRET_NAMES[0]
    path.chmod(0o644)

    _make_init(init_dir)

    assert _mode(path) == 0o600


def test_compose_mounts_secrets_as_files_not_environment() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())

    declared = compose["secrets"]
    # Every `make init` secret is declared at ./secrets/<name> ...
    for name in SECRET_NAMES:
        assert declared[name] == {"file": f"./secrets/{name}"}
    # ... and every other secret file also lives in the git-ignored secrets/ folder.
    for name, spec in declared.items():
        assert spec["file"].startswith(("./secrets/", "${DESEC_TOKEN_FILE:-./secrets/")), name

    app_secrets = tuple(compose["services"]["app"]["secrets"])
    assert app_secrets == APP_SECRETS + OPTIONAL_SECRETS
    secretish = re.compile(r"SECRET|TOKEN|PASSWORD|API_KEY", re.IGNORECASE)
    for service_name, service in compose["services"].items():
        env = service.get("environment") or {}
        keys = env if isinstance(env, dict) else [e.split("=", 1)[0] for e in env]
        # A *_FILE variable pointing at a mounted secret (e.g. POSTGRES_PASSWORD_FILE) is fine.
        leaked = [
            k
            for k in keys
            if secretish.search(k)
            and not (k.endswith("_FILE") and str(env[k]).startswith("/run/secrets/"))
        ]
        assert not leaked, f"{service_name} passes secrets via environment: {leaked}"


def test_env_example_holds_no_secret_values() -> None:
    """Report key names only, so a failure never prints a value."""
    secretish = re.compile(r"SECRET|TOKEN|PASSWORD|PASSPHRASE|API_KEY|PRIVATE", re.IGNORECASE)
    random_looking = re.compile(r"^(?:[0-9a-fA-F]{32,}|[A-Za-z0-9+/=_-]{40,})$")
    offending = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        value = value.strip("'\"")
        if (secretish.search(key) and value) or random_looking.match(value):
            offending.append(key)
    assert not offending, f".env.example must not hold secret values: {offending}"


@pytest.mark.parametrize(
    "path",
    [".env", "community.yaml", "data/keys/signing.key"],
)
def test_local_files_are_git_ignored(path: str) -> None:
    if not (ROOT / ".git").exists() or not GIT:
        pytest.skip("not a git checkout")
    # Fixed argv from the parametrize list above, no shell.
    result = subprocess.run(  # noqa: S603
        [GIT, "check-ignore", "-q", "--no-index", path], cwd=ROOT, check=False
    )
    assert result.returncode == 0, f"{path} is not git-ignored"


def test_read_secret_strips_value(tmp_path: Path) -> None:
    (tmp_path / "database_password").write_text("abc123\n")

    assert read_secret("database_password", tmp_path) == "abc123"


def test_read_secret_missing_file_names_path(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match="database_password"):
        read_secret("database_password", tmp_path)


def test_read_secret_empty_file(tmp_path: Path) -> None:
    (tmp_path / "database_password").write_text("\n")

    with pytest.raises(SecretError, match="empty"):
        read_secret("database_password", tmp_path)


def test_read_secret_rejects_unknown_names(tmp_path: Path) -> None:
    with pytest.raises(SecretError, match="unknown"):
        read_secret("../etc/passwd", tmp_path)


def test_check_secrets_requires_all_app_secrets(tmp_path: Path) -> None:
    for name in APP_SECRETS[:-1]:
        (tmp_path / name).write_text("x")

    with pytest.raises(SecretError, match=APP_SECRETS[-1]):
        check_secrets(tmp_path)

    (tmp_path / APP_SECRETS[-1]).write_text("x")
    check_secrets(tmp_path)


def test_secret_error_never_contains_value(tmp_path: Path) -> None:
    path = tmp_path / "database_password"
    path.write_text("supersecretvalue")
    path.chmod(0o000)
    try:
        with pytest.raises(SecretError) as excinfo:
            read_secret("database_password", tmp_path)
    finally:
        path.chmod(0o600)
    assert "supersecretvalue" not in str(excinfo.value)
