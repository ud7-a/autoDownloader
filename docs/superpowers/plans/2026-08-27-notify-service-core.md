# Notification Service — Core (Plan 1 of 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the storage and HTTP core of a multi-tenant service that will notify users on Discord when a followed anime gets a new episode — subscribers, their followed anime, and their Discord webhook held safely.

**Architecture:** A small FastAPI app over SQLite. Two ideas drive the schema. First, **one row per distinct anime**, shared by everyone who follows it, so the checker (Plan 2) scrapes each anime once per cycle no matter how many users follow it — load tracks catalogue size, not user count. Second, **a webhook URL is a credential**: it is encrypted at rest, never returned by any endpoint, and never logged. Subscribers authenticate with a bearer token they receive once; the server stores only its hash.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, `cryptography` (Fernet), stdlib `sqlite3`, stdlib `unittest` (matching the desktop app's convention).

**Spec:** No separate spec document. Requirements are captured in this header and Global Constraints; the three-plan split is described under "Scope split" at the end.

## Global Constraints

- Python 3.13. Tests are **stdlib `unittest`** — the desktop app has no pytest and this service follows suit. Run: `py -m unittest discover -s service/tests`.
- The service lives in `service/` inside the existing repo, with its own `service/requirements.txt`. It must not import from `ui/`, `core/` or `utils/` — those pull in PyQt6. Plan 2 handles reuse of detection logic separately.
- **A webhook URL is a credential.** It is encrypted at rest, never returned in a response body, never written to a log or an exception message, and never included in a test fixture that gets printed.
- **Bearer tokens are stored hashed** (SHA-256), never in plaintext. The plaintext is shown exactly once, at creation.
- `AED_NOTIFY_KEY` (a Fernet key) and `AED_NOTIFY_DB` (database path) come from the environment. The service refuses to start without a key rather than inventing one — a generated-at-boot key would silently make every stored webhook undecryptable on restart.
- Deleting a subscriber deletes their rows outright. No soft-delete, no orphaned webhook.
- All timestamps are UTC epoch seconds (`int`), matching `time.time()` usage in the desktop app.

---

## File Structure

**Created:**

- `service/crypto.py` — Fernet wrapper. Encrypt/decrypt webhook URLs, hash tokens. The only module that touches key material.
- `service/store.py` — SQLite schema and every query. Owns the connection; no SQL anywhere else.
- `service/api.py` — FastAPI routes and request/response models. Calls `store`, never SQL directly.
- `service/requirements.txt` — service dependencies, separate from the desktop app's.
- `service/tests/__init__.py` — points `AED_NOTIFY_DB` at a temp file before any service import, mirroring `tests/__init__.py`'s isolation.
- `service/tests/test_crypto.py`, `service/tests/test_store.py`, `service/tests/test_api.py`

Why `store.py` owns all SQL: the checker in Plan 2 runs in a different process and needs the same queries. Keeping them in one module means the checker reuses them rather than writing a second, subtly different set.

### Schema

```sql
CREATE TABLE IF NOT EXISTS subscribers (
    id            TEXT PRIMARY KEY,
    token_hash    TEXT NOT NULL,
    webhook_enc   BLOB,
    created_at    INTEGER NOT NULL
);

-- One row per distinct anime, shared by every subscriber following it. This is what
-- keeps the checker's workload proportional to the catalogue rather than the user
-- count: at a 15-minute cadence, checking per-subscriber would multiply requests to
-- the source sites by however many people follow the same show.
CREATE TABLE IF NOT EXISTS anime (
    url             TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
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
```

`anime.last_seen_max` is what the world knows; `follows.notified_max` is what this
subscriber has been told. Someone who subscribes today should not receive 300 messages
for a show's back catalogue — Plan 2 seeds `notified_max` from `last_seen_max` on first
sight, exactly as the desktop app's `first_time` flag does.

---

### Task 1: Skeleton, isolation and health check

**Files:**
- Create: `service/requirements.txt`, `service/tests/__init__.py`, `service/api.py`, `service/tests/test_api.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `service.api.app` — the FastAPI application object, imported by every later test.

- [ ] **Step 1: Write the dependency list**

Create `service/requirements.txt`:

```
fastapi==0.115.6
uvicorn==0.34.0
cryptography==44.0.0
httpx==0.28.1
```

`httpx` is needed by FastAPI's `TestClient`.

Install: `py -m pip install -r service/requirements.txt`

- [ ] **Step 2: Write the test isolation bootstrap**

Create `service/tests/__init__.py`:

```python
"""Point the service at a throwaway database BEFORE any service module is imported.

Mirrors tests/__init__.py in the desktop app: a test must never touch real data.
"""

import os
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="aed-notify-tests-"), "notify.db")
os.environ["AED_NOTIFY_DB"] = _DB
# A fixed key so tests are deterministic. Never used outside tests.
os.environ.setdefault("AED_NOTIFY_KEY", "6DJUKXGDLPVFXKSWXWZ5EMEJZQXKHFJ3NFYDQZ7YHXM=")
```

- [ ] **Step 3: Write the failing test**

Create `service/tests/test_api.py`:

```python
import unittest

from fastapi.testclient import TestClient

from service.api import app


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_reports_ok(self):
        r = self.client.get("/v1/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
```

- [ ] **Step 4: Run it to verify it fails**

Run: `py -m unittest service.tests.test_api -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'service.api'`

- [ ] **Step 5: Write the minimal app**

Create `service/api.py`:

```python
"""HTTP surface of the notification service.

Routes call service.store; no SQL lives here. Nothing in this module ever returns or
logs a Discord webhook URL -- see the note on credentials in service/crypto.py.
"""

from fastapi import FastAPI

app = FastAPI(title="Auto Episodes Downloader notifications", version="1.0")


@app.get("/v1/health")
def health():
    return {"status": "ok"}
```

- [ ] **Step 6: Run it to verify it passes**

Run: `py -m unittest service.tests.test_api -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add service/requirements.txt service/tests/__init__.py service/tests/test_api.py service/api.py
git commit -m "feat(service): add notification service skeleton with health check"
```

---

### Task 2: Webhook encryption and token hashing

**Files:**
- Create: `service/crypto.py`, `service/tests/test_crypto.py`

**Interfaces:**
- Consumes: `AED_NOTIFY_KEY` from the environment.
- Produces, used by Tasks 3-5:
  - `encrypt_webhook(url: str) -> bytes`
  - `decrypt_webhook(blob: bytes) -> str`
  - `hash_token(token: str) -> str` (hex SHA-256)
  - `new_token() -> str` (URL-safe, 32 bytes of entropy)
  - `mask_webhook(url: str) -> str` — `"https://discord.com/api/webhooks/…1234"`, safe to show a user
  - `MissingKeyError` — raised when `AED_NOTIFY_KEY` is unset

- [ ] **Step 1: Write the failing test**

Create `service/tests/test_crypto.py`:

```python
import unittest

from service import crypto


class WebhookCryptoTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"

    def test_round_trip(self):
        self.assertEqual(crypto.decrypt_webhook(crypto.encrypt_webhook(self.URL)), self.URL)

    def test_ciphertext_does_not_contain_the_url(self):
        """A database dump must not reveal the webhook."""
        blob = crypto.encrypt_webhook(self.URL)
        self.assertNotIn(b"discord.com", blob)
        self.assertNotIn(b"abcdefghijklmnop", blob)

    def test_encryption_is_salted(self):
        """Identical URLs must not produce identical ciphertext, or a dump reveals
        which subscribers share a channel."""
        self.assertNotEqual(crypto.encrypt_webhook(self.URL), crypto.encrypt_webhook(self.URL))

    def test_mask_keeps_only_a_recognisable_tail(self):
        masked = crypto.mask_webhook(self.URL)
        self.assertTrue(masked.endswith("1234"))
        self.assertNotIn("abcdefghijklmnop", masked)

    def test_mask_of_blank_is_blank(self):
        self.assertEqual(crypto.mask_webhook(""), "")


class TokenTests(unittest.TestCase):
    def test_hash_is_stable_and_not_the_token(self):
        t = crypto.new_token()
        self.assertEqual(crypto.hash_token(t), crypto.hash_token(t))
        self.assertNotIn(t, crypto.hash_token(t))

    def test_tokens_are_unique(self):
        self.assertNotEqual(crypto.new_token(), crypto.new_token())

    def test_token_is_long_enough_to_resist_guessing(self):
        self.assertGreaterEqual(len(crypto.new_token()), 32)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest service.tests.test_crypto -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'service.crypto'`

- [ ] **Step 3: Write the implementation**

Create `service/crypto.py`:

```python
"""Key material and the handling of Discord webhook URLs.

A webhook URL is a credential: anyone holding it can post into that user's channel.
So it is encrypted at rest here, and the plaintext exists only in memory, only for as
long as it takes to send a message. It is never returned by an endpoint, never logged,
and never put in an exception message. mask_webhook() is what a user is shown instead.
"""

import hashlib
import os
import secrets

from cryptography.fernet import Fernet


class MissingKeyError(RuntimeError):
    pass


def _fernet():
    key = os.environ.get("AED_NOTIFY_KEY")
    if not key:
        # Generating one here would "work" until the next restart, at which point
        # every stored webhook would be undecryptable with no way to tell why.
        raise MissingKeyError("AED_NOTIFY_KEY is not set")
    return Fernet(key)


def encrypt_webhook(url):
    return _fernet().encrypt(url.encode("utf-8"))


def decrypt_webhook(blob):
    return _fernet().decrypt(blob).decode("utf-8")


def mask_webhook(url):
    """A form safe to show a user or put in a log: last four characters only."""
    if not url:
        return ""
    return "https://discord.com/api/webhooks/…" + url[-4:]


def new_token():
    return secrets.token_urlsafe(32)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run it to verify it passes**

Run: `py -m unittest service.tests.test_crypto -v`
Expected: PASS, 8 tests.

Fernet is authenticated and randomised, so `test_encryption_is_salted` passes without extra work — but keep the test: it fails loudly if anyone swaps Fernet for a deterministic scheme.

- [ ] **Step 5: Commit**

```bash
git add service/crypto.py service/tests/test_crypto.py
git commit -m "feat(service): encrypt webhook URLs at rest and hash subscriber tokens"
```

---

### Task 3: Storage layer and subscriber creation

**Files:**
- Create: `service/store.py`, `service/tests/test_store.py`
- Modify: `service/api.py`

**Interfaces:**
- Consumes: `service.crypto` (Task 2).
- Produces, used by Tasks 4-5 and by Plan 2's checker:
  - `connect() -> sqlite3.Connection` — applies the schema, enables foreign keys
  - `create_subscriber(webhook_url: str) -> tuple[str, str]` — `(subscriber_id, plaintext_token)`
  - `authenticate(subscriber_id: str, token: str) -> bool`
  - `get_webhook(subscriber_id: str) -> str | None` — decrypted; callers must not log it
  - `set_webhook(subscriber_id: str, webhook_url: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `service/tests/test_store.py`:

```python
import unittest

from service import store


class SubscriberTests(unittest.TestCase):
    URL = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"

    def setUp(self):
        store.reset_for_tests()

    def test_create_returns_an_id_and_a_token(self):
        sid, token = store.create_subscriber(self.URL)
        self.assertTrue(sid)
        self.assertTrue(token)
        self.assertNotEqual(sid, token)

    def test_the_right_token_authenticates(self):
        sid, token = store.create_subscriber(self.URL)
        self.assertTrue(store.authenticate(sid, token))

    def test_a_wrong_token_does_not(self):
        sid, _ = store.create_subscriber(self.URL)
        self.assertFalse(store.authenticate(sid, "not-the-token"))

    def test_an_unknown_subscriber_does_not_authenticate(self):
        self.assertFalse(store.authenticate("nobody", "anything"))

    def test_webhook_round_trips(self):
        sid, _ = store.create_subscriber(self.URL)
        self.assertEqual(store.get_webhook(sid), self.URL)

    def test_webhook_is_not_stored_in_the_clear(self):
        """The strongest guarantee this layer offers: a stolen database file does not
        hand over everyone's Discord channels."""
        sid, _ = store.create_subscriber(self.URL)
        with store.connect() as db:
            raw = db.execute("SELECT webhook_enc FROM subscribers WHERE id=?", (sid,)).fetchone()[0]
        self.assertNotIn(b"discord.com", raw)

    def test_token_is_not_stored_in_the_clear(self):
        sid, token = store.create_subscriber(self.URL)
        with store.connect() as db:
            stored = db.execute("SELECT token_hash FROM subscribers WHERE id=?", (sid,)).fetchone()[0]
        self.assertNotIn(token, stored)

    def test_set_webhook_replaces_it(self):
        other = "https://discord.com/api/webhooks/999/zzzzzzzzzzzzzzzz9999"
        sid, _ = store.create_subscriber(self.URL)
        store.set_webhook(sid, other)
        self.assertEqual(store.get_webhook(sid), other)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest service.tests.test_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'service.store'`

- [ ] **Step 3: Write the implementation**

Create `service/store.py`:

```python
"""Every SQL statement in the service.

Kept in one module because the checker process (Plan 2) needs the same queries;
splitting them would mean two subtly different sets of the same logic.
"""

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
"""


def db_path():
    return os.environ.get("AED_NOTIFY_DB") or "notify.db"


def connect():
    db = sqlite3.connect(db_path())
    db.execute("PRAGMA foreign_keys = ON")   # off by default; the cascades rely on it
    db.executescript(SCHEMA)
    return db


def reset_for_tests():
    """Drop every row. Refuses to run against a database that isn't a test one."""
    path = db_path()
    if "aed-notify-tests" not in path:
        raise RuntimeError(f"refusing to wipe a non-test database: {path}")
    with connect() as db:
        for table in ("follows", "anime", "subscribers"):
            db.execute(f"DELETE FROM {table}")


def create_subscriber(webhook_url):
    sid = uuid.uuid4().hex
    token = crypto.new_token()
    with connect() as db:
        db.execute(
            "INSERT INTO subscribers (id, token_hash, webhook_enc, created_at) VALUES (?,?,?,?)",
            (sid, crypto.hash_token(token), crypto.encrypt_webhook(webhook_url), int(time.time())))
    return sid, token


def authenticate(subscriber_id, token):
    import hmac
    with connect() as db:
        row = db.execute("SELECT token_hash FROM subscribers WHERE id=?",
                         (subscriber_id,)).fetchone()
    if not row:
        return False
    # Constant-time: a timing difference here leaks the hash a byte at a time.
    return hmac.compare_digest(row[0], crypto.hash_token(token))


def get_webhook(subscriber_id):
    with connect() as db:
        row = db.execute("SELECT webhook_enc FROM subscribers WHERE id=?",
                         (subscriber_id,)).fetchone()
    if not row or not row[0]:
        return None
    return crypto.decrypt_webhook(row[0])


def set_webhook(subscriber_id, webhook_url):
    with connect() as db:
        db.execute("UPDATE subscribers SET webhook_enc=? WHERE id=?",
                   (crypto.encrypt_webhook(webhook_url), subscriber_id))
```

- [ ] **Step 4: Run it to verify it passes**

Run: `py -m unittest service.tests.test_store -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add service/store.py service/tests/test_store.py
git commit -m "feat(service): add SQLite store with encrypted webhooks and hashed tokens"
```

---

### Task 4: Registration and watchlist sync endpoints

**Files:**
- Modify: `service/api.py`, `service/store.py`
- Test: `service/tests/test_api.py`

**Interfaces:**
- Consumes: everything from Tasks 2-3.
- Produces:
  - `store.replace_follows(subscriber_id: str, items: list[dict]) -> int` — each item `{"url": str, "title": str}`; returns the number followed. Upserts into `anime`, replaces that subscriber's `follows`.
  - `store.followers_of(anime_url: str) -> list[str]` — subscriber ids; used by Plan 2's fan-out.
  - `store.due_anime(limit: int = 200) -> list[dict]` — anime ordered by `last_checked_at` ascending; Plan 2's work queue.
  - `POST /v1/subscribers` → `201 {"id", "token", "webhook": masked}`
  - `PUT /v1/subscribers/{id}/watchlist` (Bearer token) → `{"following": n}`

- [ ] **Step 1: Write the failing test**

Append to `service/tests/test_api.py`:

```python
from service import store

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.client = TestClient(app)

    def register(self):
        r = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK})
        self.assertEqual(r.status_code, 201)
        return r.json()

    def test_registration_returns_id_and_token(self):
        body = self.register()
        self.assertTrue(body["id"])
        self.assertTrue(body["token"])

    def test_registration_never_echoes_the_webhook(self):
        """The response is the most likely place for a credential to leak."""
        r = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK})
        self.assertNotIn("abcdefghijklmnop", r.text)

    def test_a_non_discord_url_is_rejected(self):
        r = self.client.post("/v1/subscribers", json={"webhook": "https://evil.example/hook"})
        self.assertEqual(r.status_code, 422)

    def test_watchlist_sync_requires_the_token(self):
        body = self.register()
        r = self.client.put(f"/v1/subscribers/{body['id']}/watchlist",
                            json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]})
        self.assertEqual(r.status_code, 401)

    def test_watchlist_sync_stores_the_items(self):
        body = self.register()
        r = self.client.put(
            f"/v1/subscribers/{body['id']}/watchlist",
            headers={"Authorization": f"Bearer {body['token']}"},
            json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"},
                            {"url": "https://witanime.life/anime/y/", "title": "Y"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["following"], 2)

    def test_sync_replaces_rather_than_appends(self):
        body = self.register()
        head = {"Authorization": f"Bearer {body['token']}"}
        url = f"/v1/subscribers/{body['id']}/watchlist"
        self.client.put(url, headers=head,
                        json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]})
        r = self.client.put(url, headers=head,
                            json={"items": [{"url": "https://witanime.life/anime/y/", "title": "Y"}]})
        self.assertEqual(r.json()["following"], 1)

    def test_two_subscribers_share_one_anime_row(self):
        """The dedupe that keeps the checker's load tied to the catalogue, not users."""
        a, b = self.register(), self.register()
        item = {"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]}
        for body in (a, b):
            self.client.put(f"/v1/subscribers/{body['id']}/watchlist",
                            headers={"Authorization": f"Bearer {body['token']}"}, json=item)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM anime").fetchone()[0], 1)
        self.assertEqual(len(store.followers_of("https://witanime.life/anime/x/")), 2)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest service.tests.test_api -v`
Expected: FAIL — `404` on `/v1/subscribers`, and `AttributeError: module 'service.store' has no attribute 'replace_follows'`

- [ ] **Step 3: Add the store functions**

Append to `service/store.py`:

```python
def replace_follows(subscriber_id, items):
    """Set exactly what this subscriber follows.

    Anime rows are upserted and shared: two subscribers following the same show
    produce one anime row, which is what lets the checker scrape it once per cycle.
    An existing anime row keeps its last_seen_max -- it belongs to the anime, not to
    whoever happened to sync last.
    """
    now = int(time.time())
    with connect() as db:
        db.execute("DELETE FROM follows WHERE subscriber_id=?", (subscriber_id,))
        for item in items:
            url = (item.get("url") or "").strip()
            if not url:
                continue
            db.execute(
                "INSERT INTO anime (url, title, last_seen_max, last_checked_at) "
                "VALUES (?,?,0,0) ON CONFLICT(url) DO UPDATE SET title=excluded.title",
                (url, (item.get("title") or url)[:200]))
            db.execute(
                "INSERT OR REPLACE INTO follows (subscriber_id, anime_url, notified_max) "
                "VALUES (?, ?, COALESCE((SELECT notified_max FROM follows "
                "WHERE subscriber_id=? AND anime_url=?), 0))",
                (subscriber_id, url, subscriber_id, url))
        count = db.execute("SELECT COUNT(*) FROM follows WHERE subscriber_id=?",
                           (subscriber_id,)).fetchone()[0]
    return count


def followers_of(anime_url):
    with connect() as db:
        return [r[0] for r in db.execute(
            "SELECT subscriber_id FROM follows WHERE anime_url=?", (anime_url,))]


def due_anime(limit=200):
    """Anime least recently checked first -- the checker's work queue."""
    with connect() as db:
        rows = db.execute(
            "SELECT url, title, last_seen_max, last_checked_at FROM anime "
            "ORDER BY last_checked_at ASC LIMIT ?", (limit,)).fetchall()
    return [{"url": r[0], "title": r[1], "last_seen_max": r[2], "last_checked_at": r[3]}
            for r in rows]
```

- [ ] **Step 4: Add the endpoints**

Replace the contents of `service/api.py` with:

```python
"""HTTP surface of the notification service.

Routes call service.store; no SQL lives here. Nothing in this module ever returns or
logs a Discord webhook URL -- see the note on credentials in service/crypto.py.
"""

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, field_validator

from service import crypto, store

app = FastAPI(title="Auto Episodes Downloader notifications", version="1.0")


class Registration(BaseModel):
    webhook: str

    @field_validator("webhook")
    @classmethod
    def must_be_a_discord_webhook(cls, v):
        # Anything else is either a mistake or an attempt to make the service post to
        # a third party on a schedule.
        if not v.startswith("https://discord.com/api/webhooks/"):
            raise ValueError("must be a https://discord.com/api/webhooks/ URL")
        return v


class WatchItem(BaseModel):
    url: str
    title: str = ""


class WatchlistSync(BaseModel):
    items: list[WatchItem]


def require_subscriber(subscriber_id: str, authorization: str = Header(default="")):
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not token or not store.authenticate(subscriber_id, token):
        raise HTTPException(status_code=401, detail="bad subscriber or token")
    return subscriber_id


@app.get("/v1/health")
def health():
    return {"status": "ok"}


@app.post("/v1/subscribers", status_code=201)
def register(body: Registration):
    sid, token = store.create_subscriber(body.webhook)
    # The token is shown exactly once; only its hash is stored.
    return {"id": sid, "token": token, "webhook": crypto.mask_webhook(body.webhook)}


@app.put("/v1/subscribers/{subscriber_id}/watchlist")
def sync_watchlist(subscriber_id: str, body: WatchlistSync,
                   _auth: str = Depends(require_subscriber)):
    n = store.replace_follows(subscriber_id, [i.model_dump() for i in body.items])
    return {"following": n}
```

- [ ] **Step 5: Run it to verify it passes**

Run: `py -m unittest service.tests.test_api -v`
Expected: PASS, 8 tests (1 health + 7 registration).

- [ ] **Step 6: Commit**

```bash
git add service/api.py service/store.py service/tests/test_api.py
git commit -m "feat(service): add registration and watchlist sync with shared anime rows"
```

---

### Task 5: Deletion, and proving nothing survives it

**Files:**
- Modify: `service/api.py`, `service/store.py`
- Test: `service/tests/test_api.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `store.delete_subscriber(subscriber_id: str) -> None`
  - `store.prune_orphan_anime() -> int` — removes anime nobody follows; returns how many
  - `DELETE /v1/subscribers/{id}` (Bearer token) → `204`

- [ ] **Step 1: Write the failing test**

Append to `service/tests/test_api.py`:

```python
class DeletionTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.client = TestClient(app)

    def register_with_one_follow(self):
        body = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK}).json()
        self.client.put(f"/v1/subscribers/{body['id']}/watchlist",
                        headers={"Authorization": f"Bearer {body['token']}"},
                        json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]})
        return body

    def test_delete_requires_the_token(self):
        body = self.register_with_one_follow()
        self.assertEqual(self.client.delete(f"/v1/subscribers/{body['id']}").status_code, 401)

    def test_delete_removes_the_subscriber_and_their_follows(self):
        body = self.register_with_one_follow()
        r = self.client.delete(f"/v1/subscribers/{body['id']}",
                               headers={"Authorization": f"Bearer {body['token']}"})
        self.assertEqual(r.status_code, 204)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM follows").fetchone()[0], 0)

    def test_the_webhook_is_gone_after_deletion(self):
        """Deleting means deleting: no soft-delete row keeping a credential alive."""
        body = self.register_with_one_follow()
        self.client.delete(f"/v1/subscribers/{body['id']}",
                           headers={"Authorization": f"Bearer {body['token']}"})
        self.assertIsNone(store.get_webhook(body["id"]))

    def test_deletion_does_not_remove_an_anime_someone_else_follows(self):
        a = self.register_with_one_follow()
        self.register_with_one_follow()          # a second subscriber, same anime
        self.client.delete(f"/v1/subscribers/{a['id']}",
                           headers={"Authorization": f"Bearer {a['token']}"})
        self.assertEqual(len(store.followers_of("https://witanime.life/anime/x/")), 1)
        self.assertEqual(len(store.due_anime()), 1)

    def test_pruning_drops_anime_nobody_follows(self):
        a = self.register_with_one_follow()
        self.client.delete(f"/v1/subscribers/{a['id']}",
                           headers={"Authorization": f"Bearer {a['token']}"})
        self.assertEqual(store.prune_orphan_anime(), 1)
        self.assertEqual(len(store.due_anime()), 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest service.tests.test_api -v`
Expected: FAIL — `405 Method Not Allowed` on DELETE, and `AttributeError: ... has no attribute 'prune_orphan_anime'`

- [ ] **Step 3: Add the store functions**

Append to `service/store.py`:

```python
def delete_subscriber(subscriber_id):
    """Remove the subscriber and everything belonging to them.

    No soft delete: a disabled row still holds a live Discord credential, and the only
    safe way to stop holding it is to stop holding it.
    """
    with connect() as db:
        db.execute("DELETE FROM follows WHERE subscriber_id=?", (subscriber_id,))
        db.execute("DELETE FROM subscribers WHERE id=?", (subscriber_id,))


def prune_orphan_anime():
    """Drop anime nobody follows any more, so the checker stops scraping them."""
    with connect() as db:
        cur = db.execute(
            "DELETE FROM anime WHERE url NOT IN (SELECT DISTINCT anime_url FROM follows)")
        return cur.rowcount
```

- [ ] **Step 4: Add the endpoint**

Add to `service/api.py`:

```python
@app.delete("/v1/subscribers/{subscriber_id}", status_code=204)
def unsubscribe(subscriber_id: str, _auth: str = Depends(require_subscriber)):
    store.delete_subscriber(subscriber_id)
    store.prune_orphan_anime()
    return Response(status_code=204)
```

- [ ] **Step 5: Run it to verify it passes**

Run: `py -m unittest service.tests.test_api -v`
Expected: PASS, 13 tests.

Then the whole service suite: `py -m unittest discover -s service/tests -v`
Expected: **29 tests** — 8 crypto, 8 store, 13 api (1 health + 7 registration + 5 deletion).

- [ ] **Step 6: Commit**

```bash
git add service/api.py service/store.py service/tests/test_api.py
git commit -m "feat(service): add subscriber deletion and orphan anime pruning"
```

---

## Verification

```bash
py -m pip install -r service/requirements.txt
py -m unittest discover -s service/tests -v
py -m unittest discover -s tests          # the desktop app's 140 must still pass
py tools/lint.py
```

Run the service by hand and exercise it end to end:

```bash
$env:AED_NOTIFY_KEY = (py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
$env:AED_NOTIFY_DB = "$env:TEMP\notify-dev.db"
py -m uvicorn service.api:app --port 8000
```

Then check that a webhook never comes back out — the guarantee that matters most:

```bash
curl -X POST localhost:8000/v1/subscribers -H "Content-Type: application/json" -d "{\"webhook\":\"https://discord.com/api/webhooks/1/secretpart1234\"}"
```

The response must contain `id`, `token`, and a masked webhook — never `secretpart`.
Confirm the same for the stored bytes:

```bash
py -c "import sqlite3,os; print(sqlite3.connect(os.environ['AED_NOTIFY_DB']).execute('SELECT webhook_enc FROM subscribers').fetchone())"
```

Expected: an opaque `gAAAAA…` blob with no readable URL.

---

## Scope split

This plan is the foundation. Three more follow, each standing alone:

**Plan 2 — Checker worker.** A loop over `store.due_anime()` at the chosen 15-minute
cadence, reusing the desktop app's `AnimeDetailsThread.detect_entries` for detection
(it needs PyQt6 installed and `QT_QPA_PLATFORM=offscreen`, which the existing tests
already prove works headlessly). Updates `anime.last_seen_max`, fans out to
`followers_of()`, sends Discord messages, advances `follows.notified_max`. Must seed
`notified_max` from `last_seen_max` on first sight so a new subscriber is not sent a
show's entire back catalogue. Needs rate limiting between site requests, and retry
handling for Discord's 429s.

**Plan 3 — Desktop client.** Opt-in in the app: register once, store the returned
subscriber id and token locally, push the watchlist on change, offer "stop notifying
me" which calls `DELETE`. Reuses the existing `discord_webhook` setting.

**Plan 4 — Deployment.** Container with Chrome, a small always-on host, `AED_NOTIFY_KEY`
in the host's secret store, a backup for the SQLite file, and a plan for what happens
when the key is lost (every stored webhook becomes undecryptable — users must re-enter
them, so the app should handle that path).

## Before Plan 2, decide these

- **Where it runs and what it costs.** Selenium needs real Chrome, which rules out most
  free serverless tiers. A small always-on VPS is the realistic shape.
- **What you tell users.** You will hold their Discord webhooks. That deserves a plain
  sentence in the app at opt-in time saying what is stored and how to delete it — which
  is what Task 5's endpoint is for.
- **Load on the source sites.** At 15 minutes, the shared `anime` table keeps requests
  proportional to distinct shows rather than users, but the checker still needs a
  deliberate delay between requests. Set that number in Plan 2 rather than discovering
  it from a block.
