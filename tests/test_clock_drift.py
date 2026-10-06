"""Q17: tests must not depend on when they run.

Every test module that pins an instant (a hard-coded date, a certification clock, a
fixed `now`) or runs a certification is re-run with the real wall clock moved
forward by 30 days, 1 year and 3 years (tests/clock_shift.py; Python subprocesses
inherit the shift). A test that compares a fixed instant with the real clock, or
measures a TTL from a fixed instant, fails here instead of failing in CI on some
future date. Before this check, the MCP and CLI certification paths verified the
2026 fixtures against the real clock and broke once the year reached 2029.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
HARNESS = TESTS / "clock_shift.py"
SITE = TESTS / "clock_shift_site"
SHIFT_DAYS = (30, 365, 3 * 365)

# A pinned instant or a real-clock read next to one: hard-coded timestamps, the
# certification clocks, injected `now`s, and every certification run.
PINNED = re.compile(r"20[2-9]\d-\d\d-\d\d|datetime\(20[2-9]\d|CERT_NOW|\bnow=|(?i:certif)")


def clock_sensitive_modules() -> list[str]:
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if PINNED.search(path.read_text(encoding="utf-8")):
            out.append(f"tests.{path.stem}")
    return out


def _clean_env() -> dict[str, str]:
    # Each run shifts from the real clock, even if this test is itself running shifted.
    env = {k: v for k, v in os.environ.items() if k != "LI_TEST_CLOCK_SHIFT_SECONDS"}
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep)
             if p and Path(p).resolve() != SITE.resolve()]
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(days: int, modules: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HARNESS), "--days", str(days), *modules], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=_clean_env(), timeout=1800)


class ClockShiftHarness(unittest.TestCase):
    def test_the_shift_reaches_this_process_and_child_processes(self):
        probe = ("import datetime, subprocess, sys, time; "
                 "child = subprocess.run([sys.executable, '-c', 'import time; print(time.time())'], "
                 "capture_output=True, text=True).stdout.strip(); "
                 "print(datetime.datetime.now(datetime.timezone.utc).timestamp(), time.time(), "
                 "datetime.date.today().toordinal(), child)")
        env = {**_clean_env(), "LI_TEST_CLOCK_SHIFT_SECONDS": str(365 * 86400.0),
               "PYTHONPATH": os.pathsep.join(p for p in (str(SITE), _clean_env().get("PYTHONPATH")) if p)}
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                             env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        dt_now, time_now, today, child = out.stdout.split()
        real = datetime.now(timezone.utc)
        year = timedelta(days=365)
        for value in (float(dt_now), float(time_now), float(child)):
            self.assertAlmostEqual(value, (real + year).timestamp(), delta=600)
        self.assertEqual(int(today), (real + year).date().toordinal())

    def test_the_selection_covers_the_fixed_clock_modules(self):
        mods = clock_sensitive_modules()
        for name in ("tests.test_v4_execution", "tests.test_v1_future_evidence", "tests.test_case_approval",
                     "tests.test_durable_jobs", "tests.test_v2_mcp_discovery", "tests.test_v3_complete",
                     "tests.test_v2_certification", "tests.test_v3_production"):
            self.assertIn(name, mods)
        self.assertNotIn("tests.test_clock_drift", mods)


class InjectedClock(unittest.TestCase):
    """The clock the certifications and their tests now pin, exercised directly."""

    def test_server_clock_is_explicit_and_timezone_aware(self):
        from lofgren_intelligence.certification import CERT_NOW
        from lofgren_intelligence.mcp.server import Server

        self.assertIsNone(Server().now)  # default: the real clock
        self.assertIsNone(Server()._verifier())
        pinned = Server(now=CERT_NOW)
        self.assertEqual(pinned._clock(), CERT_NOW)
        self.assertEqual(pinned._verifier().now, CERT_NOW)
        with self.assertRaises(ValueError):
            Server(now=datetime(2026, 9, 30, 12, 0))

    def test_session_clock_decides_staleness_not_the_wall_clock(self):
        import lofgren_intelligence.certification as C
        from lofgren_intelligence.mcp.server import Server

        def statuses(now):
            srv = Server(now=now)
            out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "investigate", "arguments": {"objective": C.OBJECTIVE, "texts": C.AGREE_AND_CONFLICT}}})
            run = srv.runs[out["result"]["structuredContent"]["run_id"]]
            return {c.statement: (c.status.value, tuple(c.issues)) for c in run.graph.claims.values()}

        at_cert = statuses(C.CERT_NOW)
        self.assertEqual(statuses(C.CERT_NOW), at_cert)  # same pinned clock, same answer
        later = statuses(C.CERT_NOW.replace(year=C.CERT_NOW.year + 3))
        self.assertTrue(any(any(i.startswith("stale:") for i in issues) for _, issues in later.values()))
        self.assertFalse(any(any(i.startswith("stale:") for i in issues) for _, issues in at_cert.values()))

    def test_cli_as_of_needs_an_explicit_offset(self):
        import argparse

        from lofgren_intelligence.cli import _as_of

        self.assertEqual(_as_of("2026-09-30T12:00:00Z"), datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(_as_of("2026-09-30T05:00:00-07:00"), datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
        for bad in ("2026-09-30", "2026-09-30T12:00:00", "yesterday"):
            with self.assertRaises(argparse.ArgumentTypeError):
                _as_of(bad)


class ClockDrift(unittest.TestCase):
    def test_clock_sensitive_tests_pass_30_days_1_year_and_3_years_from_now(self):
        modules = clock_sensitive_modules()
        with ThreadPoolExecutor(max_workers=len(SHIFT_DAYS)) as pool:
            results = dict(zip(SHIFT_DAYS, pool.map(lambda d: _run(d, modules), SHIFT_DAYS)))
        for days, proc in results.items():
            with self.subTest(days=days):
                tail = proc.stderr[-4000:]
                self.assertEqual(proc.returncode, 0, f"+{days} days:\n{tail}")
                self.assertRegex(proc.stderr, r"\nOK\s*$", tail)  # no failures, errors or skips
                ran = re.search(r"Ran (\d+) tests?", proc.stderr)
                self.assertTrue(ran and int(ran.group(1)) > 100, tail)
                self.assertIn(f"clock_shift: +{days} days", proc.stderr)


if __name__ == "__main__":
    unittest.main()
