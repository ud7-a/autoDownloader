"""Dump the live site configuration to the parity fixture.

These values drive real downloads: a changed selector or delay silently breaks every
episode for that site, and the delays had already drifted from the intended template
once before anyone noticed. The fixture is the picture of what the shipped sites do
right now, so a later edit has to be deliberate rather than accidental.

Run deliberately (not part of the build):

    py tools/snapshot_sites.py
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Redirect the data dir BEFORE importing anything from the app, so generating the
# fixture can never read or write the real config, history db or browser profile.
os.environ.setdefault("AED_APP_DIR", os.path.join(REPO, "build", "snapshot-appdir"))
os.makedirs(os.environ["AED_APP_DIR"], exist_ok=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.search_tab import SUPPORTED_SITES, DEFAULT_SITE_FLOWS       # noqa: E402
from core.selenium_engine import PATH_HOSTS                          # noqa: E402
from core.schedule import SCHEDULE_URLS, SCHEDULE_MATCH              # noqa: E402
from utils.browser_flags import DEFAULT_HOSTS                        # noqa: E402


def main():
    from utils import config
    if not config.IS_ISOLATED:
        sys.exit("refusing to run against the real data directory")

    payload = {
        "supported_sites": SUPPORTED_SITES,
        "site_flows": DEFAULT_SITE_FLOWS,
        "path_hosts": {k: list(v) for k, v in PATH_HOSTS.items()},
        "schedule_urls": SCHEDULE_URLS,
        "schedule_match": SCHEDULE_MATCH,
        "dns_hosts": list(DEFAULT_HOSTS),
    }
    dest = os.path.join(REPO, "tests", "fixtures", "sites_snapshot.json")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
