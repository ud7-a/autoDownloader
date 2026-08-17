"""Sets APP_VERSION in utils/config.py.  py tools/bump_version.py 4.2.3

APP_VERSION is what the in-app updater compares against the latest GitHub release, so
it has to match the release tag exactly or every installed copy either misses the
update or re-installs one it already has. The release workflow calls this, then checks
the result with `build_release.py --expect-version`.
"""

import os
import re
import sys

CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "utils", "config.py")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: bump_version.py X.Y.Z")
    version = sys.argv[1].lstrip("v").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit(f"bad version {version!r}: expected X.Y.Z")

    src = open(CONFIG, encoding="utf-8").read()
    new, count = re.subn(r'^APP_VERSION\s*=\s*["\'][^"\']+["\']',
                         f'APP_VERSION = "{version}"', src, count=1, flags=re.M)
    if count != 1:
        sys.exit("APP_VERSION not found in utils/config.py")
    if new == src:
        print(f"APP_VERSION already {version}")
        return
    open(CONFIG, "w", encoding="utf-8", newline="").write(new)
    print(f"APP_VERSION -> {version}")


if __name__ == "__main__":
    main()
