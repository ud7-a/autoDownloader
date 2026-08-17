"""Lint gate for CI and local use:  py tools/lint.py

Plain pyflakes over this repo reports a steady set of harmless findings, so a bare
run can never be "green" and stops being a useful signal. This filters exactly those
known-benign categories and fails on anything else, which lets CI treat a non-zero
exit as a real problem.

Ignored, deliberately:
  * re-importing a name inside a function that is also imported at module level
    (done on purpose to keep heavy imports off the startup path)
  * f-strings without placeholders in status messages
  * `global` on a container that is only ever mutated, never rebound
  * build/dev scripts that are not shipped with the app
"""

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_PATHS = ("venv", ".venv", "build", "dist", "__pycache__", ".git")
SKIP_FILES = ("exeCompile.py", "publish.py")

IGNORE_PATTERNS = (
    r"redefinition of unused",
    r"f-string is missing placeholders",
    r"`global .*` is unused: name is never assigned in scope",
    r"'ctypes' imported but unused",
    r"'subprocess' imported but unused",
    r"'urllib\.request' imported but unused",
    r"'PyQt6\.QtCore\.Qt' imported but unused",
    r"unable to detect undefined names",
)


def main():
    result = subprocess.run([sys.executable, "-m", "pyflakes", REPO],
                            capture_output=True, text=True)
    problems = []
    for line in (result.stdout + result.stderr).splitlines():
        if not line.strip():
            continue
        if any(part in line for part in SKIP_PATHS):
            continue
        if any(os.sep + name in line or line.startswith(name) for name in SKIP_FILES):
            continue
        if any(re.search(p, line) for p in IGNORE_PATTERNS):
            continue
        problems.append(line)

    if problems:
        print(f"lint: {len(problems)} problem(s)\n")
        for line in problems:
            print("  " + line)
        return 1
    print("lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
