#!/usr/bin/env python3
"""COMMONS preflight: checks a repo for cloud dependencies and common security mistakes.

Usage: python preflight.py <repo_path> [--allow-host HOST ...]

Exits 1 if any ERROR is found. Standard library only (PyYAML used if installed).
Checks:
  - external URLs in runtime code/templates (CDNs, hosted AI APIs, analytics, fonts)
  - docker-compose: published ports, privileged, docker.sock, unpinned images,
    missing non-root user / read_only / cap_drop / no-new-privileges
  - committed secrets (.env, private keys, obvious tokens)
"""

import argparse
import os
import re
import sys
from pathlib import Path

RUNTIME_EXT = {
    ".py",
    ".html",
    ".jinja",
    ".j2",
    ".js",
    ".ts",
    ".css",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".conf",
}
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    "tests",
    "docs",
}
SKIP_FILES = {
    "preflight.py",
    "package-lock.json",
    "uv.lock",
    "poetry.lock",
    "requirements.txt",
    "requirements.lock",
}

BAD_HOSTS = [
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.mistral.ai",
    "api-inference.huggingface.co",
    "router.huggingface.co",
    "api.groq.com",
    "api.together.xyz",
    "cdn.jsdelivr.net",
    "unpkg.com",
    "cdnjs.cloudflare.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "ajax.googleapis.com",
    "code.jquery.com",
    "cdn.tailwindcss.com",
    "use.fontawesome.com",
    "google-analytics.com",
    "googletagmanager.com",
    "plausible.io",
    "sentry.io",
    "segment.io",
    "matrix.org",
    "vector.im",
    "element.io",
]
URL_RE = re.compile(r"https?://([A-Za-z0-9.-]+)")
OK_HOST_RE = re.compile(
    r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|app|bot|llm|matrix|caddy|([a-z0-9-]+\.)+(lan|local|internal|home\.arpa)|example\.(com|org)|.*\.example\.(com|org)|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)$"
)
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (OPENSSH |EC |RSA |)PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "API key-like token"),
    (re.compile(r"\bsyt_[A-Za-z0-9_]{20,}"), "Matrix access token"),
    (
        re.compile(r"(?i)(password|secret|token)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
        "hard-coded secret",
    ),
]

findings = []


def add(level, path, msg):
    """Record one preflight finding for the final sorted report."""
    findings.append((level, path, msg))


def _git_files(root):
    """Files git would commit (tracked plus untracked, not ignored), or None outside git.

    Git-ignored local files such as .env and secrets/ are on every working box
    by design; the check is about what reaches the repository.
    """
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None or not os.path.exists(os.path.join(root, ".git")):
        return None
    try:
        out = subprocess.run(  # noqa: S603  # nosec B603 - fixed git subcommand, no user input
            [git, "-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [f for f in out.decode().split("\0") if f]


def walk(root):
    """Yield files below root while excluding generated and dependency directories."""
    tracked = _git_files(root)
    if tracked is not None:
        for rel in tracked:
            parts = rel.split("/")
            if any(part in SKIP_DIRS for part in parts[:-1]):
                continue
            path = os.path.join(root, rel)
            if os.path.isfile(path):
                yield path
        return
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            yield os.path.join(d, f)


def check_urls(root, allow):
    """Report disallowed external runtime hosts, distinguishing known blockers."""
    for p in walk(root):
        name = os.path.basename(p)
        if name in SKIP_FILES or os.path.splitext(p)[1] not in RUNTIME_EXT:
            continue
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for host in URL_RE.findall(line):
                host = host.lower().rstrip(".")
                if host in allow or OK_HOST_RE.match(host):
                    continue
                level = (
                    "ERROR"
                    if any(host == b or host.endswith("." + b) for b in BAD_HOSTS)
                    else "WARN"
                )
                add(
                    level,
                    f"{os.path.relpath(p, root)}:{i}",
                    f"external URL host '{host}' (runtime must not depend on the internet)",
                )


def check_secrets(root):
    """Report committed secret-like files or text and missing ignore rules."""
    for p in walk(root):
        rel = os.path.relpath(p, root)
        base = os.path.basename(p)
        if base == ".env" or (base.startswith(".env.") and not base.endswith(".example")):
            add("ERROR", rel, ".env file in repo; keep secrets out of version control")
        if base.endswith((".pem", ".key")) and "example" not in base:
            add("ERROR", rel, "key file in repo")
        if (
            os.path.splitext(p)[1] not in RUNTIME_EXT | {".md", ".txt", ".sh", ""}
            or base in SKIP_FILES
        ):
            continue
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx, what in SECRET_PATTERNS:
            m = rx.search(text)
            if m and "example" not in rel and "change-me" not in m.group(0).lower():
                add("WARN", rel, f"possible {what}: {m.group(0)[:40]}...")
    gi = os.path.join(root, ".gitignore")
    if not os.path.exists(gi):
        add(
            "WARN",
            ".gitignore",
            "missing .gitignore (should ignore .env, secrets/, data/, models/)",
        )
    else:
        g = Path(gi).read_text()
        for must in (".env", "secrets", "data"):
            if must not in g:
                add("WARN", ".gitignore", f"does not mention '{must}'")


def load_yaml(path):
    """Parse YAML when PyYAML is installed; record malformed YAML as an error."""
    try:
        import yaml  # type: ignore

        return yaml.safe_load(open(path))
    except ImportError:
        return None
    except Exception as e:
        add("ERROR", path, f"invalid YAML: {e}")
        return None


def check_compose(root):
    """Check Compose services, network exposure, image pins, and hardening controls."""
    paths = [
        os.path.join(root, n)
        for n in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    ]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        add(
            "WARN",
            "docker-compose.yml",
            "no compose file found (COMMONS expects a one-command local start)",
        )
        return
    path = paths[0]
    rel = os.path.relpath(path, root)
    data = load_yaml(path)
    raw = Path(path).read_text()
    if data is None:
        if "privileged: true" in raw:
            add("ERROR", rel, "privileged container")
        if "docker.sock" in raw:
            add("ERROR", rel, "docker socket mounted")
        add("INFO", rel, "PyYAML not installed; only basic compose checks ran")
        return
    services = (data or {}).get("services", {}) or {}
    exposed = []
    for name, svc in services.items():
        svc = svc or {}
        where = f"{rel}:{name}"
        ports = svc.get("ports") or []
        if ports:
            exposed.append(name)
        img = svc.get("image")
        if img and "@sha256:" not in img:
            add("WARN", where, f"image '{img}' not pinned by digest")
        if img and (img.endswith(":latest") or ":" not in img.split("/")[-1]):
            add("WARN", where, "image uses latest/implicit tag")
        if svc.get("privileged"):
            add("ERROR", where, "privileged: true")
        if any("docker.sock" in str(v) for v in svc.get("volumes") or []):
            add("ERROR", where, "docker socket mounted")
        if svc.get("network_mode") == "host":
            add("ERROR", where, "network_mode: host bypasses network isolation")
        if not svc.get("user"):
            add("WARN", where, "no non-root 'user:' set")
        if not svc.get("read_only"):
            add("WARN", where, "read_only: true not set")
        if "ALL" not in [str(c).upper() for c in svc.get("cap_drop") or []]:
            add("WARN", where, "cap_drop: [ALL] not set")
        if not any("no-new-privileges" in str(s) for s in svc.get("security_opt") or []):
            add("WARN", where, "security_opt no-new-privileges not set")
        env = svc.get("environment") or {}
        items = (
            env.items()
            if isinstance(env, dict)
            else [tuple(e.split("=", 1)) if "=" in e else (e, "") for e in env]
        )
        for k, v in items:
            # A *_FILE variable pointing at a mounted Docker secret holds a path, not a secret.
            if str(k).upper().endswith("_FILE") and str(v).startswith("/run/secrets/"):
                continue
            if (
                re.search(r"(?i)(password|secret|token|key)", str(k))
                and v
                and not str(v).startswith("${")
                and "change" not in str(v).lower()
            ):
                add(
                    "ERROR",
                    where,
                    f"secret '{k}' hard-coded in compose; use secrets files or ${{VAR}}",
                )
        if name in ("llm", "ollama", "llama", "model") and ports:
            add("ERROR", where, "model server publishes ports; it must be internal-only")
    if len(exposed) > 1:
        add(
            "WARN",
            rel,
            f"several services publish ports ({', '.join(exposed)}); only the reverse proxy should",
        )
    nets = (data or {}).get("networks", {}) or {}
    if not any((n or {}).get("internal") for n in nets.values()):
        add(
            "WARN", rel, "no network with 'internal: true' (backend services should have no egress)"
        )


def main():
    """Run repository checks and exit with status 1 when any ERROR is found."""
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--allow-host", action="append", default=[])
    a = ap.parse_args()
    root = os.path.abspath(a.repo)
    allow = {h.lower() for h in a.allow_host}
    check_urls(root, allow)
    check_secrets(root)
    check_compose(root)
    order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    for level, where, msg in sorted(findings, key=lambda f: order[f[0]]):
        print(f"[{level}] {where}: {msg}")
    errors = sum(1 for f in findings if f[0] == "ERROR")
    warns = sum(1 for f in findings if f[0] == "WARN")
    print(f"\npreflight: {errors} error(s), {warns} warning(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
