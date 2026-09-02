# Close the Notification Service Auth Bypass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `store.authenticate()` actually authenticate, and put the database somewhere that survives a restart so it no longer needs to be worked around.

**Architecture:** The bypass exists to let clients recover after Render's ephemeral `/tmp` wipes the database. That recovery already has a legitimate form: subscriber ids are `sha256(webhook)[:32]`, and `POST /v1/subscribers` is unauthenticated, so a client holding its webhook can re-register and get its identity back. So the client learns to re-register on `401` (Task 1), the bypass is deleted (Task 2), the signing key stops falling back to a public constant (Task 3), and the storage that caused all of it is fixed (Task 4).

**Tech Stack:** Python 3.13, FastAPI, SQLite, stdlib `unittest`, Render (Docker).

**Spec:** No separate spec. The defects are stated in Context below, each with the file and line it lives at.

## Global Constraints

- Tests are stdlib `unittest`. Service: `py -m unittest discover -s service/tests` (**baseline 45, passing**). Desktop app: `py -m unittest discover -s tests` (**baseline 140, passing**). Neither count may go down.
- `py tools/lint.py` must exit 0.
- **The service is deployed.** Task 2 changes who can authenticate, so Task 1 ships first — but see "Deploy order" at the end: clients that have not updated will lose cloud notifications until they do. That is the intended trade, not an oversight.
- A webhook URL is a credential. It stays encrypted at rest, is never returned by an endpoint, and never appears in a log or an exception message.
- `AED_NOTIFY_KEY` is required. Code must fail loudly without it rather than substituting a default — Task 3 exists because one place does the opposite.
- No new dependencies.

---

## Context

Three defects, found reviewing the deployed service.

**1. `authenticate()` accepts invented credentials** — [service/store.py:139-148](../../../service/store.py)

```python
if not row:
    # Auto-provision subscriber on ephemeral container restart if valid format
    if len(subscriber_id) == 32 and len(token) >= 32:
        db.execute("INSERT INTO subscribers ...")
        return True
```

Any 32-character id with any 32-character token is accepted and created. Nothing is
verified. A second, narrower case sits at [store.py:149-152](../../../service/store.py):
a row whose `token_hash` is blank has it **set to whatever token the caller presents**,
returning True — so such a row is claimable by the first caller to name it.

This gates five endpoints through `require_subscriber` ([api.py:65](../../../service/api.py)):
watchlist sync, webhook update, deletion, heartbeat, and the command queue.

**2. It is not an edge case in production** — [render.yaml](../../../render.yaml) sets
`plan: free` and `AED_NOTIFY_DB: /tmp/notify.db`. Render's free tier has no persistent
disk and spins down when idle, so `/tmp` is wiped regularly and the `subscribers` table
is empty much of the time. The missing-row branch is the *normal* path there.

The commits `0f0c499`, `eb59fbb`, `3c5a37e`, `f213f2f` are all attempts to live with
this. They cannot succeed: the auto-provisioned row stores `webhook_enc` as `""`, so
`get_webhook()` returns `None` and that subscriber can never be notified anyway. The
storage is the actual bug.

**3. The action signing key falls back to a public constant** —
[service/crypto.py:54](../../../service/crypto.py):

```python
key = (os.environ.get("AED_NOTIFY_KEY") or "default-aed-action-key").encode("utf-8")
```

The repository is public. Any deployment without that variable set has forgeable
signatures on the two web-action endpoints ([api.py:194](../../../service/api.py),
[api.py:214](../../../service/api.py)). `_fernet()` in the same file correctly refuses
to run without the key; this silently degrades instead.

Existing tests do not catch the bypass: `test_an_unknown_subscriber_does_not_authenticate`
([service/tests/test_store.py:33](../../../service/tests/test_store.py)) passes only
because `"nobody"` happens to be 6 characters rather than 32.

---

## File Structure

**Modified:**

- `utils/config.py` — `cloud_register_and_sync()` at `:164` already performs the exact
  recovery needed. Task 1 adds `cloud_recover_identity()` beside it and calls it from
  the request helper when the server answers `401`.
- `service/store.py:133-153` — `authenticate()` loses both grant-on-assertion branches.
- `service/crypto.py:53-56` — `sign_action()` requires the key.
- `render.yaml` — a persistent disk, and `AED_NOTIFY_DB` moved onto it.
- `service/tests/test_store.py`, `service/tests/test_crypto.py`, `tests/test_logic.py` —
  new cases.

No new files. Every change lands in a function that already exists.

---

### Task 1: Client re-registers when the server rejects it

Ships **before** Task 2 so an updated client survives the tightened server.

**Files:**
- Modify: `utils/config.py` (add `cloud_recover_identity`, use it on 401)
- Test: `tests/test_logic.py`

**Interfaces:**
- Consumes: `cloud_register_and_sync(service_url=None, webhook_url=None) -> tuple[bool, str]` (`utils/config.py:164`).
- Produces: `cloud_recover_identity() -> bool` — re-registers using the stored webhook and returns whether new credentials were obtained.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_logic.py`:

```python
class CloudRecoveryTests(unittest.TestCase):
    """The service can lose its database. Recovery is a re-registration proving
    possession of the webhook -- never the server trusting whatever id it is handed."""

    def setUp(self):
        from utils import config
        self.config = config
        with config.config_lock:
            self.saved = dict(config.app_settings)

    def tearDown(self):
        with self.config.config_lock:
            self.config.app_settings.clear()
            self.config.app_settings.update(self.saved)

    def test_recovery_needs_a_stored_webhook(self):
        with self.config.config_lock:
            self.config.app_settings["discord_webhook"] = ""
            self.config.app_settings["cloud_service_url"] = "https://example.invalid"
        self.assertFalse(self.config.cloud_recover_identity())

    def test_recovery_needs_a_service_url(self):
        with self.config.config_lock:
            self.config.app_settings["discord_webhook"] = "https://discord.com/api/webhooks/1/aaaa"
            self.config.app_settings["cloud_service_url"] = ""
        self.assertFalse(self.config.cloud_recover_identity())

    def test_recovery_re_registers_and_keeps_the_new_credentials(self):
        calls = []

        def fake_register(service_url=None, webhook_url=None):
            calls.append((service_url, webhook_url))
            with self.config.config_lock:
                self.config.app_settings["cloud_subscriber_id"] = "new-id"
                self.config.app_settings["cloud_token"] = "new-token"
            return True, "registered"

        original = self.config.cloud_register_and_sync
        self.config.cloud_register_and_sync = fake_register
        try:
            with self.config.config_lock:
                self.config.app_settings["discord_webhook"] = "https://discord.com/api/webhooks/1/aaaa"
                self.config.app_settings["cloud_service_url"] = "https://example.invalid"
                self.config.app_settings["cloud_subscriber_id"] = "stale-id"
            self.assertTrue(self.config.cloud_recover_identity())
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.config.app_settings["cloud_subscriber_id"], "new-id")
        finally:
            self.config.cloud_register_and_sync = original

    def test_a_failed_re_registration_reports_false(self):
        original = self.config.cloud_register_and_sync
        self.config.cloud_register_and_sync = lambda service_url=None, webhook_url=None: (False, "down")
        try:
            with self.config.config_lock:
                self.config.app_settings["discord_webhook"] = "https://discord.com/api/webhooks/1/aaaa"
                self.config.app_settings["cloud_service_url"] = "https://example.invalid"
            self.assertFalse(self.config.cloud_recover_identity())
        finally:
            self.config.cloud_register_and_sync = original
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest tests.test_logic.CloudRecoveryTests -v`
Expected: FAIL — `AttributeError: module 'utils.config' has no attribute 'cloud_recover_identity'`

- [ ] **Step 3: Write the implementation**

Add to `utils/config.py`, directly below `cloud_register_and_sync`:

```python
def cloud_recover_identity() -> bool:
    """Re-register after the service has forgotten us, returning whether it worked.

    The service can lose its database (see render.yaml). Recovery is a plain
    re-registration: subscriber ids are derived from the webhook, so registering again
    with the same webhook returns the same identity and a fresh token. Possession of
    the webhook is the proof -- the server must never simply believe an id it is
    handed, which is what it used to do.
    """
    with config_lock:
        s_url = (app_settings.get("cloud_service_url") or "").rstrip("/")
        wh_url = app_settings.get("discord_webhook") or ""
    if not s_url or not wh_url:
        return False
    ok, _msg = cloud_register_and_sync(s_url, wh_url)
    return bool(ok)
```

- [ ] **Step 4: Run it to verify it passes**

Run: `py -m unittest tests.test_logic.CloudRecoveryTests -v`
Expected: PASS, 4 tests.

- [ ] **Step 5: Call it when the server returns 401**

Five functions in `utils/config.py` call the service with the stored
`cloud_subscriber_id` / `cloud_token`, and each treats an HTTP error as a plain
failure:

| Function | Line | Route through recovery? |
|---|---|---|
| `cloud_sync_watchlist()` | `:207` | yes |
| `cloud_unsubscribe()` | `:254` | **no** — a 401 means the server has already forgotten us, which is what unsubscribing wanted. Re-registering only to delete would recreate the subscriber. |
| `cloud_send_heartbeat()` | `:329` | yes |
| `cloud_fetch_commands()` | `:354` | yes |
| `cloud_ack_command(command_id)` | `:378` | yes |

`cloud_register_and_sync()` (`:164`) is the registration call itself and must not be
wrapped — it would recurse.

Give the four a shared retry by adding this helper next to `cloud_recover_identity`:

```python
def cloud_request_with_recovery(do_request):
    """Run a service call; on 401, re-register once and try again.

    `do_request` takes (subscriber_id, token) and raises urllib.error.HTTPError on a
    non-2xx response. A 401 means the service no longer knows us -- almost always
    because it lost its database -- so we prove who we are and retry exactly once.
    """
    import urllib.error

    with config_lock:
        sid = app_settings.get("cloud_subscriber_id") or ""
        token = app_settings.get("cloud_token") or ""
    try:
        return do_request(sid, token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    if not cloud_recover_identity():
        raise RuntimeError("cloud service rejected our credentials and re-registration failed")
    with config_lock:
        sid = app_settings.get("cloud_subscriber_id") or ""
        token = app_settings.get("cloud_token") or ""
    return do_request(sid, token)
```

Then route each of those service calls through it, passing a closure that performs the
one request. Do not add a second retry: if re-registration succeeded and the call is
still rejected, the problem is not identity and looping will hide it.

- [ ] **Step 6: Run the whole suite and lint**

Run: `py -m unittest discover -s tests`
Expected: `Ran 144 tests` … `OK`

Run: `py tools/lint.py`
Expected: `lint: clean`

- [ ] **Step 7: Commit**

```bash
git add utils/config.py tests/test_logic.py
git commit -m "feat: re-register with the cloud service when it rejects our credentials"
```

---

### Task 2: Delete the authentication bypass

**Files:**
- Modify: `service/store.py:133-153`
- Test: `service/tests/test_store.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `authenticate(subscriber_id: str, token: str) -> bool` — unchanged signature, now returning False for anything it cannot verify.

- [ ] **Step 1: Write the failing test**

Append to `service/tests/test_store.py`:

```python
class AuthenticationBypassTests(unittest.TestCase):
    """authenticate() used to create a subscriber for any 32-character id paired with
    any 32-character token, and return True. On a host with ephemeral storage the
    subscribers table is empty much of the time, so that was the ordinary path."""

    def setUp(self):
        store.reset_for_tests()

    def test_an_invented_id_and_token_are_rejected(self):
        self.assertFalse(store.authenticate("a" * 32, "b" * 40))

    def test_an_invented_pair_does_not_create_a_subscriber(self):
        store.authenticate("c" * 32, "d" * 40)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)

    def test_short_and_long_ids_are_both_rejected(self):
        self.assertFalse(store.authenticate("nobody", "anything"))
        self.assertFalse(store.authenticate("e" * 64, "f" * 64))

    def test_a_blank_token_hash_cannot_be_claimed(self):
        """A row with no token_hash used to accept the first token offered and adopt
        it. Such rows exist from earlier auto-provisioning."""
        sid, _ = store.create_subscriber("https://discord.com/api/webhooks/1/aaaabbbbccccdddd")
        with store.connect() as db:
            db.execute("UPDATE subscribers SET token_hash='' WHERE id=?", (sid,))
        self.assertFalse(store.authenticate(sid, "g" * 40))
        with store.connect() as db:
            self.assertEqual(
                db.execute("SELECT token_hash FROM subscribers WHERE id=?", (sid,)).fetchone()[0], "")

    def test_a_real_subscriber_still_authenticates(self):
        sid, token = store.create_subscriber("https://discord.com/api/webhooks/2/eeeeffffgggghhhh")
        self.assertTrue(store.authenticate(sid, token))
        self.assertFalse(store.authenticate(sid, "not-the-token"))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest service.tests.test_store.AuthenticationBypassTests -v`
Expected: FAIL — `test_an_invented_id_and_token_are_rejected`, `test_an_invented_pair_does_not_create_a_subscriber` and `test_a_blank_token_hash_cannot_be_claimed` all fail, because today they return True.

- [ ] **Step 3: Replace the function**

In `service/store.py`, replace `authenticate` (lines 133-153) with:

```python
def authenticate(subscriber_id: str, token: str) -> bool:
    """True only when this subscriber exists and the token matches its stored hash.

    There is deliberately no recovery path here. This used to create a subscriber for
    any id and token of the right length so clients could survive the database being
    wiped, which meant anyone could authenticate as anyone. Recovery belongs to the
    client: it re-registers with its webhook (see cloud_recover_identity in
    utils/config.py), which proves possession of something the server can check.
    """
    if not (subscriber_id and token):
        return False
    with get_db() as db:
        row = db.execute("SELECT token_hash FROM subscribers WHERE id=?",
                         (subscriber_id,)).fetchone()
    if not row or not row[0]:
        return False
    return hmac.compare_digest(row[0], crypto.hash_token(token))
```

- [ ] **Step 4: Run it to verify it passes**

Run: `py -m unittest service.tests.test_store -v`
Expected: PASS. `test_an_unknown_subscriber_does_not_authenticate` (`test_store.py:33`) still passes and now does so for the right reason.

- [ ] **Step 5: Run the whole service suite**

Run: `py -m unittest discover -s service/tests`
Expected: `Ran 50 tests` … `OK`

If `test_api.py`, `test_commands.py` or `test_integration.py` fail here, read the
failure before changing anything: a test that depended on the bypass was asserting the
bug. Fix such a test by registering a real subscriber in its setup — never by
loosening `authenticate` again.

- [ ] **Step 6: Commit**

```bash
git add service/store.py service/tests/test_store.py
git commit -m "fix: reject unknown subscribers instead of provisioning them on demand"
```

---

### Task 3: Require the signing key

**Files:**
- Modify: `service/crypto.py:53-56`
- Test: `service/tests/test_crypto.py`

**Interfaces:**
- Consumes: `MissingKeyError` (already defined, `service/crypto.py:17`).
- Produces: `sign_action(subscriber_id: str, action: str) -> str` — same signature, now raising `MissingKeyError` when `AED_NOTIFY_KEY` is unset.

- [ ] **Step 1: Write the failing test**

Append to `service/tests/test_crypto.py`:

```python
class SigningKeyTests(unittest.TestCase):
    """sign_action fell back to a constant that is published in this repository, so a
    deployment without the key had forgeable action signatures."""

    def test_signing_without_a_key_raises(self):
        import os
        saved = os.environ.pop("AED_NOTIFY_KEY", None)
        try:
            with self.assertRaises(crypto.MissingKeyError):
                crypto.sign_action("sub", "act")
        finally:
            if saved is not None:
                os.environ["AED_NOTIFY_KEY"] = saved

    def test_verifying_without_a_key_is_false_not_a_crash(self):
        """Verification runs on a request path; a missing key must reject, not 500."""
        import os
        saved = os.environ.pop("AED_NOTIFY_KEY", None)
        try:
            self.assertFalse(crypto.verify_action("sub", "act", "0" * 16))
        finally:
            if saved is not None:
                os.environ["AED_NOTIFY_KEY"] = saved

    def test_a_signature_verifies_and_a_tampered_one_does_not(self):
        sig = crypto.sign_action("sub", "download:5")
        self.assertTrue(crypto.verify_action("sub", "download:5", sig))
        self.assertFalse(crypto.verify_action("sub", "download:6", sig))
        self.assertFalse(crypto.verify_action("other", "download:5", sig))

    def test_the_published_default_key_does_not_produce_valid_signatures(self):
        """Guards against the fallback being reintroduced."""
        import hashlib
        import hmac as _hmac
        forged = _hmac.new(b"default-aed-action-key", b"sub:act", hashlib.sha256).hexdigest()[:16]
        self.assertFalse(crypto.verify_action("sub", "act", forged))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -m unittest service.tests.test_crypto.SigningKeyTests -v`
Expected: FAIL — `test_signing_without_a_key_raises` gets a signature instead of an
exception, and `test_the_published_default_key_does_not_produce_valid_signatures` fails
whenever the tests' own key is absent.

- [ ] **Step 3: Write the implementation**

In `service/crypto.py`, replace `sign_action` and `verify_action` with:

```python
def sign_action(subscriber_id: str, action: str) -> str:
    key = os.environ.get("AED_NOTIFY_KEY")
    if not key:
        # There used to be a literal default here. This repository is public, so that
        # made every signature forgeable on any deployment missing the variable.
        raise MissingKeyError("AED_NOTIFY_KEY is not set")
    msg = f"{subscriber_id}:{action}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def verify_action(subscriber_id: str, action: str, sig: str) -> bool:
    # Verification sits on a request path, so a missing key rejects the request rather
    # than raising through the handler.
    try:
        expected = sign_action(subscriber_id, action)
    except MissingKeyError:
        return False
    return hmac.compare_digest(expected, sig or "")
```

- [ ] **Step 4: Run it to verify it passes**

Run: `py -m unittest service.tests.test_crypto -v`
Expected: PASS.

- [ ] **Step 5: Run the whole service suite and commit**

Run: `py -m unittest discover -s service/tests`
Expected: `Ran 54 tests` … `OK`

```bash
git add service/crypto.py service/tests/test_crypto.py
git commit -m "fix: require AED_NOTIFY_KEY for action signing instead of a public default"
```

---

### Task 4: Give the database somewhere to live

Removes the reason the bypass was written.

**Files:**
- Modify: `render.yaml`
- Modify: `service/README.md`

**Interfaces:**
- Consumes: `AED_NOTIFY_DB`, read by `store.db_path()`.
- Produces: no code interface. A deployment where the database and the key both survive a restart.

- [ ] **Step 1: Attach a disk and move the database onto it**

Replace `render.yaml` with:

```yaml
services:
  - type: web
    name: aed-notification-service
    env: docker
    dockerfilePath: service/Dockerfile
    # A paid plan is required: free instances have no persistent disk and sleep when
    # idle. On free, /tmp was wiped on every restart, which is what the removed
    # auto-provisioning in store.authenticate existed to paper over -- and sleeping
    # instances also mean the periodic checker does not reliably run.
    plan: starter
    region: frankfurt
    disk:
      name: aed-notify-data
      mountPath: /data
      sizeGB: 1
    envVars:
      - key: AED_NOTIFY_KEY
        sync: false          # set once by hand; see the warning in service/README.md
      - key: AED_NOTIFY_DB
        value: /data/notify.db
```

`sync: false` stops Render generating the value, so the key is entered deliberately and
can be recorded somewhere you control.

- [ ] **Step 2: Write down what losing the key costs**

Add to `service/README.md`:

```markdown
## AED_NOTIFY_KEY

Every stored Discord webhook is encrypted with this Fernet key. It is set by hand on
the service and **is not recoverable from anywhere else**. If it is lost or changed,
every stored webhook becomes permanently undecryptable: notifications stop and each
user has to re-enter their webhook in the app.

Keep a copy somewhere separate from the host. Generate one with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

The service refuses to start without it, deliberately — a generated-on-boot key would
appear to work until the next restart and then silently orphan every stored webhook.
```

- [ ] **Step 3: Verify the database path is honoured**

Run:

```bash
py -c "import os; os.environ['AED_NOTIFY_DB']='/data/notify.db'; from service import store; print(store.db_path())"
```

Expected: `/data/notify.db` — confirming `store.db_path()` reads the variable rather
than a hardcoded path.

- [ ] **Step 4: Confirm nothing still points at /tmp**

Run: `git grep -n "/tmp/notify" -- . ":!docs"`
Expected: no matches.

- [ ] **Step 5: Commit**

```bash
git add render.yaml service/README.md
git commit -m "fix: persist the notification database and require a hand-set key"
```

---

## Verification

```bash
py -m unittest discover -s service/tests -v      # 45 -> 54
py -m unittest discover -s tests                 # 140 -> 144
py tools/lint.py
```

Then prove the bypass is gone against a running service:

```bash
$env:AED_NOTIFY_KEY = (py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
$env:AED_NOTIFY_DB = "$env:TEMP\notify-check.db"
py -m uvicorn service.api:app --port 8000
```

In another shell, present invented credentials of exactly the shape that used to work:

```bash
curl -i -X PUT localhost:8000/v1/subscribers/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/watchlist -H "Authorization: Bearer bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" -H "Content-Type: application/json" -d "{\"items\":[]}"
```

Expected: **401**. Before this plan it returned 200 and created a subscriber. Confirm
none was created:

```bash
py -c "import sqlite3,os; print(sqlite3.connect(os.environ['AED_NOTIFY_DB']).execute('SELECT COUNT(*) FROM subscribers').fetchone())"
```

Expected: `(0,)`.

Then register properly and confirm the same endpoint now works, and that re-registering
with the same webhook returns the same id:

```bash
curl -s -X POST localhost:8000/v1/subscribers -H "Content-Type: application/json" -d "{\"webhook\":\"https://discord.com/api/webhooks/1/aaaabbbbccccdddd\"}"
```

Run it twice: the `id` must match both times and the `token` must differ. That is the
recovery path Task 1 relies on.

## Deploy order, and who breaks

1. **Task 1 first**, released to users. An updated client re-registers by itself when
   the service rejects it.
2. **Tasks 2-4 together**, deployed to the service.

Between those two, clients that have not updated will get `401` and lose cloud
notifications until they update — they have no re-registration logic. That is the
deliberate trade: the alternative is leaving an open authentication hole on a live
service. Consider telling users in the release notes that cloud notifications need the
update.

Task 4 moves to a paid Render plan. If that is not wanted, the service needs a
different host with a persistent disk — but it cannot stay on ephemeral storage now
that the workaround is gone: every restart would drop all subscribers, and each client
would silently re-register on its next call.

## Not covered here

`create_subscriber` is unauthenticated and ids are `sha256(webhook)[:32]`, so anyone
who knows a webhook URL can register it and receive a valid token for that subscriber,
gaining access to that subscriber's watchlist and command queue. Knowing the webhook
already allows posting to the channel, so this is not a new secret — but it is a wider
grant than it looks, and it is what makes Task 1's recovery possible. Worth a separate
decision about whether registration should invalidate existing tokens for that id.
