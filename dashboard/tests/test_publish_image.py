"""Publishing rejects bad input and stops before the next external operation."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("publish_image", ROOT / "scripts/publish-image.py")
assert spec is not None and spec.loader is not None
publisher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = publisher
spec.loader.exec_module(publisher)


def _environment() -> dict[str, str]:
    return {
        "FORGE_SERVER_URL": "https://gitea.example.com",
        "FORGE_REPOSITORY": "Owner/DeskMate",
        "RELEASE_TAG": "v1.2.3-rc.1",
        "RELEASE_REVISION": "a" * 40,
        "REGISTRY_USERNAME": "publisher",
        "REGISTRY_PASSWORD": "test-token",
    }


def test_registry_defaults_and_image_name_normalization() -> None:
    publication = publisher.configuration(_environment())
    assert publication.reference == "gitea.example.com/owner/deskmate:v1.2.3-rc.1"
    assert publication.source == "https://gitea.example.com/Owner/DeskMate"
    environment = {**_environment(), "CONTAINER_REGISTRY": "ghcr.io", "CONTAINER_IMAGE": "Owner/Hub"}
    assert publisher.configuration(environment).reference == "ghcr.io/owner/hub:v1.2.3-rc.1"


@pytest.mark.parametrize("tag", ["v", "vRelease", "v1.2.3", "v" + "x" * 127])
def test_valid_tag_is_preserved_exactly(tag: str) -> None:
    assert publisher.configuration({**_environment(), "RELEASE_TAG": tag}).tag == tag


@pytest.mark.parametrize("name,value", [
    ("RELEASE_TAG", "main"), ("RELEASE_TAG", "v1.2.3+build"),
    ("RELEASE_TAG", "v1/next"), ("RELEASE_TAG", "v" + "x" * 128),
    ("CONTAINER_REGISTRY", "https://gitea.example.com"), ("CONTAINER_REGISTRY", "gitea.example.com/images"),
    ("CONTAINER_REGISTRY", "gitea.example.com:70000"), ("CONTAINER_IMAGE", "owner/image:latest"),
    ("CONTAINER_IMAGE", "owner/../image"), ("RELEASE_REVISION", "abc123"),
    ("REGISTRY_PASSWORD", ""), ("REGISTRY_USERNAME", ""),
    ("FORGE_SERVER_URL", "https://user:password@gitea.example.com"),
])
def test_invalid_input_fails_before_docker(name: str, value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    environment = {**_environment(), name: value}
    monkeypatch.setattr(publisher.os, "environ", environment)

    def unexpected_run(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid input must not execute Docker")

    monkeypatch.setattr(publisher.subprocess, "run", unexpected_run)
    assert publisher.main() == 1


def test_publish_builds_labeled_image_then_logs_in_and_pushes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    config_paths = []

    def docker(arguments: list[str], **options: object) -> None:
        calls.append((arguments, options))
        config = Path(options["env"]["DOCKER_CONFIG"])
        config_paths.append(config)
        assert config.is_dir()
        assert options["cwd"] == ROOT
        assert options["check"] is True
        if arguments[1] == "login":
            (config / "config.json").write_text("test authentication", encoding="utf-8")

    monkeypatch.setattr(publisher.subprocess, "run", docker)
    publication = publisher.configuration(_environment())
    publisher.publish(publication)
    build, login, push = calls
    assert build[0][:6] == ["docker", "build", "--platform", "linux/amd64", "--file", "dashboard/Dockerfile"]
    assert "org.opencontainers.image.version=v1.2.3-rc.1" in build[0]
    assert "org.opencontainers.image.revision=" + "a" * 40 in build[0]
    assert "org.opencontainers.image.source=https://gitea.example.com/Owner/DeskMate" in build[0]
    assert build[0][-3:] == ["--tag", publication.reference, "."]
    assert login[0] == ["docker", "login", publication.registry, "--username", "publisher", "--password-stdin"]
    assert login[1]["input"] == "test-token\n"
    assert all("test-token" not in argument for arguments, _ in calls for argument in arguments)
    assert push[0] == ["docker", "push", publication.reference]
    assert all(not path.exists() for path in config_paths)


@pytest.mark.parametrize("failure", ["build", "login", "push"])
def test_docker_failure_stops_pipeline_and_removes_auth(failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    config_paths = []

    def docker(arguments: list[str], **options: object) -> None:
        calls.append(arguments[1])
        config = Path(options["env"]["DOCKER_CONFIG"])
        config_paths.append(config)
        (config / "config.json").write_text("test authentication", encoding="utf-8")
        if arguments[1] == failure:
            raise subprocess.CalledProcessError(1, arguments)

    monkeypatch.setattr(publisher.subprocess, "run", docker)
    with pytest.raises(subprocess.CalledProcessError):
        publisher.publish(publisher.configuration(_environment()))
    assert calls == ["build", "login", "push"][:["build", "login", "push"].index(failure) + 1]
    assert all(not path.exists() for path in config_paths)
