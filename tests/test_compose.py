from pathlib import Path

import anyio
import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_services_are_hardened_and_private() -> None:
    """Require the one-box topology and container hardening in the Compose file."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    assert set(services) == {"caddy", "matrix", "mas", "postgres", "app", "llm", "bot", "egress"}
    # Caddy publishes the only port; everything else is unreachable from the LAN.
    assert services["caddy"]["ports"] == ["${COMMONS_BIND:-0.0.0.0}:443:8443"]
    for name, service in services.items():
        if name != "caddy":
            assert "ports" not in service, f"{name} must not publish ports"

    for name, service in services.items():
        assert service["user"], name
        assert not str(service["user"]).startswith("0:"), f"{name} must not run as root"
        assert service["read_only"] is True, name
        assert service["tmpfs"], name
        assert service["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in service["security_opt"], name
        assert service["mem_limit"], name
        assert service["pids_limit"], name
        assert "privileged" not in service, name
        volumes = service.get("volumes", [])
        assert not any("docker.sock" in v for v in volumes), name
        if "image" in service and "build" not in service:
            assert "@sha256:" in service["image"], f"{name} image must be pinned by digest"
    for name in ("caddy", "app"):
        assert services[name]["healthcheck"], name
    # Session cookies stay `Secure` on the box (the setting exists for local tests only).
    assert "COMMONS_COOKIE_SECURE" not in services["app"]["environment"]

    networks = compose["networks"]
    assert networks["internal"]["internal"] is True
    assert not (networks["web"] or {}).get("internal", False)
    assert set(services["caddy"]["networks"]) == {"web", "internal"}
    # Only the allowlist proxy reaches the outside; backend services stay internal.
    assert set(services["egress"]["networks"]) == {"internal", "outbound"}
    for name in ("matrix", "mas", "postgres", "app", "llm", "bot"):
        assert services[name]["networks"] == ["internal"], name


def test_egress_allows_exactly_the_documented_hosts() -> None:
    """The allowlist proxy lets out only the documented exceptions, HTTPS only."""
    allowlist = (ROOT / "deploy/egress/allowlist.txt").read_text().split()
    # matrix.org: Element's push gateway; SMHI: the weather forecast.
    assert allowlist == [r"^matrix\.org$", r"^opendata-download-metfcst\.smhi\.se$"]
    conf = [
        line.split(None, 1)
        for line in (ROOT / "deploy/egress/tinyproxy.conf").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    settings = {}
    for key, value in conf:
        settings.setdefault(key, []).append(value.strip())
    assert settings["FilterDefaultDeny"] == ["Yes"]
    assert settings["ConnectPort"] == ["443"]
    assert settings["FilterURLs"] == ["Off"]
    assert settings["Filter"] == ['"/etc/tinyproxy/allowlist.txt"']


def _caddy_sites() -> dict[str, str]:
    """Map each public site address in the Caddyfile to the text of its block."""
    text = (ROOT / "deploy/caddy/Caddyfile").read_text()
    sites: dict[str, str] = {}
    current = None
    depth = 0
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if depth == 0 and stripped.startswith("{$COMMONS_") and stripped.endswith("{"):
            current = stripped[:-1].strip()
            sites[current] = ""
        elif current is not None:
            sites[current] += line + "\n"
        depth += stripped.count("{") - stripped.count("}")
        # Placeholders like {$X:y} open and close on one line, so they cancel out.
        if depth == 0:
            current = None
    return sites


def test_every_public_site_blocks_internal_first() -> None:
    """/internal is refused on every host before anything is proxied."""
    sites = _caddy_sites()
    assert set(sites) == {
        "{$COMMONS_HOSTS:localhost}",
        "{$COMMONS_MATRIX_HOST:matrix.localhost}",
        "{$COMMONS_AUTH_HOST:auth.localhost}",
    }
    for name, block in sites.items():
        assert "import site_common" in block, name
        route = block.split("route {", 1)[1]
        first = next(line.strip() for line in route.splitlines() if line.strip())
        assert first == "import block_internal", name


def test_login_endpoints_are_rate_limited_at_caddy() -> None:
    """Caddy caps password logins on the Matrix and MAS hosts per client IP."""
    sites = _caddy_sites()
    assert "import matrix_login_limits" in sites["{$COMMONS_MATRIX_HOST:matrix.localhost}"]
    auth = sites["{$COMMONS_AUTH_HOST:auth.localhost}"]
    assert "rate_limit" in auth
    assert "path /login /register /register/*" in auth


def test_make_up_verifies_the_model_checksum() -> None:
    """`make up` refuses to start llm with weights that don't match deploy/models.sha256."""
    makefile = (ROOT / "Makefile").read_text()
    up = next(line for line in makefile.splitlines() if line.startswith("up:"))
    assert "verify-model" in up


def test_continuwuity_is_gone() -> None:
    """One homeserver: no leftover Continuwuity stack or config."""
    assert not (ROOT / "deploy/matrix").exists()
    assert not (ROOT / "deploy/caddy/compose.yml").exists()
    assert not (ROOT / "deploy/synapse/compose.override.yml").exists()
    assert "continuwuity" not in (ROOT / "deploy/caddy/Caddyfile").read_text().lower()


def test_app_health_endpoint() -> None:
    """Verify the ASGI health route used by the app container health check."""
    from app.main import app

    async def get_health() -> tuple[int, dict]:
        """Request the health route in-process without starting a network server."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        return response.status_code, response.json()

    status_code, body = anyio.run(get_health)

    assert status_code == 200
    assert body == {"status": "ok"}


def test_image_pinner_skips_locally_built_services() -> None:
    """Ensure digest pinning does not treat locally built service tags as registry images."""
    from scripts.pin_images import image_lines

    text = """\
services:
  caddy:
    image: caddy:2.10.2-alpine
  app:
    build:
      context: .
    image: commons-app:local
"""
    services = {
        "caddy": {"image": "caddy:2.10.2-alpine"},
        "app": {"build": {"context": "."}, "image": "commons-app:local"},
    }

    assert image_lines(text, services) == {"caddy": (3, "caddy:2.10.2-alpine")}


def test_image_pinner_finds_dockerfile_base_arguments() -> None:
    """Ensure Dockerfile ARG declarations that identify base images are discovered."""
    from scripts.pin_images import dockerfile_image_lines

    text = """\
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS build
"""

    assert dockerfile_image_lines(text) == {1: "python:3.12-slim"}


def test_dockerfile_base_images_are_digest_pinned() -> None:
    """Require every external Dockerfile base image (ARG or FROM) to include a digest."""
    from scripts.pin_images import dockerfile_image_lines

    dockerfiles = [ROOT / "Dockerfile", ROOT / "bot/Dockerfile", *ROOT.glob("deploy/*/Dockerfile")]
    for dockerfile in dockerfiles:
        text = dockerfile.read_text()
        images = list(dockerfile_image_lines(text).values())
        stages = set()
        for line in text.splitlines():
            parts = line.split()
            if not parts or parts[0].upper() != "FROM":
                continue
            image = next(p for p in parts[1:] if not p.startswith("--"))
            if not image.startswith("$") and image not in stages:
                images.append(image)
            if len(parts) >= 4 and parts[-2].upper() == "AS":
                stages.add(parts[-1])
        assert images, dockerfile
        unpinned = [i for i in images if "@sha256:" not in i]
        assert not unpinned, f"{dockerfile.relative_to(ROOT)}: {unpinned}"
