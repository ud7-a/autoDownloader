import os
import unittest

# Isolation is by schema: reset_for_tests() refuses to touch "public". Set here as
# well as in __init__.py because `unittest discover -s service/tests` imports these as
# top-level modules, so the package __init__ never runs.
os.environ.setdefault("AED_NOTIFY_SCHEMA", "aed_test")
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

from service import store

URL = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"


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
        with store.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)

    def test_short_and_long_ids_are_both_rejected(self):
        self.assertFalse(store.authenticate("nobody", "anything"))
        self.assertFalse(store.authenticate("e" * 64, "f" * 64))

    def test_a_blank_token_hash_cannot_be_claimed(self):
        """A row with no token_hash used to accept the first token offered and adopt
        it. Such rows exist from earlier auto-provisioning."""
        sid, _ = store.create_subscriber(URL)
        with store.get_db() as db:
            db.execute("UPDATE subscribers SET token_hash='' WHERE id=%s", (sid,))
        self.assertFalse(store.authenticate(sid, "g" * 40))
        with store.get_db() as db:
            self.assertEqual(
                db.execute("SELECT token_hash FROM subscribers WHERE id=%s", (sid,)).fetchone()[0], "")

    def test_a_real_subscriber_still_authenticates(self):
        sid, token = store.create_subscriber(URL)
        self.assertTrue(store.authenticate(sid, token))
        self.assertFalse(store.authenticate(sid, "not-the-token"))


class SubscriberStoreTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()

    def test_create_returns_an_id_and_a_token(self):
        sid, token = store.create_subscriber(URL)
        self.assertTrue(sid)
        self.assertTrue(token)
        self.assertNotEqual(sid, token)

    def test_the_right_token_authenticates(self):
        sid, token = store.create_subscriber(URL)
        self.assertTrue(store.authenticate(sid, token))

    def test_a_wrong_token_does_not(self):
        sid, _ = store.create_subscriber(URL)
        self.assertFalse(store.authenticate(sid, "not-the-token"))

    def test_an_unknown_subscriber_does_not_authenticate(self):
        self.assertFalse(store.authenticate("nobody", "anything"))

    def test_webhook_round_trips(self):
        sid, _ = store.create_subscriber(URL)
        self.assertEqual(store.get_webhook(sid), URL)

    def test_webhook_is_not_stored_in_the_clear(self):
        """The strongest guarantee this layer offers: a stolen database file does not
        hand over everyone's Discord channels."""
        sid, _ = store.create_subscriber(URL)
        with store.get_db() as db:
            raw = db.execute("SELECT webhook_enc FROM subscribers WHERE id=%s", (sid,)).fetchone()[0]
        self.assertNotIn(b"discord.com", raw)

    def test_token_is_not_stored_in_the_clear(self):
        sid, token = store.create_subscriber(URL)
        with store.get_db() as db:
            stored = db.execute("SELECT token_hash FROM subscribers WHERE id=%s", (sid,)).fetchone()[0]
        self.assertNotIn(token, stored)

    def test_set_webhook_replaces_it(self):
        other = "https://discord.com/api/webhooks/999/zzzzzzzzzzzzzzzz9999"
        sid, _ = store.create_subscriber(URL)
        store.set_webhook(sid, other)
        self.assertEqual(store.get_webhook(sid), other)

    def test_delete_subscriber_removes_follows_and_subscriber(self):
        sid, _ = store.create_subscriber(URL)
        store.replace_follows(sid, [{"url": "https://witanime.life/anime/bleach/", "title": "Bleach"}])
        store.delete_subscriber(sid)
        with store.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM follows").fetchone()[0], 0)

    def test_prune_orphan_anime(self):
        sid, _ = store.create_subscriber(URL)
        store.replace_follows(sid, [{"url": "https://witanime.life/anime/bleach/", "title": "Bleach"}])
        store.delete_subscriber(sid)
        pruned = store.prune_orphan_anime()
        self.assertEqual(pruned, 1)
        self.assertEqual(len(store.due_anime()), 0)

    def test_shared_anime_rows_between_subscribers(self):
        s1, _ = store.create_subscriber(URL)
        s2, _ = store.create_subscriber("https://discord.com/api/webhooks/999/different_subscriber")
        items = [{"url": "https://witanime.life/anime/bleach/", "title": "Bleach"}]
        store.replace_follows(s1, items)
        store.replace_follows(s2, items)
        with store.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM anime").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM follows").fetchone()[0], 2)
        self.assertEqual(len(store.followers_of("https://witanime.life/anime/bleach/")), 2)

    def test_notification_query_and_advancement(self):
        sid, _ = store.create_subscriber(URL)
        anime_url = "https://witanime.life/anime/naruto/"
        store.replace_follows(sid, [{"url": anime_url, "title": "Naruto"}])
        
        # Initially notified_max = 0
        to_notify = store.subscribers_to_notify(anime_url, 12)
        self.assertEqual(len(to_notify), 1)
        self.assertEqual(to_notify[0][0], sid)
        self.assertEqual(to_notify[0][1], URL)
        
        # Advance notification
        store.advance_notified_max(sid, anime_url, 12)
        store.update_anime_progress(anime_url, 12)
        
        # Should not notify for episode 12 anymore
        self.assertEqual(len(store.subscribers_to_notify(anime_url, 12)), 0)
        # Should notify if new episode 13 appears
        self.assertEqual(len(store.subscribers_to_notify(anime_url, 13)), 1)

    def test_due_anime_filters_by_today_day(self):
        sid, _ = store.create_subscriber(URL)
        items = [
            {"url": "https://example.com/sat", "title": "Sat Show", "release_day": "saturday"},
            {"url": "https://example.com/sun", "title": "Sun Show", "release_day": "sunday"},
            {"url": "https://example.com/unknown", "title": "Unknown Day Show", "release_day": ""}
        ]
        store.replace_follows(sid, items)

        # On Saturday: should return Sat Show and Unknown Day Show, but NOT Sun Show
        due_sat = store.due_anime(today_day="saturday")
        due_urls_sat = {a["url"] for a in due_sat}
        self.assertIn("https://example.com/sat", due_urls_sat)
        self.assertIn("https://example.com/unknown", due_urls_sat)
        self.assertNotIn("https://example.com/sun", due_urls_sat)

        # On Sunday: should return Sun Show and Unknown Day Show, but NOT Sat Show
        due_sun = store.due_anime(today_day="sunday")
        due_urls_sun = {a["url"] for a in due_sun}
        self.assertIn("https://example.com/sun", due_urls_sun)
        self.assertIn("https://example.com/unknown", due_urls_sun)
        self.assertNotIn("https://example.com/sat", due_urls_sun)


if __name__ == "__main__":
    unittest.main()
