"""Run every behaviour test in this folder.

Each test_*.py exits 0 (pass), 2 (skip — a prerequisite such as network or a
secret is absent), or anything else (fail). Skips never fail the run; the exit
code is non-zero only when a test genuinely failed.

    python3 tests/run_tests.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SKIP_EXIT_CODE = 2


def main() -> int:
    test_files = sorted(TESTS_DIR.glob("test_*.py"))
    if not test_files:
        print("no test_*.py files found")
        return 0

    failed = []
    skipped = []
    for test_file in test_files:
        print(f"── {test_file.name}")
        completed = subprocess.run([sys.executable, str(test_file)], text=True)
        if completed.returncode == 0:
            print(f"   PASS {test_file.name}")
        elif completed.returncode == SKIP_EXIT_CODE:
            print(f"   SKIP {test_file.name}")
            skipped.append(test_file.name)
        else:
            print(f"   FAIL {test_file.name} (exit {completed.returncode})")
            failed.append(test_file.name)

    total = len(test_files)
    print(
        f"\n{total - len(failed) - len(skipped)} passed, "
        f"{len(skipped)} skipped, {len(failed)} failed"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
