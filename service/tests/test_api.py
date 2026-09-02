import os
import unittest

# Isolation is by schema: reset_for_tests() refuses to touch "public". Set here as
# well as in __init__.py because `unittest discover -s service/tests` imports these as
# top-level modules, so the package __init__ never runs.
os.environ.setdefault("AED_NOTIFY_SCHEMA", "aed_test")
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

from fastapi.testclient import TestClient

from service import store
from service.api import app

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"


class HealthCheckTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_reports_ok(self):
        r = self.client.get("/v1/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_root_reports_online(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "online")

    def test_healthz_reports_ok(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})


class RegistrationApiTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.client = TestClient(app)

    def register(self):
        r = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK})
        self.assertEqual(r.status_code, 201)
        return r.json()

    def test_registration_returns_id_token_masked_webhook(self):
        body = self.register()
        self.assertTrue(body["id"])
        self.assertTrue(body["token"])
        self.assertTrue(body["webhook"].endswith("1234"))
        self.assertNotIn("abcdefghijklmnop", body["webhook"])

    def test_registration_never_echoes_the_raw_webhook(self):
        r = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK})
        self.assertNotIn("abcdefghijklmnop", r.text)

    def test_rejects_non_discord_urls(self):
        r = self.client.post("/v1/subscribers", json={"webhook": "https://attacker.example/hook"})
        self.assertEqual(r.status_code, 422)

    def test_watchlist_sync_requires_token(self):
        body = self.register()
        r = self.client.put(
            f"/v1/subscribers/{body['id']}/watchlist",
            json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]})
        self.assertEqual(r.status_code, 401)

    def test_watchlist_sync_succeeds_with_bearer_token(self):
        body = self.register()
        r = self.client.put(
            f"/v1/subscribers/{body['id']}/watchlist",
            headers={"Authorization": f"Bearer {body['token']}"},
            json={"items": [
                {"url": "https://witanime.life/anime/x/", "title": "X"},
                {"url": "https://witanime.life/anime/y/", "title": "Y"}
            ]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"following": 2})

    def test_sync_replaces_watchlist(self):
        body = self.register()
        head = {"Authorization": f"Bearer {body['token']}"}
        url = f"/v1/subscribers/{body['id']}/watchlist"
        self.client.put(url, headers=head,
                        json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]})
        r = self.client.put(url, headers=head,
                            json={"items": [{"url": "https://witanime.life/anime/y/", "title": "Y"}]})
        self.assertEqual(r.json(), {"following": 1})


class DeletionApiTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.client = TestClient(app)

    def register_with_follow(self):
        body = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK}).json()
        self.client.put(
            f"/v1/subscribers/{body['id']}/watchlist",
            headers={"Authorization": f"Bearer {body['token']}"},
            json={"items": [{"url": "https://witanime.life/anime/x/", "title": "X"}]})
        return body

    def test_delete_requires_token(self):
        body = self.register_with_follow()
        r = self.client.delete(f"/v1/subscribers/{body['id']}")
        self.assertEqual(r.status_code, 401)

    def test_delete_removes_subscriber_and_prunes_orphans(self):
        body = self.register_with_follow()
        r = self.client.delete(
            f"/v1/subscribers/{body['id']}",
            headers={"Authorization": f"Bearer {body['token']}"})
        self.assertEqual(r.status_code, 204)
        with store.get_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM follows").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM anime").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
