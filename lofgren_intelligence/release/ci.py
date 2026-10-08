"""Exact-SHA CI evidence and release-artifact identity verification."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.request
import zipfile
from email.parser import BytesParser
from email.policy import compat32
from pathlib import Path
from typing import Any, Iterable

MATRIX = ("3.10", "3.11", "3.12")
PACKAGE_STEP = "Package smoke test (clean wheel, installed and run outside the source tree)"
ARTIFACT_MANIFEST_SCHEMA = "lofgren.release-artifact-manifest/1"
HEX_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")


class ArtifactManifestError(ValueError):
    """Raised when release artifacts cannot be identified unambiguously."""


def _require_object_id(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not HEX_OBJECT_ID.fullmatch(normalized):
        raise ArtifactManifestError(f"{label} must be a full 40-character Git object ID")
    return normalized


def _metadata_identity(raw: bytes, artifact: str) -> tuple[str, str]:
    metadata = BytesParser(policy=compat32).parsebytes(raw)
    name = str(metadata.get("Name", "")).strip()
    version = str(metadata.get("Version", "")).strip()
    if not name or not version:
        raise ArtifactManifestError(f"{artifact} metadata must contain Name and Version")
    return name, version


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(members) != 1:
                raise ArtifactManifestError(f"{path.name} must contain exactly one METADATA file")
            return _metadata_identity(archive.read(members[0]), path.name)
    except zipfile.BadZipFile as exc:
        raise ArtifactManifestError(f"{path.name} is not a valid wheel archive") from exc


def _sdist_identity(path: Path) -> tuple[str, str]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
            ]
            if len(members) != 1:
                raise ArtifactManifestError(f"{path.name} must contain exactly one top-level PKG-INFO file")
            stream = archive.extractfile(members[0])
            if stream is None:
                raise ArtifactManifestError(f"cannot read PKG-INFO from {path.name}")
            return _metadata_identity(stream.read(), path.name)
    except tarfile.TarError as exc:
        raise ArtifactManifestError(f"{path.name} is not a valid source archive") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_source_distribution(dist_dir: Path, epoch: int) -> Path:
    """Rewrite the single sdist with stable gzip and tar metadata."""
    if epoch < 0:
        raise ArtifactManifestError("source date epoch must not be negative")
    dist_dir = dist_dir.resolve()
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise ArtifactManifestError("dist must contain exactly one .tar.gz source distribution")
    source = sdists[0]
    entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
    try:
        with tarfile.open(source, mode="r:gz") as archive:
            for member in archive.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ArtifactManifestError(f"unsafe source distribution member: {member.name}")
                stream = archive.extractfile(member) if member.isfile() else None
                entries.append((member, stream.read() if stream is not None else None))
    except tarfile.TarError as exc:
        raise ArtifactManifestError(f"{source.name} is not a valid source archive") from exc

    temporary = source.with_name(f".{source.name}.normalized")
    with temporary.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                for original, payload in entries:
                    member = copy.copy(original)
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    member.mtime = epoch
                    member.pax_headers = {}
                    output.addfile(member, io.BytesIO(payload) if payload is not None else None)
    temporary.replace(source)
    return source


def build_artifact_manifest(dist_dir: Path, source_sha: str, source_tree: str) -> dict[str, Any]:
    """Bind exactly one wheel and sdist to a commit and tree."""
    dist_dir = dist_dir.resolve()
    if not dist_dir.is_dir():
        raise ArtifactManifestError(f"distribution directory does not exist: {dist_dir}")

    files = sorted((path for path in dist_dir.iterdir() if path.is_file()), key=lambda path: path.name)
    if any(path.is_symlink() for path in files):
        raise ArtifactManifestError("distribution artifacts must not be symbolic links")
    wheels = [path for path in files if path.suffix == ".whl"]
    sdists = [path for path in files if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(files) != 2:
        raise ArtifactManifestError("dist must contain exactly one wheel and one .tar.gz source distribution")

    wheel_identity = _wheel_identity(wheels[0])
    sdist_identity = _sdist_identity(sdists[0])
    if wheel_identity != sdist_identity:
        raise ArtifactManifestError("wheel and source distribution Name/Version metadata do not match")

    artifacts = []
    for kind, path in (("wheel", wheels[0]), ("sdist", sdists[0])):
        artifacts.append({
            "filename": path.name,
            "kind": kind,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        })
    return {
        "artifacts": artifacts,
        "project": wheel_identity[0],
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "source": {
            "commit": _require_object_id(source_sha, "source SHA"),
            "tree": _require_object_id(source_tree, "source tree"),
        },
        "version": wheel_identity[1],
    }


def write_artifact_manifest(manifest: dict[str, Any], output: Path) -> None:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)


def verify_artifact_manifest(
    manifest_path: Path,
    dist_dir: Path,
    *,
    expected_sha: str | None = None,
    expected_tree: str | None = None,
) -> dict[str, Any]:
    try:
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactManifestError(f"cannot read artifact manifest: {manifest_path}") from exc
    if not isinstance(recorded, dict) or recorded.get("schema") != ARTIFACT_MANIFEST_SCHEMA:
        raise ArtifactManifestError(f"artifact manifest schema must be {ARTIFACT_MANIFEST_SCHEMA}")
    source = recorded.get("source")
    if not isinstance(source, dict):
        raise ArtifactManifestError("artifact manifest source identity is missing")

    recorded_sha = _require_object_id(str(source.get("commit", "")), "manifest source SHA")
    recorded_tree = _require_object_id(str(source.get("tree", "")), "manifest source tree")
    if expected_sha is not None and recorded_sha != _require_object_id(expected_sha, "expected SHA"):
        raise ArtifactManifestError("artifact manifest commit does not match the expected SHA")
    if expected_tree is not None and recorded_tree != _require_object_id(expected_tree, "expected tree"):
        raise ArtifactManifestError("artifact manifest tree does not match the expected tree")

    actual = build_artifact_manifest(dist_dir, recorded_sha, recorded_tree)
    if recorded != actual:
        raise ArtifactManifestError("artifact manifest does not match the distribution files")
    return recorded


def _get(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "lofgren-release-gate",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_exact_ci(
    *,
    repo: str,
    sha: str,
    run_id: int | None,
    required_steps: Iterable[str],
    workflow_name: str = "tests",
) -> tuple[bool, str, bool, str]:
    """Verify the exact workflow, SHA, Python matrix and required steps.

    A run id is only a locator. It is never accepted as evidence by itself.
    """
    if run_id is None:
        return False, "no CI run id supplied", False, "no package evidence"
    try:
        run = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}")
        if run.get("head_sha") != sha:
            return False, f"run {run_id} tested {run.get('head_sha')} not {sha}", False, "wrong SHA"
        if run.get("name") != workflow_name:
            return False, f"run {run_id} is workflow {run.get('name')!r}, expected {workflow_name!r}", False, "wrong workflow"
        if run.get("event") != "push":
            return False, f"run {run_id} event is {run.get('event')!r}, expected push", False, "wrong event"
        jobs = _get(f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/jobs").get("jobs", [])
    except Exception as exc:
        return False, f"GitHub API unavailable: {exc}", False, "no package evidence"

    by_python: dict[str, dict] = {}
    for job in jobs:
        match = re.search(r"\((3\.\d+)\)", str(job.get("name") or ""))
        if match:
            by_python[match.group(1)] = job

    problems: list[str] = []
    package_problems: list[str] = []
    required_steps = tuple(required_steps)
    for version in MATRIX:
        job = by_python.get(version)
        if job is None:
            problems.append(f"missing Python {version} job")
            package_problems.append(f"missing Python {version} job")
            continue
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            problems.append(f"Python {version} job is {job.get('status')}/{job.get('conclusion')}")
        steps = {step.get("name"): step.get("conclusion") for step in job.get("steps", [])}
        for step in required_steps:
            if steps.get(step) != "success":
                problems.append(f"Python {version} step {step!r}: {steps.get(step, 'missing')}")
        if steps.get(PACKAGE_STEP) != "success":
            package_problems.append(f"Python {version} package step: {steps.get(PACKAGE_STEP, 'missing')}")

    ci_ok = not problems
    pkg_ok = not package_problems
    return (
        ci_ok,
        "; ".join(problems) or f"run {run_id}: exact SHA and all required matrix steps passed",
        pkg_ok,
        "; ".join(package_problems) or f"run {run_id}: clean-wheel package step passed on {', '.join(MATRIX)}",
    )


def _artifact_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify an exact-SHA release artifact manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("artifact-create")
    create.add_argument("--dist", type=Path, required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--source-tree", required=True)
    create.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("artifact-verify")
    verify.add_argument("--dist", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--expected-sha")
    verify.add_argument("--expected-tree")
    normalize = subparsers.add_parser("artifact-normalize-sdist")
    normalize.add_argument("--dist", type=Path, required=True)
    normalize.add_argument("--epoch", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _artifact_parser().parse_args(argv)
    try:
        if args.command == "artifact-create":
            manifest = build_artifact_manifest(args.dist, args.source_sha, args.source_tree)
            write_artifact_manifest(manifest, args.output)
        elif args.command == "artifact-verify":
            manifest = verify_artifact_manifest(
                args.manifest,
                args.dist,
                expected_sha=args.expected_sha,
                expected_tree=args.expected_tree,
            )
        else:
            normalized = normalize_source_distribution(args.dist, args.epoch)
            manifest = {"normalized_sdist": normalized.name, "source_date_epoch": args.epoch}
    except ArtifactManifestError as exc:
        print(f"release artifact identity failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
