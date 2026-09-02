import os
import unittest

# Isolation is by schema: reset_for_tests() refuses to touch "public". Set here as
# well as in __init__.py because `unittest discover -s service/tests` imports these as
# top-level modules, so the package __init__ never runs.
os.environ.setdefault("AED_NOTIFY_SCHEMA", "aed_test")
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

from service import crypto


class SigningKeyTests(unittest.TestCase):
    """sign_action fell back to a constant that is published in this repository, so a
    deployment without the key had forgeable action signatures."""

    def test_signing_without_a_key_raises(self):
        saved = os.environ.pop("AED_NOTIFY_KEY", None)
        try:
            with self.assertRaises(crypto.MissingKeyError):
                crypto.sign_action("sub", "act")
        finally:
            if saved is not None:
                os.environ["AED_NOTIFY_KEY"] = saved

    def test_verifying_without_a_key_is_false_not_a_crash(self):
        """Verification runs on a request path; a missing key must reject, not 500."""
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

    def test_an_empty_signature_is_rejected(self):
        self.assertFalse(crypto.verify_action("sub", "act", ""))


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


if __name__ == "__main__":
    unittest.main()
