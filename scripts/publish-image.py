"""Publish the tagged dashboard image from either forge's release workflow."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Publication:
    registry: str
    image: str
    tag: str
    revision: str
    source: str
    username: str
    password: str = field(repr=False)

    @property
    def reference(self) -> str:
        return f"{self.registry}/{self.image}:{self.tag}"


def configuration(environment: Mapping[str, str]) -> Publication:
    def required(name: str) -> str:
        value = environment.get(name, "")
        if not value:
            raise ValueError(f"{name} is required")
        return value

    tag = required("RELEASE_TAG")
    if not re.fullmatch(r"v[A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError("RELEASE_TAG must start with v and be a Docker tag of at most 128 characters")
    server = urlsplit(required("FORGE_SERVER_URL"))
    if server.scheme not in ("http", "https") or not server.hostname or server.username or server.password:
        raise ValueError("FORGE_SERVER_URL must be an HTTP(S) server URL without credentials")
    registry = (environment.get("CONTAINER_REGISTRY") or server.netloc).lower()
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?", registry):
        raise ValueError("CONTAINER_REGISTRY must be a hostname with an optional port, without a scheme or path")
    if ":" in registry and not 1 <= int(registry.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("CONTAINER_REGISTRY port must be between 1 and 65535")
    repository = required("FORGE_REPOSITORY")
    image = (environment.get("CONTAINER_IMAGE") or repository).lower()
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    if not re.fullmatch(rf"{component}(?:/{component})+", image):
        raise ValueError("CONTAINER_IMAGE must be an owner/image path without a registry or tag")
    revision = required("RELEASE_REVISION")
    if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", revision):
        raise ValueError("RELEASE_REVISION must be a full Git commit hash")
    return Publication(
        registry=registry, image=image, tag=tag, revision=revision,
        source=f"{required('FORGE_SERVER_URL').rstrip('/')}/{repository}",
        username=required("REGISTRY_USERNAME"), password=required("REGISTRY_PASSWORD"),
    )


def publish(publication: Publication) -> None:
    # A dedicated config keeps credentials off a shared runner's normal
    # Docker login; the context removes it on completion or exceptions.
    with tempfile.TemporaryDirectory(prefix="deskmate-publish-") as directory:
        environment = {**os.environ, "DOCKER_CONFIG": directory}

        def docker(*arguments: str, password: str | None = None) -> None:
            subprocess.run(
                ["docker", *arguments], check=True, cwd=ROOT, env=environment,
                input=password, text=True,
            )

        docker(
            "build", "--platform", "linux/amd64", "--file", "dashboard/Dockerfile",
            "--label", f"org.opencontainers.image.version={publication.tag}",
            "--label", f"org.opencontainers.image.revision={publication.revision}",
            "--label", f"org.opencontainers.image.source={publication.source}",
            "--tag", publication.reference, ".",
        )
        docker("login", publication.registry, "--username", publication.username,
               "--password-stdin", password=publication.password + "\n")
        docker("push", publication.reference)
        print(f"Published {publication.reference}")
        latest = f"{publication.registry}/{publication.image}:latest"
        docker("tag", publication.reference, latest)
        docker("push", latest)
        print(f"Published {latest}")


def main() -> int:
    try:
        publish(configuration(os.environ))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Image publication failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
