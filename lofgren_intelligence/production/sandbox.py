"""Run an artifact's generated tests in a separate, isolated interpreter.

Only the verifier calls this, and only after it has regenerated every file from the manifest and found it byte
identical, so the code executed is code V3 itself generates (arithmetic, json, pathlib and unittest). The run uses
a fresh temporary directory holding just the artifact files, ignores PYTHON* environment variables and user
site-packages (-E -s), writes no bytecode (-B), gets a minimal environment and a timeout.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

RUNNER = "unittest/isolated-subprocess/1"
TIMEOUT_S = 120


@dataclass(frozen=True)
class TestRun:
    ran: int
    failures: int
    errors: int
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        # `detail` is diagnostic output and stays out of receipts; the counts are deterministic.
        return {"runner": RUNNER, "ran": self.ran, "failures": self.failures, "errors": self.errors, "ok": self.ok}


def _env() -> dict[str, str]:
    keep = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR")  # needed by the interpreter on Windows
    return {k: os.environ[k] for k in keep if k in os.environ}


def run_tests(files: Iterable[Mapping[str, str]], module: str = "test_artifact") -> TestRun:
    with tempfile.TemporaryDirectory(prefix="lofgren-v3-") as tmp:
        root = Path(tmp)
        for f in files:
            target = root / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f["content"].encode("utf-8"))
        try:
            proc = subprocess.run([sys.executable, "-E", "-s", "-B", "-m", "unittest", "-v", module], cwd=root,
                                  env=_env(), capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return TestRun(0, 0, 0, False, f"tests timed out after {TIMEOUT_S}s")
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    ran = re.search(r"^Ran (\d+) tests?", text, re.M)
    fail = re.search(r"failures=(\d+)", text)
    err = re.search(r"errors=(\d+)", text)
    n, f, e = int(ran.group(1)) if ran else 0, int(fail.group(1)) if fail else 0, int(err.group(1)) if err else 0
    ok = proc.returncode == 0 and bool(re.search(r"^OK\s*$", text, re.M)) and n > 0
    return TestRun(n, f, e, ok, "\n".join(text.strip().splitlines()[-12:]))
