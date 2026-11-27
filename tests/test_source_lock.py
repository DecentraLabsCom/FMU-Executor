from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.create_source_lock import source_lock


def create_executor_checkout(
    tmp_path: Path, version: str = "0.3.11", package_version: str | None = None
) -> tuple[Path, str]:
    repository = tmp_path / "executor"
    repository.mkdir()
    (repository / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        f'[project]\nname = "fixture"\nversion = "{package_version or version}"\n', encoding="utf-8"
    )
    (repository / "requirements.txt").write_text("fastapi==1.0\n", encoding="utf-8")
    (repository / "README.md").write_text("executor fixture\n", encoding="utf-8")
    (repository / "app").mkdir()
    (repository / "app" / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repository, commit


def test_source_lock_pins_source_runtime_and_oci_image(tmp_path: Path) -> None:
    repository, commit = create_executor_checkout(tmp_path)

    lock = source_lock(repository, commit, f"sha256:{'a' * 64}")

    assert lock["schemaVersion"] == 1
    assert lock["version"] == "0.3.11"
    assert lock["commit"] == commit
    assert lock["sourceTree"]["format"] == "git-ls-tree-manifest-v1"
    assert len(lock["sourceTree"]["sha256"]) == 64
    assert lock["runtimePayload"]["format"] == "sha256-path-manifest-v1"
    assert len(lock["runtimePayload"]["sha256"]) == 64
    assert lock["containerImage"]["reference"].endswith(f"@sha256:{'a' * 64}")


@pytest.mark.parametrize(
    "image_digest",
    ["sha256:bad", "sha256:" + "A" * 64],
)
def test_source_lock_rejects_invalid_image_digests(tmp_path: Path, image_digest: str) -> None:
    repository, commit = create_executor_checkout(tmp_path)

    with pytest.raises(ValueError, match="image digest"):
        source_lock(repository, commit, image_digest)


def test_source_lock_rejects_mismatched_release_versions(tmp_path: Path) -> None:
    repository, commit = create_executor_checkout(tmp_path, package_version="0.3.10")

    with pytest.raises(ValueError, match="VERSION and pyproject.toml"):
        source_lock(repository, commit, f"sha256:{'a' * 64}")
