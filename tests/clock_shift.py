"""Run unittest with the real wall clock shifted forward (Q17 clock-drift check).

    python tests/clock_shift.py --days 365 discover -s tests -t .

Every real-clock source a test or the code under test can read -- time.time,
time.time_ns, datetime.now/utcnow/today and date.today -- is moved forward by
the offset before anything else is imported. A test that pins one side of a
comparison to a fixed instant and reads the real clock on the other side, or
that measures a TTL from a fixed instant, fails here long before it would fail
in CI. Monotonic clocks are left alone. Python subprocesses started by a test
inherit the shift (see clock_shift_site/sitecustomize.py).

Not collected as a test module (no ``test`` prefix); tests/test_clock_drift.py
drives it in a subprocess.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time as _time

_RealDateTime = _dt.datetime
_RealDate = _dt.date


def install(offset_seconds: float) -> None:
    """Shift every real-clock source forward by `offset_seconds` (process-wide)."""
    if getattr(_dt, "_li_clock_shift", None) is not None:
        raise RuntimeError("clock shift already installed")
    delta = _dt.timedelta(seconds=offset_seconds)
    real_time, real_time_ns = _time.time, _time.time_ns

    class _Meta(type):
        # Instances created by C code (fromtimestamp in libraries, unpickling,
        # arithmetic on real datetimes) still count as datetimes/dates.
        def __instancecheck__(cls, obj):
            return isinstance(obj, cls.__real__)

        def __subclasscheck__(cls, sub):
            return issubclass(sub, cls.__real__)

    class ShiftedDateTime(_RealDateTime, metaclass=_Meta):
        __real__ = _RealDateTime

        @classmethod
        def now(cls, tz=None):
            return _RealDateTime.now(tz) + delta

        @classmethod
        def utcnow(cls):
            return _RealDateTime.utcnow() + delta

        @classmethod
        def today(cls):
            # Not _RealDateTime.today(): CPython builds it from time.time(), already shifted.
            return _RealDateTime.now() + delta

    class ShiftedDate(_RealDate, metaclass=_Meta):
        __real__ = _RealDate

        @classmethod
        def today(cls):
            return (_RealDateTime.now() + delta).date()

    _time.time = lambda: real_time() + offset_seconds
    _time.time_ns = lambda: real_time_ns() + int(offset_seconds * 1_000_000_000)
    _dt.datetime = ShiftedDateTime
    _dt.date = ShiftedDate
    _dt._li_clock_shift = offset_seconds


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--days":
        print(__doc__, file=sys.stderr)
        return 2
    offset = float(argv[2]) * 86400.0
    install(offset)
    print(f"clock_shift: +{argv[2]} days, now={_dt.datetime.now(_dt.timezone.utc).isoformat()}", file=sys.stderr)
    # Child Python processes inherit the same shift through clock_shift_site/sitecustomize.py.
    os.environ["LI_TEST_CLOCK_SHIFT_SECONDS"] = repr(offset)
    site_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock_shift_site")
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (site_dir, os.environ.get("PYTHONPATH")) if p)
    # Like ``python -m unittest``: the working directory (the repo root) is importable,
    # this script's own directory is not.
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
    sys.path.insert(0, os.getcwd())
    import unittest

    # unittest's own argv[0] is ignored; the rest are ordinary unittest arguments.
    prog = unittest.main(module=None, argv=["clock_shift", *argv[3:]], exit=False)
    result = prog.result
    if result.skipped:
        print(f"clock_shift: {len(result.skipped)} skipped test(s) count as failures", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
