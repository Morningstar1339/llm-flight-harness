"""Put phase2/ and the repo root on sys.path for every test module.

The existing assert-scripts do this themselves and still run standalone
(`python tests/test_convergence.py`); this just makes them work under
pytest from any rootdir as well.
"""
import os
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_PHASE2 = os.path.dirname(_TESTS)
_REPO_ROOT = os.path.dirname(_PHASE2)

for _p in (_PHASE2, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
