"""Point the service at a throwaway database BEFORE any service module is imported.

Mirrors tests/__init__.py in the desktop app: a test must never touch real data.
"""

import os
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="aed-notify-tests-"), "notify.db")
os.environ["AED_NOTIFY_DB"] = _DB
# A fixed 32-byte urlsafe-base64 key so tests are deterministic. Never used outside tests.
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")
