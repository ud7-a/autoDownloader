"""Key material and the handling of Discord webhook URLs.

A webhook URL is a credential: anyone holding it can post into that user's channel.
So it is encrypted at rest here, and the plaintext exists only in memory, only for as
long as it takes to send a message. It is never returned by an endpoint, never logged,
and never put in an exception message. mask_webhook() is what a user is shown instead.
"""

import hashlib
import hmac
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
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt_webhook(url: str) -> bytes:
    return _fernet().encrypt(url.encode("utf-8"))


def decrypt_webhook(blob: bytes) -> str:
    return _fernet().decrypt(blob).decode("utf-8")


def mask_webhook(url: str) -> str:
    """A form safe to show a user or put in a log: last four characters only."""
    if not url:
        return ""
    return "https://discord.com/api/webhooks/…" + url[-4:]


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_action(subscriber_id: str, action: str) -> str:
    key = os.environ.get("AED_NOTIFY_KEY")
    if not key:
        # There used to be a literal default here. This repository is public, so that
        # made every action signature forgeable on any deployment missing the
        # variable -- while _fernet() above correctly refused to run without it.
        raise MissingKeyError("AED_NOTIFY_KEY is not set")
    msg = f"{subscriber_id}:{action}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def verify_action(subscriber_id: str, action: str, sig: str) -> bool:
    # Verification sits on a request path, so a missing key rejects the request
    # rather than raising up through the handler as a 500.
    try:
        expected = sign_action(subscriber_id, action)
    except MissingKeyError:
        return False
    return hmac.compare_digest(expected, sig or "")
