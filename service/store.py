"""Every SQL statement in the service.

Kept in one module because the checker process (service/checker.py) needs the same
queries; splitting them would mean two subtly different sets of the same logic.

Postgres, not SQLite. The service runs on a free Render instance, which has no
persistent disk -- the database used to sit on /tmp and was wiped on every restart or
idle spin-down. That data loss is what the auto-provisioning in authenticate() existed
to hide, and it accepted any id and token of roughly the right shape to do it.

Two environment variables matter:
  DATABASE_URL        connection string (use Supabase's *session pooler* host: the
                      direct db.*.supabase.co host is IPv6-only and unreachable from
                      Render and most home connections)
  AED_NOTIFY_SCHEMA   Postgres schema to use, default "public". Tests set this to an
                      isolated schema so reset_for_tests() can never touch live data.
"""

from contextlib import contextmanager
import hashlib
import hmac
import os
import threading
import time

import psycopg

from service import crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id             TEXT PRIMARY KEY,
    token_hash     TEXT NOT NULL,
    webhook_enc    BYTEA,
    created_at     BIGINT NOT NULL,
    last_heartbeat BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS anime (
    url             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    release_day     TEXT DEFAULT '',
    last_seen_max   BIGINT NOT NULL DEFAULT 0,
    last_checked_at BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS follows (
    subscriber_id  TEXT NOT NULL,
    anime_url      TEXT NOT NULL,
    notified_max   BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (subscriber_id, anime_url),
    FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE,
    FOREIGN KEY (anime_url)     REFERENCES anime(url)      ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS commands (
    id            BIGSERIAL PRIMARY KEY,
    subscriber_id TEXT NOT NULL,
    anime_url     TEXT NOT NULL,
    anime_title   TEXT NOT NULL,
    episodes      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    BIGINT NOT NULL,
    FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_anime_last_checked ON anime(last_checked_at);
CREATE INDEX IF NOT EXISTS idx_anime_release_day ON anime(release_day);
CREATE INDEX IF NOT EXISTS idx_follows_anime ON follows(anime_url);
CREATE INDEX IF NOT EXISTS idx_commands_sub ON commands(subscriber_id, status);
"""

# Columns added after the first release. Postgres supports IF NOT EXISTS here, so this
# is simply idempotent rather than the PRAGMA table_info dance SQLite needed.
MIGRATIONS = (
    "ALTER TABLE anime ADD COLUMN IF NOT EXISTS release_day TEXT DEFAULT ''",
    "ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS last_heartbeat BIGINT DEFAULT 0",
)

_schema_ready = False
_schema_lock = threading.Lock()


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def schema_name() -> str:
    return (os.environ.get("AED_NOTIFY_SCHEMA") or "public").strip() or "public"


def _ensure_schema(conn) -> None:
    """Create tables once per process, not once per query.

    The SQLite version re-ran the whole schema on every connection, which was free
    against a local file. Against a pooled network database that would add several
    round trips to every single call.
    """
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        name = schema_name()
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')
        conn.execute(f'SET search_path TO "{name}"')
        conn.execute(SCHEMA)
        for statement in MIGRATIONS:
            conn.execute(statement)
        conn.commit()
        _schema_ready = True


@contextmanager
def get_db():
    conn = psycopg.connect(dsn(), connect_timeout=20)
    try:
        conn.execute(f'SET search_path TO "{schema_name()}"')
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def connect():
    """Legacy helper returning a connection. Prefer `with get_db() as db:`."""
    conn = psycopg.connect(dsn(), connect_timeout=20)
    conn.execute(f'SET search_path TO "{schema_name()}"')
    _ensure_schema(conn)
    return conn


def reset_for_tests() -> None:
    """Drop every row. Refuses to run against the live schema.

    Tests and production share one database on the free tier, so the guard is the
    schema rather than a file path: AED_NOTIFY_SCHEMA must name something other than
    public before this will delete anything.
    """
    name = schema_name()
    if name == "public":
        raise RuntimeError(
            "refusing to wipe schema 'public' -- set AED_NOTIFY_SCHEMA to a test schema")
    with get_db() as db:
        db.execute("TRUNCATE commands, follows, anime, subscribers RESTART IDENTITY CASCADE")


def create_subscriber(webhook_url: str) -> tuple[str, str]:
    # Deterministic subscriber ID derived from webhook URL, so a re-registration after
    # data loss returns the same identity and old Discord links keep working.
    sid = hashlib.sha256(webhook_url.strip().encode("utf-8")).hexdigest()[:32]
    token = crypto.new_token()
    now = int(time.time())
    with get_db() as db:
        db.execute(
            "INSERT INTO subscribers (id, token_hash, webhook_enc, created_at, last_heartbeat) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET token_hash = EXCLUDED.token_hash, "
            "webhook_enc = EXCLUDED.webhook_enc, last_heartbeat = EXCLUDED.last_heartbeat",
            (sid, crypto.hash_token(token), crypto.encrypt_webhook(webhook_url), now, now)
        )
    return sid, token


def authenticate(subscriber_id: str, token: str) -> bool:
    """True only when this subscriber exists and the token matches its stored hash.

    There is deliberately no recovery path here. This used to create a subscriber for
    any id and token of roughly the right length, so that clients could survive the
    database being wiped on an ephemeral host -- which meant anyone could authenticate
    as anyone, and the auto-created row had no webhook, so it could never be notified
    anyway. A second branch adopted whatever token was offered for a row whose hash was
    blank, making such rows claimable by the first caller to name one.

    Recovery belongs to the client: it re-registers with its webhook (see
    cloud_recover_identity in utils/config.py), which proves possession of something
    the server can actually check.
    """
    if not (subscriber_id and token):
        return False
    with get_db() as db:
        row = db.execute("SELECT token_hash FROM subscribers WHERE id=%s",
                         (subscriber_id,)).fetchone()
    if not row or not row[0]:
        return False
    return hmac.compare_digest(row[0], crypto.hash_token(token))


def get_webhook(subscriber_id: str) -> str | None:
    with get_db() as db:
        row = db.execute("SELECT webhook_enc FROM subscribers WHERE id=%s",
                         (subscriber_id,)).fetchone()
    if not row or not row[0]:
        return None
    return crypto.decrypt_webhook(bytes(row[0]))


def set_webhook(subscriber_id: str, webhook_url: str) -> None:
    with get_db() as db:
        db.execute("UPDATE subscribers SET webhook_enc=%s WHERE id=%s",
                   (crypto.encrypt_webhook(webhook_url), subscriber_id))


def delete_subscriber(subscriber_id: str) -> None:
    """Remove the subscriber and everything belonging to them."""
    with get_db() as db:
        db.execute("DELETE FROM follows WHERE subscriber_id=%s", (subscriber_id,))
        db.execute("DELETE FROM subscribers WHERE id=%s", (subscriber_id,))


def replace_follows(subscriber_id: str, items: list[dict]) -> int:
    """Set exactly what this subscriber follows.

    Anime rows are upserted and shared across subscribers: multiple subscribers
    following the same show produce one anime row, which is what lets the checker
    scrape it once per cycle. An existing anime row keeps its last_seen_max.
    """
    with get_db() as db:
        # Keep existing notified_max values so re-syncing a watchlist doesn't replay
        # episodes the subscriber has already been told about.
        existing_follows = dict(db.execute(
            "SELECT anime_url, notified_max FROM follows WHERE subscriber_id=%s",
            (subscriber_id,)).fetchall())

        db.execute("DELETE FROM follows WHERE subscriber_id=%s", (subscriber_id,))
        for item in items:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            title = (item.get("title") or url)[:200]
            release_day = (item.get("release_day") or "").strip().lower()
            seen_max = max(0, int(item.get("seen_max") or 0))

            # GREATEST, not MAX: in Postgres MAX is an aggregate, and using it here
            # would not error -- it would quietly corrupt last_seen_max, which decides
            # who gets notified.
            db.execute(
                "INSERT INTO anime (url, title, release_day, last_seen_max, last_checked_at) "
                "VALUES (%s, %s, %s, %s, 0) ON CONFLICT (url) DO UPDATE SET "
                "title = CASE WHEN EXCLUDED.title != '' THEN EXCLUDED.title ELSE anime.title END, "
                "release_day = CASE WHEN EXCLUDED.release_day != '' THEN EXCLUDED.release_day ELSE anime.release_day END, "
                "last_seen_max = GREATEST(anime.last_seen_max, EXCLUDED.last_seen_max)",
                (url, title, release_day, seen_max))

            if url in existing_follows and existing_follows[url] > 0:
                prev_max = max(existing_follows[url], seen_max)
            else:
                prev_max = seen_max

            db.execute(
                "INSERT INTO follows (subscriber_id, anime_url, notified_max) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (subscriber_id, anime_url) DO UPDATE SET "
                "notified_max = EXCLUDED.notified_max",
                (subscriber_id, url, prev_max))

        count = db.execute("SELECT COUNT(*) FROM follows WHERE subscriber_id=%s",
                           (subscriber_id,)).fetchone()[0]
    return count


def followers_of(anime_url: str) -> list[str]:
    with get_db() as db:
        return [r[0] for r in db.execute(
            "SELECT subscriber_id FROM follows WHERE anime_url=%s", (anime_url,)).fetchall()]


def due_anime(today_days: list[str] | str | None = None, limit: int = 200,
              today_day: str | None = None) -> list[dict]:
    """Anime due for checking. If today_days or today_day is given (e.g. ['tuesday',
    'monday']), filters to anime airing on those days or anime with no day assigned
    yet. Least recently checked first.
    """
    days_arg = today_days if today_days is not None else today_day
    with get_db() as db:
        if days_arg:
            if isinstance(days_arg, str):
                days_list = [days_arg.lower()]
            else:
                days_list = [d.lower() for d in days_arg if d]
            placeholders = ",".join("%s" for _ in days_list)
            query = (
                "SELECT url, title, release_day, last_seen_max, last_checked_at FROM anime "
                f"WHERE (release_day = '' OR release_day IS NULL OR LOWER(release_day) IN ({placeholders})) "
                "ORDER BY last_checked_at ASC LIMIT %s"
            )
            rows = db.execute(query, (*days_list, limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT url, title, release_day, last_seen_max, last_checked_at FROM anime "
                "ORDER BY last_checked_at ASC LIMIT %s", (limit,)).fetchall()

    return [{"url": r[0], "title": r[1], "release_day": r[2],
             "last_seen_max": r[3], "last_checked_at": r[4]} for r in rows]


def update_anime_progress(anime_url: str, max_episode: int) -> None:
    now = int(time.time())
    with get_db() as db:
        db.execute(
            "UPDATE anime SET last_seen_max = GREATEST(last_seen_max, %s), "
            "last_checked_at = %s WHERE url = %s",
            (max_episode, now, anime_url))


def advance_notified_max(subscriber_id: str, anime_url: str, new_max: int) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE follows SET notified_max = GREATEST(notified_max, %s) "
            "WHERE subscriber_id = %s AND anime_url = %s",
            (new_max, subscriber_id, anime_url))


def subscribers_to_notify(anime_url: str, current_max: int) -> list[tuple[str, str, int]]:
    """Returns [(subscriber_id, webhook_url, notified_max), ...] for followers needing an alert."""
    results = []
    with get_db() as db:
        rows = db.execute(
            "SELECT f.subscriber_id, s.webhook_enc, f.notified_max "
            "FROM follows f "
            "JOIN subscribers s ON f.subscriber_id = s.id "
            "WHERE f.anime_url = %s AND f.notified_max < %s",
            (anime_url, current_max)).fetchall()

    for sid, enc_wh, notif_max in rows:
        if enc_wh:
            try:
                results.append((sid, crypto.decrypt_webhook(bytes(enc_wh)), notif_max))
            except Exception:
                # A webhook encrypted under a previous AED_NOTIFY_KEY cannot be read.
                # Skip that subscriber rather than failing the whole cycle.
                pass
    return results


def prune_orphan_anime() -> int:
    """Drop anime nobody follows any more, so the checker stops scraping them."""
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM anime WHERE url NOT IN (SELECT DISTINCT anime_url FROM follows)")
        return cur.rowcount


def record_heartbeat(subscriber_id: str) -> None:
    now = int(time.time())
    with get_db() as db:
        db.execute(
            "INSERT INTO subscribers (id, token_hash, webhook_enc, created_at, last_heartbeat) "
            "VALUES (%s, '', %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (subscriber_id, b"", now, now)
        )
        db.execute("UPDATE subscribers SET last_heartbeat = %s WHERE id = %s",
                   (now, subscriber_id))


def is_subscriber_online(subscriber_id: str, timeout_seconds: int = 180) -> bool:
    with get_db() as db:
        row = db.execute("SELECT last_heartbeat FROM subscribers WHERE id = %s",
                         (subscriber_id,)).fetchone()
    if not row or not row[0]:
        return False
    return (int(time.time()) - row[0]) < timeout_seconds


def queue_command(subscriber_id: str, anime_url: str, anime_title: str, episodes: str) -> int:
    now = int(time.time())
    with get_db() as db:
        # Ensure the subscriber row exists so a signed action link from Discord cannot
        # fail on a foreign key. The stub has no token_hash and no webhook, so it
        # authenticates nothing -- the client re-registers to fill it in.
        db.execute(
            "INSERT INTO subscribers (id, token_hash, webhook_enc, created_at, last_heartbeat) "
            "VALUES (%s, '', %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (subscriber_id, b"", now, now)
        )
        row = db.execute(
            "INSERT INTO commands (subscriber_id, anime_url, anime_title, episodes, status, created_at) "
            "VALUES (%s, %s, %s, %s, 'pending', %s) RETURNING id",
            (subscriber_id, anime_url, anime_title, episodes, now)
        ).fetchone()
        return row[0] if row else None


def get_pending_commands(subscriber_id: str) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT id, anime_url, anime_title, episodes, created_at FROM commands "
            "WHERE subscriber_id = %s AND status = 'pending' ORDER BY id ASC",
            (subscriber_id,)
        ).fetchall()
    return [
        {"id": r[0], "anime_url": r[1], "anime_title": r[2], "episodes": r[3], "created_at": r[4]}
        for r in rows
    ]


def ack_command(subscriber_id: str, command_id: int) -> bool:
    with get_db() as db:
        cur = db.execute(
            "UPDATE commands SET status = 'completed' WHERE id = %s AND subscriber_id = %s",
            (command_id, subscriber_id)
        )
        return cur.rowcount > 0


def get_subscriber_by_token(token: str) -> str | None:
    th = crypto.hash_token(token)
    with get_db() as db:
        rows = db.execute("SELECT id, token_hash FROM subscribers").fetchall()
    for sid, stored_hash in rows:
        if stored_hash and hmac.compare_digest(th, stored_hash):
            return sid
    return None
