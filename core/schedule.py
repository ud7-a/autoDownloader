"""Weekly release schedules -- which day each anime's new episode drops.

Both supported sites publish a schedule page, but structure them differently:

* witanime lists Arabic <h3> day headings followed by /anime/<slug>/ links, so an
  entry can be matched by URL exactly.
* animerco uses tabbed panels whose ids are English weekday names, holding cards
  that link to /seasons/<slug>/ -- never to /animes/, which is what the watchlist
  stores. Those have to be matched on title instead.

Parsing lives here, away from Qt, so the matching rules can be unit-tested.
"""

import re
import threading
import time

DAY_ORDER = ["saturday", "sunday", "monday", "tuesday",
             "wednesday", "thursday", "friday"]

DAY_LABELS = {
    "saturday": "Saturday", "sunday": "Sunday", "monday": "Monday",
    "tuesday": "Tuesday", "wednesday": "Wednesday", "thursday": "Thursday",
    "friday": "Friday",
}

# Arabic weekday names as the sites spell them (both hamza spellings appear).
ARABIC_DAYS = {
    "السبت": "saturday",
    "الأحد": "sunday", "الاحد": "sunday",
    "الإثنين": "monday", "الاثنين": "monday", "الاثنين ": "monday",
    "الثلاثاء": "tuesday",
    "الأربعاء": "wednesday", "الاربعاء": "wednesday",
    "الخميس": "thursday",
    "الجمعة": "friday",
}

SCHEDULE_URLS = {
    "witanime.life": "https://witanime.life/مواعيد-الحلقات/",
    "eta.animerco.org": "https://eta.animerco.org/schedule/",
}

# How an entry may be matched against each site's schedule.
#
# "url"   -- witanime lists /anime/<slug>/ links, the exact page a search result
#            points at, so the URL decides. Titles must NOT be used here: each
#            season is its own anime page ("Grand Blue", "Grand Blue Season 3"),
#            and by title they are indistinguishable, which would mark every
#            season of a show as airing whenever any one of them is.
# "title" -- animerco only links /seasons/, never the /animes/ page a search
#            result uses, so its titles are the only thing available to compare.
SCHEDULE_MATCH = {
    "witanime.life": "url",
    "eta.animerco.org": "title",
}

_SEASON_NUMBER = re.compile(
    r"(?:season|part|cour|الموسم|الجزء)\s*(\d+)|\b(\d+)(?:st|nd|rd|th)\s+season\b",
    re.IGNORECASE)


def season_number(title):
    """The season number stated in a title, or None. 'Grand Blue Season 3' -> 3."""
    m = _SEASON_NUMBER.search(title or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))

# witanime: walk the document in order; each anime link belongs to the most recent
# day heading above it.
_WITANIME_JS = r"""
var DAYS = /^(السبت|الأحد|الاحد|الإثنين|الاثنين|الثلاثاء|الأربعاء|الاربعاء|الخميس|الجمعة)$/;
var out = [], current = null, seen = {};
var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
while (walker.nextNode()) {
    var el = walker.currentNode;
    if (el.tagName === 'H3') {
        var t = (el.textContent || '').trim();
        if (DAYS.test(t)) current = t;
    } else if (el.tagName === 'A' && current && /\/anime\//.test(el.href || '')) {
        var href = el.href.split('#')[0];
        if (seen[href]) continue;
        var name = (el.textContent || '').trim() || (el.getAttribute('title') || '').trim();
        if (name) { seen[href] = 1; out.push({day: current, title: name, url: href}); }
    }
}
return out;
"""

# animerco: each tab panel's id is the weekday; cards link to a season page and
# carry the readable name in the title attribute.
_ANIMERCO_JS = r"""
var out = [];
document.querySelectorAll('.tab-content[id]').forEach(function (panel) {
    var day = panel.id;
    panel.querySelectorAll('a[href*="/seasons/"]').forEach(function (a) {
        var title = (a.getAttribute('title') || a.textContent || '').trim();
        if (!title) return;
        out.push({day: day, title: title, url: a.href.split('#')[0]});
    });
});
return out;
"""

_SITE_SCRIPTS = {
    "witanime.life": _WITANIME_JS,
    "eta.animerco.org": _ANIMERCO_JS,
}

# Trailing season/part markers differ between a watchlist entry and the schedule
# listing for the same show, so they are stripped before comparing.
_SEASON_NOISE = re.compile(
    r"\b(season|s|part|cour|الموسم|الجزء)\s*\d+\b|\b(2nd|3rd|4th|5th)\s+season\b|"
    r"\bfinal\s+season\b|\bseason\b", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_title(title):
    """Reduce a title to a comparable core: no season markers, punctuation or case."""
    text = (title or "").lower()
    text = _SEASON_NOISE.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def canonical_day(raw):
    """Map either an Arabic weekday or an English panel id to our canonical key."""
    key = (raw or "").strip()
    if key in ARABIC_DAYS:
        return ARABIC_DAYS[key]
    low = key.lower()
    return low if low in DAY_LABELS else None


def fetch_schedule(driver, domain, settle=2.5):
    """Scrape one site's schedule. Returns [{"day","title","url"}] with canonical days."""
    # Looked up by site name, not exact host: animerco redirects eta.* -> det.*, and
    # an exact-host miss here made the scrape return [] -- swallowed by the caller,
    # so every entry silently lost its release day.
    from core.site_health import lookup as site_lookup
    script = site_lookup(_SITE_SCRIPTS, domain)
    url = site_lookup(SCHEDULE_URLS, domain)
    if not script or not url:
        return []
    driver.get(url)
    time.sleep(settle)          # both pages render server-side; a short settle is enough
    try:
        raw = driver.execute_script(script) or []
    except Exception:
        return []
    items = []
    for row in raw:
        day = canonical_day(row.get("day"))
        if day and row.get("title"):
            items.append({"day": day, "title": row["title"], "url": row.get("url", "")})
    return items


_cache = {"items": [], "at": 0.0}
_cache_lock = threading.Lock()

# A weekly schedule changes at most once a day, so a copy this old is still correct
# and saves loading two pages -- which is slow enough to be felt if it happens while
# the user is searching.
CACHE_MAX_AGE = 12 * 3600


def _disk_cache_path():
    import os
    from utils.config import APP_DIR
    return os.path.join(APP_DIR, "schedule_cache.json")


def cached_items(max_age=CACHE_MAX_AGE):
    """Schedules already on hand, from memory or the last session's file. [] if stale.

    Kept synchronous and cheap so callers can use it without starting a browser.
    """
    with _cache_lock:
        if _cache["items"] and (time.time() - _cache["at"]) < max_age:
            return list(_cache["items"])
    try:
        import json
        with open(_disk_cache_path(), encoding="utf-8") as f:
            blob = json.load(f)
        if time.time() - float(blob.get("at", 0)) < max_age and blob.get("items"):
            with _cache_lock:
                _cache["items"] = list(blob["items"])
                _cache["at"] = float(blob["at"])
            return list(blob["items"])
    except Exception:
        pass
    return []


def _store(items):
    now = time.time()
    with _cache_lock:
        _cache["items"] = list(items)
        _cache["at"] = now
    try:
        import json
        with open(_disk_cache_path(), "w", encoding="utf-8") as f:
            json.dump({"at": now, "items": items}, f, ensure_ascii=False)
    except Exception:
        pass          # an unwritable cache just means we fetch again next time


def load_all(driver, max_age=CACHE_MAX_AGE):
    """Both sites' schedules, reusing a cached copy when one is recent enough."""
    ready = cached_items(max_age)
    if ready:
        return ready
    items = []
    for domain in SCHEDULE_URLS:
        try:
            items += fetch_schedule(driver, domain)
        except Exception:
            continue          # one site being unreachable shouldn't lose the other
    if items:
        _store(items)
    return items


def is_scheduled(entry, items):
    """True if this title appears in either site's weekly schedule (i.e. it is airing)."""
    return find_day(entry, items) is not None


def find_day(entry, items, season=None):
    """Which day this entry airs on, or None.

    The strategy depends on the site (see SCHEDULE_MATCH). `season` narrows a title
    match to one specific season number, so only the season that is actually airing
    matches rather than every season of the show.
    """
    url = (entry.get("url") or "").rstrip("/")
    from core.site_health import lookup as site_lookup
    # By site name: a moved host would otherwise fall to the "title" default, which
    # is the wrong strategy for witanime and matches the wrong show without a word.
    strategy = site_lookup(SCHEDULE_MATCH, url, "title")

    if url:                                  # an exact URL hit always wins
        for item in items:
            if item.get("url", "").rstrip("/") == url:
                return item["day"]

    if strategy == "url":
        # This site's schedule carries the very URLs search returns, so a miss above
        # is a genuine "not airing" -- guessing by title here would tag every season.
        return None

    target = normalize_title(entry.get("title"))
    if not target:
        return None

    def season_ok(item):
        if season is None:
            return True
        return season_number(item["title"]) == season

    for item in items:                       # exact match on the normalized core
        if normalize_title(item["title"]) == target and season_ok(item):
            return item["day"]

    if len(target) >= 8:                     # then a cautious containment match
        for item in items:
            other = normalize_title(item["title"])
            if not other or not season_ok(item):
                continue
            if (target in other) or (other in target and len(other) >= 8):
                return item["day"]
    return None


def is_season_scheduled(anime_title, season_label, items):
    """True if this particular season of the anime is the one currently airing."""
    return find_day({"title": anime_title, "url": ""}, items,
                    season=season_number(season_label)) is not None
