#!/usr/bin/env python3
"""Resolve non-build Compose images to the digest of the image pulled by Docker."""

import json
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from pathlib import Path

import yaml

IMAGE_LINE = re.compile(r"^(?P<prefix>\s*image:\s*)(?P<image>\S+)(?P<suffix>\s*(?:#.*)?)$")
DOCKERFILE_IMAGE_LINE = re.compile(
    r"^(?P<prefix>\s*ARG\s+[A-Za-z_][A-Za-z0-9_]*_IMAGE=)"
    r"(?P<image>\S+)(?P<suffix>\s*(?:#.*)?)$"
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}$")


def image_lines(text: str, services: dict) -> dict[str, tuple[int, str]]:
    """Return registry image references in non-build Compose services by service name."""
    result = {}
    in_services = False
    current_service = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        if line == "services:":
            in_services = True
            continue
        if in_services and line and not line.startswith(" "):
            in_services = False
            current_service = None
        service_match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if in_services and service_match:
            current_service = service_match.group(1)
            continue
        image_match = IMAGE_LINE.match(line)
        if in_services and current_service and line.startswith("    ") and image_match:
            service = services[current_service]
            if "build" not in service:
                result[current_service] = (line_number, image_match.group("image"))
    return result


def dockerfile_image_lines(text: str) -> dict[int, str]:
    """Return external base-image references from Dockerfile image ARG declarations."""
    return {
        line_number: match.group("image")
        for line_number, line in enumerate(text.splitlines(), start=1)
        if (match := DOCKERFILE_IMAGE_LINE.match(line))
    }


def resolve_digest(image: str) -> str:
    """Pull an image and return its verified-format SHA-256 repository digest."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker CLI is required to pin images")
    subprocess.run([docker, "pull", image], check=True)  # noqa: S603  # nosec B603
    inspected = subprocess.run(  # noqa: S603  # nosec B603
        [docker, "image", "inspect", "--format={{json .RepoDigests}}", image],
        check=True,
        capture_output=True,
        text=True,
    )
    repo_digests = json.loads(inspected.stdout)
    if not repo_digests:
        raise RuntimeError(f"Docker returned no repository digest for {image}")

    digest = repo_digests[0].rsplit("@", 1)[-1]
    if not DIGEST.fullmatch(digest):
        raise RuntimeError(f"Docker returned an invalid repository digest for {image}")
    return digest


def write_pinned_references(path: Path, text: str, references: dict[int, str]) -> int:
    """Replace unpinned references atomically and return the number of replacements."""
    replacements = {}
    for line_number, image in references.items():
        if "@sha256:" in image:
            continue
        if "$" in image:
            raise ValueError(f"{path}:{line_number} uses a dynamic image reference")
        replacements[line_number] = (image, f"{image}@{resolve_digest(image)}")

    if not replacements:
        return 0

    lines = text.splitlines(keepends=True)
    for line_number, (old_image, pinned_image) in replacements.items():
        line = lines[line_number - 1]
        pattern = DOCKERFILE_IMAGE_LINE if path.name.startswith("Dockerfile") else IMAGE_LINE
        match = pattern.match(line.rstrip("\n"))
        if match is None or match.group("image") != old_image:
            raise RuntimeError(f"image reference in {path}:{line_number} changed while pinning")
        lines[line_number - 1] = f"{match.group('prefix')}{pinned_image}{match.group('suffix')}" + (
            "\n" if line.endswith("\n") else ""
        )

    stat = path.stat()
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.writelines(lines)
        temporary_path = Path(temporary.name)
    temporary_path.chmod(stat.st_mode)
    temporary_path.replace(path)

    for line_number, (_, pinned_image) in replacements.items():
        print(f"{path}:{line_number}: {pinned_image}")
    return len(replacements)


def pin_compose(path: Path) -> int:
    """Pin registry images in a Compose file, excluding services built from source."""
    text = path.read_text()
    compose = yaml.safe_load(text)
    services = compose.get("services", {})
    count = write_pinned_references(
        path,
        text,
        {line_number: image for line_number, image in image_lines(text, services).values()},
    )
    if not count:
        print(f"{path}: no unpinned Compose images found")
    return count


def pin_dockerfiles() -> int:
    """Pin external base images in Dockerfiles under the project and return the count."""
    dockerfiles = [
        path
        for directory in (Path("."), Path("app"), Path("bot"), Path("deploy"))
        for path in directory.glob("Dockerfile*")
        if path.is_file()
    ]
    return sum(
        write_pinned_references(path, text, dockerfile_image_lines(text))
        for path in dockerfiles
        if (text := path.read_text())
    )


def main() -> None:
    """Pin image references and exit nonzero when Docker or a reference cannot be resolved."""
    compose_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docker-compose.yml")
    try:
        pin_compose(compose_path)
        pin_dockerfiles()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: could not pin Compose images: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
