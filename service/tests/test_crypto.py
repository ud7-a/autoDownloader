import os
import tempfile
import unittest

os.environ.setdefault("AED_NOTIFY_DB",
                      os.path.join(tempfile.gettempdir(), "aed-notify-tests", "notify.db"))
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

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


if __name__ == "__main__":
    unittest.main()
