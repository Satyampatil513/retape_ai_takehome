"""Make the test suite runnable from any working directory.

The tests refer to case folders by relative path ("cases/case1_feasible_even"),
and pytest may be invoked from the repo root or from inside tests/. Both are
pinned here so neither the imports nor the fixture paths depend on the caller's
cwd.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
