import os
import tempfile
import unittest

os.environ.setdefault("AED_NOTIFY_DB",
                      os.path.join(tempfile.gettempdir(), "aed-notify-tests", "notify.db"))
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

from fastapi.testclient import TestClient

from service import crypto, store
from service.api import app

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"


class RemoteCommandTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()
        self.client = TestClient(app)
        self.reg = self.client.post("/v1/subscribers", json={"webhook": WEBHOOK}).json()
        self.sid = self.reg["id"]
        self.token = self.reg["token"]
        self.auth_headers = {"Authorization": f"Bearer {self.token}"}

    def test_heartbeat_and_online_status(self):
        # Freshly registered subscriber is online
        self.assertTrue(store.is_subscriber_online(self.sid))
        # With zero timeout it behaves as offline
        self.assertFalse(store.is_subscriber_online(self.sid, timeout_seconds=0))
        # Send heartbeat
        r = self.client.post(f"/v1/subscribers/{self.sid}/heartbeat", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(store.is_subscriber_online(self.sid))

    def test_queue_and_fetch_commands(self):
        # 1. Queue a download command via public web action
        url = "https://witanime.life/anime/rezero/"
        ep = "15"
        sig = crypto.sign_action(self.sid, f"{url}:{ep}")
        r = self.client.get(f"/v1/queue?sid={self.sid}&url={url}&title=ReZero&ep={ep}&sig={sig}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Download Queued", r.text)

        # 2. Fetch pending commands from desktop app endpoint
        r = self.client.get(f"/v1/subscribers/{self.sid}/commands", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        cmds = r.json().get("commands", [])
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0]["anime_url"], url)
        self.assertEqual(cmds[0]["episodes"], ep)

        # 3. Acknowledge command execution
        cmd_id = cmds[0]["id"]
        r = self.client.post(f"/v1/subscribers/{self.sid}/commands/{cmd_id}/ack", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)

        # 4. Verify queue is now empty
        r = self.client.get(f"/v1/subscribers/{self.sid}/commands", headers=self.auth_headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json().get("commands", [])), 0)

    def test_invalid_signature_is_rejected(self):
        url = "https://witanime.life/anime/rezero/"
        r = self.client.get(f"/v1/queue?sid={self.sid}&url={url}&title=ReZero&ep=15&sig=fake_sig")
        self.assertEqual(r.status_code, 401)

    def test_delete_subscriber_cascades_commands(self):
        # Queue a command
        store.queue_command(self.sid, "https://witanime.life/anime/rezero/", "ReZero", "15")
        self.assertEqual(len(store.get_pending_commands(self.sid)), 1)

        # Delete subscriber
        r = self.client.delete(f"/v1/subscribers/{self.sid}", headers=self.auth_headers)
        self.assertEqual(r.status_code, 204)

        # Verify command was cascaded
        self.assertEqual(len(store.get_pending_commands(self.sid)), 0)

    def test_is_subscriber_online_timeout(self):
        store.record_heartbeat(self.sid)
        self.assertTrue(store.is_subscriber_online(self.sid, timeout_seconds=10))
        # With timeout of -1s, it should report offline
        self.assertFalse(store.is_subscriber_online(self.sid, timeout_seconds=-1))
