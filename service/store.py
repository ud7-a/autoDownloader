"""Every SQL statement in the service.

Kept in one module because the checker process (service/checker.py) needs the same queries;
splitting them would mean two subtly different sets of the same logic.
"""

from contextlib import contextmanager
import hmac
import os
import sqlite3
import time
import uuid

from service import crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id            TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL,
    webhook_enc   BLOB,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS anime (
    url             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    release_day     TEXT DEFAULT '',
    last_seen_max   INTEGER NOT NULL DEFAULT 0,
    last_checked_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS follows (
    subscriber_id  TEXT NOT NULL,
    anime_url      TEXT NOT NULL,
    notified_max   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (subscriber_id, anime_url),
    FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE,
    FOREIGN KEY (anime_url)     REFERENCES anime(url)      ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_anime_last_checked ON anime(last_checked_at);
CREATE INDEX IF NOT EXISTS idx_anime_release_day ON anime(release_day);
CREATE INDEX IF NOT EXISTS idx_follows_anime ON follows(anime_url);
"""


def db_path() -> str:
    return os.environ.get("AED_NOTIFY_DB") or "notify.db"


def _init_db(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA foreign_keys = ON")
    # Check if anime table exists and needs migration before running full schema script
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(anime)").fetchall()]
        if cols and "release_day" not in cols:
            db.execute("ALTER TABLE anime ADD COLUMN release_day TEXT DEFAULT ''")
    except Exception:
        pass
    db.executescript(SCHEMA)


@contextmanager
def get_db():
    path = db_path()
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(path)
    _init_db(db)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def connect() -> sqlite3.Connection:
    """Legacy helper returning a connection. Prefer `with get_db() as db:`."""
    path = db_path()
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    db = sqlite3.connect(path)
    _init_db(db)
    return db


def reset_for_tests() -> None:
    """Drop every row. Refuses to run against a database that isn't a test one."""
    path = db_path()
    if "aed-notify-tests" not in path:
        raise RuntimeError(f"refusing to wipe a non-test database: {path}")
    with get_db() as db:
        for table in ("follows", "anime", "subscribers"):
            db.execute(f"DELETE FROM {table}")


def create_subscriber(webhook_url: str) -> tuple[str, str]:
    sid = uuid.uuid4().hex
    token = crypto.new_token()
    with get_db() as db:
        db.execute(
            "INSERT INTO subscribers (id, token_hash, webhook_enc, created_at) VALUES (?,?,?,?)",
            (sid, crypto.hash_token(token), crypto.encrypt_webhook(webhook_url), int(time.time())))
    return sid, token


def authenticate(subscriber_id: str, token: str) -> bool:
    with get_db() as db:
        row = db.execute("SELECT token_hash FROM subscribers WHERE id=?",
                         (subscriber_id,)).fetchone()
    if not row:
        return False
    # Constant-time comparison prevents timing attacks that leak the hash byte-by-byte
    return hmac.compare_digest(row[0], crypto.hash_token(token))


def get_webhook(subscriber_id: str) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT webhook_enc FROM subscribers WHERE id=?",
                         (subscriber_id,)).fetchone()
    if not row or not row[0]:
        return None
    return crypto.decrypt_webhook(row[0])


def set_webhook(subscriber_id: str, webhook_url: str) -> None:
    with get_db() as db:
        db.execute("UPDATE subscribers SET webhook_enc=? WHERE id=?",
                   (crypto.encrypt_webhook(webhook_url), subscriber_id))


def delete_subscriber(subscriber_id: str) -> None:
    """Remove the subscriber and everything belonging to them."""
    with get_db() as db:
        db.execute("DELETE FROM follows WHERE subscriber_id=?", (subscriber_id,))
        db.execute("DELETE FROM subscribers WHERE id=?", (subscriber_id,))


def replace_follows(subscriber_id: str, items: list[dict]) -> int:
    """Set exactly what this subscriber follows.

    Anime rows are upserted and shared across subscribers: multiple subscribers
    following the same show produce one anime row, which is what lets the checker
    scrape it once per cycle. An existing anime row keeps its last_seen_max.
    """
    with get_db() as db:
        # Fetch existing notified_max values for this subscriber so updating watchlist doesn't lose them
        existing_follows = dict(db.execute(
            "SELECT anime_url, notified_max FROM follows WHERE subscriber_id=?",
            (subscriber_id,)).fetchall())

        db.execute("DELETE FROM follows WHERE subscriber_id=?", (subscriber_id,))
        for item in items:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            title = (item.get("title") or url)[:200]
            release_day = (item.get("release_day") or "").strip().lower()
            # Upsert anime row
            db.execute(
                "INSERT INTO anime (url, title, release_day, last_seen_max, last_checked_at) "
                "VALUES (?,?,?,0,0) ON CONFLICT(url) DO UPDATE SET "
                "title = CASE WHEN excluded.title != '' THEN excluded.title ELSE anime.title END, "
                "release_day = CASE WHEN excluded.release_day != '' THEN excluded.release_day ELSE anime.release_day END",
                (url, title, release_day))

            # Initial notified_max is preserved if previously followed, else seeded from anime's last_seen_max
            # to avoid spamming the user on initial follow
            if url in existing_follows:
                prev_max = existing_follows[url]
            else:
                anime_row = db.execute("SELECT last_seen_max FROM anime WHERE url=?", (url,)).fetchone()
                prev_max = anime_row[0] if anime_row else 0

            db.execute(
                "INSERT OR REPLACE INTO follows (subscriber_id, anime_url, notified_max) "
                "VALUES (?, ?, ?)",
                (subscriber_id, url, prev_max))

        count = db.execute("SELECT COUNT(*) FROM follows WHERE subscriber_id=?",
                           (subscriber_id,)).fetchone()[0]
    return count


def followers_of(anime_url: str) -> list[str]:
    with get_db() as db:
        return [r[0] for r in db.execute(
            "SELECT subscriber_id FROM follows WHERE anime_url=?", (anime_url,))]


def due_anime(today_day: str = None, limit: int = 200) -> list[dict]:
    """Anime due for checking. If today_day is given (e.g. 'saturday'), filters to anime
    airing today or anime with no day assigned yet (so newly added shows aren't missed).
    Least recently checked first.
    """
    with get_db() as db:
        if today_day:
            rows = db.execute(
                "SELECT url, title, release_day, last_seen_max, last_checked_at FROM anime "
                "WHERE (release_day = '' OR release_day IS NULL OR LOWER(release_day) = ?) "
                "ORDER BY last_checked_at ASC LIMIT ?", (today_day.lower(), limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT url, title, release_day, last_seen_max, last_checked_at FROM anime "
                "ORDER BY last_checked_at ASC LIMIT ?", (limit,)).fetchall()

    return [{"url": r[0], "title": r[1], "release_day": r[2], "last_seen_max": r[3], "last_checked_at": r[4]}
            for r in rows]


def update_anime_progress(anime_url: str, max_episode: int) -> None:
    now = int(time.time())
    with get_db() as db:
        db.execute(
            "UPDATE anime SET last_seen_max = MAX(last_seen_max, ?), last_checked_at = ? WHERE url = ?",
            (max_episode, now, anime_url))


def advance_notified_max(subscriber_id: str, anime_url: str, new_max: int) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE follows SET notified_max = MAX(notified_max, ?) WHERE subscriber_id = ? AND anime_url = ?",
            (new_max, subscriber_id, anime_url))


def subscribers_to_notify(anime_url: str, current_max: int) -> list[tuple[str, str, int]]:
    """Returns [(subscriber_id, webhook_url, notified_max), ...] for followers needing an alert."""
    results = []
    with get_db() as db:
        rows = db.execute(
            "SELECT f.subscriber_id, s.webhook_enc, f.notified_max "
            "FROM follows f "
            "JOIN subscribers s ON f.subscriber_id = s.id "
            "WHERE f.anime_url = ? AND f.notified_max < ?",
            (anime_url, current_max)).fetchall()

    for sid, enc_wh, notif_max in rows:
        if enc_wh:
            try:
                wh_url = crypto.decrypt_webhook(enc_wh)
                results.append((sid, wh_url, notif_max))
            except Exception:
                pass
    return results


def prune_orphan_anime() -> int:
    """Drop anime nobody follows any more, so the checker stops scraping them."""
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM anime WHERE url NOT IN (SELECT DISTINCT anime_url FROM follows)")
        return cur.rowcount
