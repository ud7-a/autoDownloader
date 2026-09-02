import os
import unittest
from unittest.mock import patch

# Isolation is by schema: reset_for_tests() refuses to touch "public". Set here as
# well as in __init__.py because `unittest discover -s service/tests` imports these as
# top-level modules, so the package __init__ never runs.
os.environ.setdefault("AED_NOTIFY_SCHEMA", "aed_test")
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

from fastapi.testclient import TestClient

from service import checker, store
from service.api import app

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"


class EndToEndCloudServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.client = TestClient(app)

    @patch("service.checker.send_discord_notification")
    @patch("service.checker.fetch_latest_episode")
    def test_full_subscriber_lifecycle_and_notification(self, mock_fetch, mock_notify):
        mock_notify.return_value = True

        # 1. Health check
        r = self.client.get("/v1/health")
        self.assertEqual(r.status_code, 200)

        # 2. Register subscriber
        r = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK})
        self.assertEqual(r.status_code, 201)
        data = r.json()
        sub_id, token = data["id"], data["token"]
        self.assertTrue(sub_id)
        self.assertTrue(token)

        # 3. Sync watchlist with 2 anime
        anime_url_1 = "https://witanime.life/anime/bleach/"
        anime_url_2 = "https://eta.animerco.org/animes/naruto/"
        watchlist = [
            {"url": anime_url_1, "title": "Bleach"},
            {"url": anime_url_2, "title": "Naruto"}
        ]
        r = self.client.put(
            f"/v1/subscribers/{sub_id}/watchlist",
            headers={"Authorization": f"Bearer {token}"},
            json={"items": watchlist}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["following"], 2)

        # 4. First checker cycle: Seeds initial catalogue baseline (e.g. Bleach ep 10, Naruto ep 20) without spamming
        def mock_fetch_side_effect(url, client=None):
            if "bleach" in url: return 10
            if "naruto" in url: return 20
            return 0
        mock_fetch.side_effect = mock_fetch_side_effect

        stats = checker.run_checker_cycle()
        self.assertEqual(stats["checked"], 2)
        self.assertEqual(stats["notifications_sent"], 0)
        self.assertEqual(mock_notify.call_count, 0)

        # 5. Second cycle: Bleach drops episode 11!
        def mock_fetch_new_ep(url, client=None):
            if "bleach" in url: return 11
            if "naruto" in url: return 20
            return 0
        mock_fetch.side_effect = mock_fetch_new_ep

        stats = checker.run_checker_cycle()
        self.assertEqual(stats["notifications_sent"], 1)
        self.assertEqual(mock_notify.call_count, 1)

        # 6. Unsubscribe
        r = self.client.delete(f"/v1/subscribers/{sub_id}", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 204)

        # 7. Confirm database is cleaned up
        with store.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM follows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM anime").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
