"""Child-process hook for tests/clock_shift.py (never on the path otherwise).

clock_shift.py puts this directory on PYTHONPATH and sets LI_TEST_CLOCK_SHIFT_SECONDS,
so every Python subprocess a test starts (``python -m lofgren_intelligence mcp``,
the CLI, generated-artifact tests) runs on the same shifted clock as the parent.
"""

import importlib.util
import os

_offset = os.environ.get("LI_TEST_CLOCK_SHIFT_SECONDS")
if _offset:
    _path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "clock_shift.py")
    _spec = importlib.util.spec_from_file_location("_li_clock_shift", _path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod.install(float(_offset))
