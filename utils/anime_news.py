"""Anime news: what's next for each anime in the Library, from AniList.

For every Library entry the anime is matched to an AniList entry once (by title, or
by the user's own pick), then its sequels are followed to the newest season, and
that season's state becomes one of:

    airing    the newest season is airing now (with the next episode's date)
    coming    a new season is announced (with its start date when there is one)
    waiting   the anime has finished but its source (manga, novel...) hasn't, so
              another season may come -- there's simply no news yet
    finished  the anime and its source have both finished, or it is an original
    paused    on hiatus or cancelled
    unknown   no AniList entry could be matched with confidence

AniList's public GraphQL API needs no key. Results are kept in
APP_DIR/anime_news.json and refreshed at most once a day (MAX_AGE), so a Library
of 30 anime costs about 60 requests a day, far inside the API's limit.

"finished" can't promise the whole story was animated: AniList has no such flag,
only that both the anime and its source have ended.

Pure logic, HTTP and file I/O only (no Qt), so it is unit-testable; `post` is the
one network call, and tests hand in a fake.
"""
import datetime
import difflib
import json
import os
import threading
import time
import urllib.error
import urllib.request

from utils.config import APP_DIR, APP_VERSION

API_URL = "https://graphql.anilist.co"
FILE = os.path.join(APP_DIR, "anime_news.json")
MAX_AGE = 24 * 3600          # seconds before a show is checked again
TIMEOUT = 15                 # seconds per request
REQUEST_GAP = 0.7            # seconds between requests: about 85 a minute at most
MAX_HOPS = 10                # sequels followed before giving up (and loops)
MATCH_THRESHOLD = 0.6        # how close a title must be to match on its own

AIRING, COMING, WAITING, FINISHED, PAUSED, UNKNOWN = (
    "airing", "coming", "waiting", "finished", "paused", "unknown")
KINDS = (AIRING, COMING, WAITING, FINISHED, PAUSED, UNKNOWN)
KIND_LABELS = {AIRING: "Airing now", COMING: "New season coming", WAITING: "Waiting for news",
               FINISHED: "Finished", PAUSED: "On hold", UNKNOWN: "Not matched"}

# Seasons proper. Films and specials are left out: "a new season" means a series.
SERIES_FORMATS = ("TV", "TV_SHORT", "ONA")

_MEDIA_FIELDS = """
  id siteUrl format status source episodes synonyms
  title { romaji english }
  startDate { year month day }
  endDate { year month day }
  nextAiringEpisode { episode airingAt }
"""
_SEARCH_QUERY = """
query ($search: String) {
  Page(perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {""" + _MEDIA_FIELDS + """
      title { native } coverImage { medium } }
  }
}"""
_MEDIA_QUERY = """
query ($id: Int) {
  Media(id: $id, type: ANIME) {""" + _MEDIA_FIELDS + """
    relations { edges { relationType node { type""" + _MEDIA_FIELDS + """} } }
  }
}"""

_lock = threading.Lock()


class NewsError(Exception):
    """AniList couldn't be reached or answered with an error."""


# ------------------------------------------------------------------ network

def post(query, variables, timeout=TIMEOUT):
    """One GraphQL request. Waits once and retries when AniList says to slow down."""
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    for attempt in range(2):
        req = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": f"AutoEpisodesDownloader/{APP_VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(min(60, int(e.headers.get("Retry-After") or 30)))
                continue
            raise NewsError(f"AniList answered HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NewsError(f"AniList couldn't be reached ({type(e).__name__})") from e
        if data.get("errors") and not data.get("data"):
            raise NewsError(str(data["errors"][0].get("message", "AniList error")))
        return data.get("data") or {}
    raise NewsError("AniList is busy; try again in a minute")


def search(query, fetch=post):
    """AniList entries for a title, best first: [media dict]."""
    data = fetch(_SEARCH_QUERY, {"search": query})
    return ((data.get("Page") or {}).get("media")) or []


def media(anilist_id, fetch=post):
    return fetch(_MEDIA_QUERY, {"id": int(anilist_id)}).get("Media")


# ------------------------------------------------------------------ matching

def _norm(title):
    from core.schedule import normalize_title
    return normalize_title(title)


def search_text(title):
    """What to ask AniList for a Library title: no season words, and run-together
    names split ("FateZero" -> "fate zero"), which AniList's search can't find."""
    import re
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title or "")
    return _norm(spaced) or (title or "").strip()


def display_title(m):
    t = (m or {}).get("title") or {}
    return t.get("english") or t.get("romaji") or "?"


def _names(m):
    """Every name AniList knows the entry by, and each one's short form before a
    subtitle: the sites often say "Frieren" for "Frieren: Beyond Journey's End"."""
    t = m.get("title") or {}
    names = [n for n in [t.get("english"), t.get("romaji"), *(m.get("synonyms") or [])] if n]
    short = [n.split(":")[0].split(" - ")[0] for n in names]
    return names + [s for s in short if s not in names]


def similarity(title, m):
    """0..1: how closely the Library title names this AniList entry."""
    want = _norm(title)
    if not want:
        return 0.0

    def ratio(a, b):
        # Spaces ignored too: "fatezero" and "fate zero" (Fate/Zero) are one name.
        return max(difflib.SequenceMatcher(None, a, b).ratio(),
                   difflib.SequenceMatcher(None, a.replace(" ", ""), b.replace(" ", "")).ratio())
    return max((ratio(want, _norm(n)) for n in _names(m)), default=0.0)


def best_match(title, candidates):
    """The candidate this Library title most likely is, or None when nothing is
    close enough. Series beat films and specials, and between equally close
    entries the earliest wins: following sequels from season 1 finds the newest."""
    scored = []
    for m in candidates:
        if m.get("format") not in SERIES_FORMATS:
            continue
        score = similarity(title, m)
        if score >= MATCH_THRESHOLD:
            start = m.get("startDate") or {}
            scored.append((-round(score, 2), start.get("year") or 9999, start.get("month") or 99, m))
    if not scored:
        return None
    scored.sort(key=lambda s: s[:3])
    return scored[0][3]


STATUS_WORDS = {"FINISHED": "Finished", "RELEASING": "Airing", "NOT_YET_RELEASED": "Not out yet",
                "HIATUS": "On hiatus", "CANCELLED": "Cancelled"}


def candidate_line(m):
    """'TV · 2011 · 13 episodes · Finished' for a pick list."""
    year = (m.get("startDate") or {}).get("year")
    fmt = {"TV_SHORT": "TV short", "MOVIE": "Film", "SPECIAL": "Special"}.get(
        m.get("format"), m.get("format") or "")
    eps = m.get("episodes")
    eps = f"{eps} episode{'s' if eps != 1 else ''}" if eps else ""
    return " · ".join(str(p) for p in (fmt, year, eps, STATUS_WORDS.get(m.get("status"), "")) if p)


def other_title(m):
    """The entry's second name, shown under the first: romaji under English, or
    native under romaji. '' when there is none."""
    t = m.get("title") or {}
    first = display_title(m)
    for name in (t.get("romaji"), t.get("native")):
        if name and name != first:
            return name
    return ""


# ------------------------------------------------------------------ the newest season

def _sequels(m):
    out = []
    for edge in ((m.get("relations") or {}).get("edges") or []):
        node = edge.get("node") or {}
        if (edge.get("relationType") == "SEQUEL" and node.get("type") == "ANIME"
                and node.get("format") in SERIES_FORMATS):
            out.append(node)
    start = lambda n: tuple((n.get("startDate") or {}).get(k) or 9999 for k in ("year", "month", "day"))
    return sorted(out, key=start)


def _source_status(m):
    """'FINISHED' / 'RELEASING' / ... of what the anime adapts, 'ORIGINAL', or None."""
    if (m.get("source") or "").upper() == "ORIGINAL":
        return "ORIGINAL"
    for edge in ((m.get("relations") or {}).get("edges") or []):
        node = edge.get("node") or {}
        if edge.get("relationType") == "SOURCE" or (
                edge.get("relationType") == "ADAPTATION" and node.get("type") == "MANGA"):
            return node.get("status")
    return None


def newest_season(anilist_id, fetch=post, gap=REQUEST_GAP):
    """(newest series entry, source status) following sequels from `anilist_id`.
    An announced season ends the walk without another request: it has no sequel."""
    current = media(anilist_id, fetch)
    if not current:
        raise NewsError("AniList has no entry with that id")
    seen = {current["id"]}
    source = _source_status(current)
    for _ in range(MAX_HOPS):
        nxt = next((s for s in _sequels(current) if s.get("id") not in seen), None)
        if nxt is None:
            break
        seen.add(nxt["id"])
        if nxt.get("status") == "NOT_YET_RELEASED":
            return nxt, source
        if gap:
            time.sleep(gap)
        current = media(nxt["id"], fetch) or nxt
        source = _source_status(current) or source
    return current, source


# ------------------------------------------------------------------ wording

def _date_text(d):
    """'3 April 2027', 'April 2027', '2027' or '' from AniList's partial date."""
    d = d or {}
    year, month, day = d.get("year"), d.get("month"), d.get("day")
    if not year:
        return ""
    # A date AniList holds wrongly (30 February, month 13) falls back to what of it
    # is valid, instead of failing the whole check.
    try:
        if month and day:
            return datetime.date(year, month, day).strftime("%d %B %Y").lstrip("0")
    except (TypeError, ValueError):
        pass
    try:
        if month:
            return datetime.date(year, month, 1).strftime("%B %Y")
    except (TypeError, ValueError):
        pass
    return str(year)


def _when_epoch(ts):
    """'Sat 7 Nov' for a time a week or so away."""
    d = datetime.datetime.fromtimestamp(int(ts))
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def describe(newest, source):
    """The state of the newest season, in words: a dict with kind, headline, detail,
    when (a short date for the row), key (what a change notice compares) and the
    AniList link."""
    name = display_title(newest)
    status = newest.get("status")
    base = {"season_title": name, "anilist_id": newest.get("id"),
            "site_url": newest.get("siteUrl") or f"https://anilist.co/anime/{newest.get('id')}"}
    if status == "RELEASING":
        nxt = newest.get("nextAiringEpisode") or {}
        started = _date_text(newest.get("startDate"))
        if nxt.get("airingAt"):
            headline = f"{name} is airing. Episode {nxt['episode']} comes out next."
            when = _when_epoch(nxt["airingAt"])
        else:
            headline, when = f"{name} is airing.", ""
        return {**base, "kind": AIRING, "headline": headline, "when": when,
                "detail": f"Started {started}" if started else "Airing now",
                "key": f"{AIRING}|{newest.get('id')}"}
    if status == "NOT_YET_RELEASED":
        date = _date_text(newest.get("startDate"))
        on = "on" if len(date.split()) == 3 else "in"     # on 2 October 2027, in October 2027
        return {**base, "kind": COMING, "when": date or "No date yet",
                "headline": f"{name} is coming {on} {date}." if date else f"{name} is coming.",
                "detail": "Announced" if date else "Announced, with no release date yet",
                "key": f"{COMING}|{newest.get('id')}|{date}"}
    if status in ("HIATUS", "CANCELLED"):
        word = "on hiatus" if status == "HIATUS" else "cancelled"
        return {**base, "kind": PAUSED, "headline": f"{name} is {word}.", "when": "",
                "detail": "AniList lists it as " + word, "key": f"{PAUSED}|{newest.get('id')}"}
    ended = _date_text(newest.get("endDate"))
    if source in ("FINISHED", "ORIGINAL"):
        detail = ("An original anime" if source == "ORIGINAL" else "The story it adapts has ended too")
        return {**base, "kind": FINISHED, "headline": "Finished series.", "when": "",
                "detail": f"{name} ended {ended}. {detail}." if ended else f"{detail}.",
                "key": f"{FINISHED}|{newest.get('id')}"}
    still = {"RELEASING": "The story it adapts is still going, so another season is possible.",
             "HIATUS": "The story it adapts is on hiatus."}.get(source, "")
    return {**base, "kind": WAITING, "headline": "No news about another season yet.", "when": "",
            "detail": " ".join(p for p in (f"{name} ended {ended}." if ended else f"{name} has ended.",
                                           still) if p),
            "key": f"{WAITING}|{newest.get('id')}"}


def change_message(title, old, new):
    """The notice for a show whose state changed, or None when it isn't news:
    a season announced, given a date, or starting to air."""
    if not old or not new or old.get("key") == new.get("key"):
        return None
    if old.get("kind") == UNKNOWN:
        return None           # found on AniList at last: nothing about it changed
    kind = new.get("kind")
    if kind == COMING:
        if old.get("kind") == COMING and old.get("anilist_id") == new.get("anilist_id"):
            when, before = new.get("when"), old.get("when")
            if not when or when == "No date yet":
                return None           # a date taken back isn't news
            # "October 2027" -> "2 October 2027" is the same date made exact;
            # anything else is the date changing.
            if before and before != "No date yet" and before not in when:
                return f"{title}: {new['season_title']} moved to {when}."
            return f"{title}: {new['season_title']} now has a date, {when}."
        return f"{title}: {new['season_title']} has been announced" + (
            f" for {new['when']}." if new.get("when") and new["when"] != "No date yet" else ".")
    if kind == AIRING and old.get("kind") != AIRING:
        return f"{title}: {new['season_title']} has started airing."
    return None


# ------------------------------------------------------------------ store

def _load():
    try:
        with open(FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("shows"), dict):
            # A damaged record is dropped (checked again), never trusted.
            shows = {}
            for url, rec in data["shows"].items():
                if isinstance(rec, dict):
                    if not isinstance(rec.get("state"), dict):
                        rec = dict(rec, state=None, checked_at=0)
                    try:
                        rec["checked_at"] = float(rec.get("checked_at") or 0)
                    except (TypeError, ValueError):
                        rec["checked_at"] = 0.0
                    shows[url] = rec
            data["shows"] = shows
            return data
    except (OSError, ValueError):
        pass
    return {"shows": {}, "checked_at": 0}


def _save(data):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def load():
    """{'shows': {library url: record}, 'checked_at': time of the last full check}."""
    with _lock:
        return _load()


def set_match(url, anilist_id, match_title):
    """The user's own pick for a Library show; never replaced by a title match.
    Its state is cleared, so the next check works it out for the new entry."""
    with _lock:
        data = _load()
        data["shows"][url] = {"anilist_id": int(anilist_id), "manual": True,
                              "match_title": match_title, "state": None, "checked_at": 0}
        _save(data)


def check_show(title, record, fetch=post, gap=REQUEST_GAP):
    """A fresh record for one Library show: matched (unless it already is) and its
    newest season described."""
    record = dict(record or {})
    if not record.get("anilist_id"):
        found = best_match(title, search(search_text(title), fetch))
        if found is None:
            record.update(anilist_id=None, match_title="", checked_at=time.time(),
                          state={"kind": UNKNOWN, "key": UNKNOWN, "when": "", "season_title": "",
                                 "headline": "Not found on AniList.", "site_url": "",
                                 "detail": "Use “Not the right anime?” to pick it yourself."})
            return record
        record.update(anilist_id=found["id"], match_title=display_title(found), manual=False)
        if gap:
            time.sleep(gap)
    newest, source = newest_season(record["anilist_id"], fetch, gap)
    record.update(state=describe(newest, source), checked_at=time.time())
    return record


def check(entries, force=False, fetch=post, gap=REQUEST_GAP, now=None, progress=None,
          stop=lambda: False):
    """Bring the news for Library `entries` up to date.

    Only shows not checked in MAX_AGE are fetched, unless `force`. Returns
    (records by url, [(title, change notice)], [(title, error)]). A show that
    fails keeps its last state; one failure doesn't stop the rest. `stop()`
    returning True ends the run between shows (the app closing).
    """
    now = time.time() if now is None else now
    with _lock:
        data = _load()
    shows = data["shows"]
    changes, errors = [], []
    def is_due(url):
        last = float((shows.get(url) or {}).get("checked_at") or 0)
        return force or not last or now - last >= MAX_AGE
    due = [e for e in entries if e.get("url") and is_due(e["url"])]
    for i, entry in enumerate(due):
        if stop():
            break
        url, title = entry["url"], entry.get("title") or "Anime"
        old = shows.get(url) or {}
        try:
            new = check_show(title, old, fetch, gap)
        except NewsError as e:
            errors.append((title, str(e)))
            continue
        new["checked_at"] = now
        msg = change_message(title, old.get("state"), new.get("state"))
        if msg:
            changes.append((title, msg))
            new["changed_at"] = now
        elif old.get("changed_at"):
            new["changed_at"] = old["changed_at"]
        with _lock:
            data = _load()                       # a pick made meanwhile wins
            if (data["shows"].get(url) or {}).get("manual") and not old.get("manual"):
                continue
            data["shows"][url] = new
            if i == len(due) - 1 and not errors:
                data["checked_at"] = now
            _save(data)
        shows[url] = new
        if progress:
            progress(i + 1, len(due))
        if gap and i < len(due) - 1:
            time.sleep(gap)
    return shows, changes, errors


def is_fresh(record, now=None, window=7 * 24 * 3600):
    """True for a few days after a show's news changed: the row says so."""
    now = time.time() if now is None else now
    try:
        changed = float((record or {}).get("changed_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(changed) and 0 <= now - changed < window
