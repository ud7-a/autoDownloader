"""Point the service at an isolated schema BEFORE any service module is imported.

Mirrors tests/__init__.py in the desktop app: a test must never touch real data.

On the free tier the service and its tests share one Postgres database, so isolation
is by schema rather than by file. Tests run in `aed_test`, and reset_for_tests()
refuses to delete anything while the schema is `public` -- without that, running the
suite would wipe every live subscriber.
"""

import os

os.environ["AED_NOTIFY_SCHEMA"] = "aed_test"
# A fixed 32-byte urlsafe-base64 key so tests are deterministic. Never used outside tests.
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

if not os.environ.get("DATABASE_URL"):
    raise RuntimeError(
        "DATABASE_URL is not set. The service tests run against a real Postgres "
        "database (in the 'aed_test' schema, never 'public'). Set DATABASE_URL to a "
        "session-pooler connection string before running them."
    )
