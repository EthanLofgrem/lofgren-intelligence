"""Write a produced artifact to a directory and load it back for independent verification.

Layout: the artifact's own files at their relative paths, plus three V3 records:
`lofgren-artifact.json` (the artifact without file contents), `production-receipt.json` and `v4-handoff.json`.
Loading reads only declared paths, refuses any path that escapes the directory and reports undeclared files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .core import (_safe_path, check_production_receipt, validate_v4_handoff, verify_artifact)
from .errors import ProductionError

ARTIFACT_RECORD = "lofgren-artifact.json"
RECEIPT_RECORD = "production-receipt.json"
HANDOFF_RECORD = "v4-handoff.json"
RECORDS = (ARTIFACT_RECORD, RECEIPT_RECORD, HANDOFF_RECORD)


def _dump(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_artifact(result: Any, directory: str | Path) -> list[Path]:
    """Write a ProductionResult. The directory must be new or empty; nothing is overwritten."""
    root = Path(directory)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ProductionError(f"{root} exists and is not an empty directory; V3 never overwrites files")
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for f in result.artifact["files"]:
        if f["path"] in RECORDS:
            raise ProductionError(f"artifact file {f['path']} collides with a V3 record name")
        target = root / _safe_path(f["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f["content"].encode("utf-8"))
        written.append(target)
    record = {k: v for k, v in result.artifact.items() if k != "files"}
    record["files"] = [{k: f[k] for k in ("path", "media_type", "sha256")} for f in result.artifact["files"]]
    for name, value in ((ARTIFACT_RECORD, record), (RECEIPT_RECORD, result.receipt),
                        (HANDOFF_RECORD, result.v4_handoff)):
        (root / name).write_bytes(_dump(value))
        written.append(root / name)
    return written


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductionError(f"cannot read {path.name}: {exc}") from None


def load_artifact(directory: str | Path) -> tuple[dict, dict, dict, list[str]]:
    """(artifact with contents, receipt, V4 handoff, undeclared files) from a directory written by write_artifact."""
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ProductionError(f"{directory} is not a directory")
    record = _read_json(root / ARTIFACT_RECORD)
    if not isinstance(record, dict) or not isinstance(record.get("files"), list):
        raise ProductionError(f"{ARTIFACT_RECORD} is not an artifact record")
    files = []
    for item in record["files"]:
        if not isinstance(item, Mapping):
            raise ProductionError(f"{ARTIFACT_RECORD} has a malformed file entry")
        rel = _safe_path(item.get("path"))
        target = (root / rel).resolve()
        if root not in target.parents:
            raise ProductionError(f"declared path {rel} escapes the artifact directory")
        try:
            content = target.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProductionError(f"cannot read declared file {rel}: {exc}") from None
        files.append({**{k: item.get(k) for k in ("path", "media_type", "sha256")}, "content": content})
    artifact = {**record, "files": files}
    declared = {f["path"] for f in files} | set(RECORDS)
    undeclared = sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                        if p.is_file() and p.relative_to(root).as_posix() not in declared
                        and "__pycache__" not in p.relative_to(root).parts)  # left by running the tests
    return artifact, _read_json(root / RECEIPT_RECORD), _read_json(root / HANDOFF_RECORD), undeclared


def verify_directory(directory: str | Path) -> dict[str, Any]:
    """Independently verify an artifact directory: files, receipt binding, V4 handoff and undeclared files."""
    try:
        artifact, receipt, handoff, undeclared = load_artifact(directory)
    except ProductionError as exc:
        return {"passed": False, "problems": [str(exc)], "artifact_id": None, "tests": None}
    v = verify_artifact(artifact)
    problems = list(v.problems) + check_production_receipt(receipt, artifact)
    problems += [p for p in validate_v4_handoff(handoff, receipt) if p not in problems]
    problems += [f"undeclared file {p}" for p in undeclared]
    return {"passed": not problems, "problems": problems, "artifact_id": artifact.get("artifact_id"),
            "kind": artifact.get("kind"), "tests": v.tests}
