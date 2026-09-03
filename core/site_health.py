"""Notice when a supported site changes under us, and absorb the changes we can.

The app already loads these sites constantly -- search, episode detection, the
schedule scrape, every download. This module watches those page loads instead of
adding a checker of its own, so it costs no extra requests and runs only while the
app runs.

Two different problems, and only one of them can be repaired automatically:

  Host moves ARE repairable. animerco moved eta.animerco.org -> det.animerco.org
  and nothing noticed: every per-site table is keyed by exact host, so the download
  flow, the schedule URL, the schedule match strategy and the favicon all missed at
  once, each failing quietly in its own way. Episode detection kept only links whose
  host matched the URL it had requested, so it found no seasons and loaded every
  animerco anime as a single fake episode. site_key() collapses a host to the site's
  name ("det.animerco.org" and "eta.animerco.org" are both "animerco"), so a move
  stops mattering, and an observed redirect is remembered for DNS pinning.

  Layout changes are NOT repairable. If a site rewrites its episode markup there is
  no honest way to invent new selectors at runtime. What this module does instead is
  refuse to let that failure stay silent: a page that loads fine and yields nothing
  is recorded as a break, so the app can say so rather than reporting "no episodes".

Nothing here ever raises: a health-tracking failure must not break a download.
"""

import json
import os
import threading
import time
from urllib.parse import urlparse

STATE_FILE = "site_health.json"

# Two-label public suffixes, so "example.co.uk" keys as "example". Only what these
# sites could plausibly move to -- the fallback (second-to-last label) is already
# right for every ordinary domain.
_TWO_PART_SUFFIXES = {"co.uk", "com.br", "co.jp", "com.tr", "co.in", "com.au", "co.kr"}

# A page that loaded properly but produced nothing is only evidence of a real break
# if there was actually content on it. Below this, assume a block page or a bad load
# and stay quiet rather than crying wolf.
MIN_ANCHORS_FOR_A_REAL_PAGE = 20

_lock = threading.RLock()
_state = None


def host_of(url):
    """Bare host for a URL or host string, without scheme or leading www."""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def site_key(url_or_host):
    """The site's name, independent of subdomain and TLD.

    "eta.animerco.org", "det.animerco.org" and "animerco.org" are all "animerco".
    Per-site tables stay keyed by their real entry host -- this is what lookups
    compare, so a mirror move cannot orphan them.
    """
    host = host_of(url_or_host)
    if not host:
        return ""
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    if ".".join(parts[-2:]) in _TWO_PART_SUFFIXES and len(parts) >= 3:
        return parts[-3]
    return parts[-2]


def lookup(table, url_or_host, default=None):
    """Read a per-site table by site name rather than exact host.

    Tries the exact host first so an explicit entry always wins, then falls back to
    any key naming the same site.
    """
    if not table:
        return default
    host = host_of(url_or_host)
    if host in table:
        return table[host]
    key = site_key(host)
    if not key:
        return default
    for candidate, value in table.items():
        if site_key(candidate) == key:
            return value
    return default


# ---------------------------------------------------------------- persistence

def _path():
    from utils.config import APP_DIR
    return os.path.join(APP_DIR, STATE_FILE)


def _load():
    global _state
    if _state is not None:
        return _state
    data = {}
    try:
        with open(_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("moves", {})    # site key -> where it redirects to now
    data.setdefault("breaks", {})   # site key -> detection stopped working
    _state = data
    return _state


def _save():
    """Write the state file. Best-effort: losing it only costs the learned hosts."""
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass


def reset_for_tests():
    """Drop the in-memory state so a test starts from the file it just wrote."""
    global _state
    with _lock:
        _state = None


# ---------------------------------------------------------------- observations

def record_landing(requested_url, landed_url):
    """Note where a page load actually ended up. Returns the new host if the site
    has moved to one we had not already recorded, else "".

    Only a move WITHIN the same site counts. A download link that leaves for
    mediafire or Google Drive is not the anime site relocating, and treating it as
    one would poison the state with file hosts.
    """
    try:
        want, got = host_of(requested_url), host_of(landed_url)
        if not want or not got or want == got:
            return ""
        key = site_key(want)
        if not key or key != site_key(got):
            return ""      # left for another site entirely -- not a move
        with _lock:
            state = _load()
            rec = state["moves"].get(key)
            if rec and rec.get("host") == got:
                rec["last"] = int(time.time())
                rec["seen"] = int(rec.get("seen", 1)) + 1
                _save()
                return ""
            state["moves"][key] = {"host": got, "from": want, "seen": 1,
                                   "first": int(time.time()), "last": int(time.time())}
            _save()
        return got
    except Exception:
        return ""


def record_detection(url, anchor_count, found):
    """Record whether detection worked on a page that loaded.

    `found` is how many entries came out. A page carrying real content that yields
    nothing is the signature of a layout change -- the failure that previously
    surfaced to the user as "no episodes detected", indistinguishable from an anime
    that genuinely has none.
    """
    try:
        key = site_key(url)
        if not key:
            return
        with _lock:
            state = _load()
            if found > 0:
                if state["breaks"].pop(key, None) is not None:
                    _save()          # recovered -- stop warning about it
                return
            if anchor_count < MIN_ANCHORS_FOR_A_REAL_PAGE:
                return               # block page or failed load, not a layout change
            rec = state["breaks"].setdefault(
                key, {"count": 0, "first": int(time.time()), "anchors": anchor_count})
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["last"] = int(time.time())
            rec["anchors"] = anchor_count
            _save()
    except Exception:
        pass


# ---------------------------------------------------------------- consumers

def learned_hosts():
    """Hosts observed as redirect targets, for DNS pinning.

    A site that moves is exactly the case where the new host is missing from the
    pinned list, on precisely the networks that need pinning to reach it at all.
    """
    try:
        with _lock:
            state = _load()
            return tuple(r["host"] for r in state["moves"].values() if r.get("host"))
    except Exception:
        return ()


def broken_sites(min_count=2):
    """Site keys whose detection has come up empty on a real page repeatedly.

    Requires more than one occurrence by default: a single empty page is far more
    often one odd anime than a site rewrite.
    """
    try:
        with _lock:
            state = _load()
            return tuple(k for k, r in state["breaks"].items()
                         if int(r.get("count", 0)) >= min_count)
    except Exception:
        return ()
