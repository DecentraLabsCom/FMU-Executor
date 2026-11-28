"""Create the canonical source and container lock for an FMU-Executor release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path
from typing import TypedDict

REPOSITORY = "DecentraLabsCom/FMU-Executor"
IMAGE_REPOSITORY = "ghcr.io/decentralabscom/fmu-executor"
SOURCE_TREE_PATHS = ("VERSION", "pyproject.toml", "requirements.txt", "README.md", "app", "tests")
RUNTIME_PATHS = ("VERSION", "requirements.txt", "app")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class ContentDigest(TypedDict):
    format: str
    sha256: str


class ContainerImage(TypedDict):
    repository: str
    reference: str
    digest: str


class SourceLock(TypedDict):
    schemaVersion: int
    repository: str
    version: str
    runtimeDownloads: bool
    commit: str
    sourceTree: ContentDigest
    runtimePayload: ContentDigest
    containerImage: ContainerImage


def git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def source_lock(repository: Path, commit: str, image_digest: str) -> SourceLock:
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("commit must be a full lowercase Git SHA")
    if not IMAGE_DIGEST_PATTERN.fullmatch(image_digest):
        raise ValueError("image digest must use sha256:<64 lowercase hexadecimal characters>")

    resolved_commit = git_bytes(repository, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved_commit != commit:
        raise ValueError("commit did not resolve to the requested full Git SHA")

    version = git_bytes(repository, "show", f"{commit}:VERSION").decode("utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("VERSION must contain a three-part numeric release version")

    pyproject = tomllib.loads(git_bytes(repository, "show", f"{commit}:pyproject.toml").decode("utf-8"))
    package_version = pyproject.get("project", {}).get("version")
    if package_version != version:
        raise ValueError("VERSION and pyproject.toml project.version must match")

    source_tree = git_bytes(
        repository,
        "ls-tree",
        "-r",
        "--full-tree",
        commit,
        "--",
        *SOURCE_TREE_PATHS,
    )
    if not source_tree:
        raise ValueError("release source tree is empty")

    runtime_paths = git_bytes(
        repository,
        "ls-tree",
        "-r",
        "--full-tree",
        "--name-only",
        commit,
        "--",
        *RUNTIME_PATHS,
    ).decode("utf-8").splitlines()
    if not runtime_paths:
        raise ValueError("release runtime payload is empty")

    runtime_manifest = bytearray()
    for relative_path in sorted(runtime_paths):
        file_digest = hashlib.sha256(git_bytes(repository, "show", f"{commit}:{relative_path}")).hexdigest()
        runtime_manifest.extend(f"{relative_path}\t{file_digest}\n".encode("utf-8"))

    image_reference = f"{IMAGE_REPOSITORY}:{version}@{image_digest}"
    return {
        "schemaVersion": 1,
        "repository": REPOSITORY,
        "version": version,
        "runtimeDownloads": False,
        "commit": commit,
        "sourceTree": {
            "format": "git-ls-tree-manifest-v1",
            "sha256": hashlib.sha256(source_tree).hexdigest(),
        },
        "runtimePayload": {
            "format": "sha256-path-manifest-v1",
            "sha256": hashlib.sha256(runtime_manifest).hexdigest(),
        },
        "containerImage": {
            "repository": IMAGE_REPOSITORY,
            "reference": image_reference,
            "digest": image_digest,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest = source_lock(args.repository.resolve(), args.commit, args.image_digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
