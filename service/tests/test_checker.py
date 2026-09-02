import base64
import os
import unittest
from unittest.mock import patch

# Isolation is by schema: reset_for_tests() refuses to touch "public". Set here as
# well as in __init__.py because `unittest discover -s service/tests` imports these as
# top-level modules, so the package __init__ never runs.
os.environ.setdefault("AED_NOTIFY_SCHEMA", "aed_test")
os.environ.setdefault("AED_NOTIFY_KEY", "bXl0ZXN0a2V5MTIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM=")

from service import checker, store

WEBHOOK = "https://discord.com/api/webhooks/123456789/abcdefghijklmnop1234"


class EpisodeHtmlExtractionTests(unittest.TestCase):
    def test_extracts_from_witanime_onclick_handlers(self):
        url1 = "https://witanime.life/episode/bleach-sennen-kessen-hen-الحلقة-1/"
        url2 = "https://witanime.life/episode/bleach-sennen-kessen-hen-الحلقة-26/"
        enc1 = base64.b64encode(url1.encode()).decode()
        enc2 = base64.b64encode(url2.encode()).decode()
        
        html = f"""
        <div>
            <a onclick="openEpisode('{enc1}')">Ep 1</a>
            <a onclick="openEpisode('{enc2}')">Ep 26</a>
        </div>
        """
        episodes = checker.extract_episodes_from_html(html)
        self.assertEqual(episodes, [1, 26])

    def test_extracts_from_animerco_direct_hrefs(self):
        html = """
        <div>
            <a href="https://eta.animerco.org/episodes/jujutsu-kaisen-الحلقة-1/">الحلقة 1</a>
            <a href="https://eta.animerco.org/episodes/jujutsu-kaisen-الحلقة-12/">الحلقة 12</a>
            <a href="https://eta.animerco.org/anime-genre/action/">Action</a>
        </div>
        """
        episodes = checker.extract_episodes_from_html(html)
        self.assertEqual(episodes, [1, 12])

    def test_handles_empty_or_non_episode_html(self):
        html = "<html><body><h1>No episodes here</h1></body></html>"
        self.assertEqual(checker.extract_episodes_from_html(html), [])


class DiscordEmbedTests(unittest.TestCase):
    def test_embed_structure(self):
        payload = checker.create_discord_embed("One Piece", "https://witanime.life/anime/one-piece/", 1100)
        self.assertIn("embeds", payload)
        embed = payload["embeds"][0]
        self.assertIn("New Episode", embed["title"])
        self.assertIn("One Piece", embed["description"])
        self.assertIn("1100", embed["description"])
        self.assertEqual(embed["color"], 0x4CC2FF)


class CheckerProcessingTests(unittest.TestCase):
    def setUp(self):
        store.reset_for_tests()

    @patch("service.checker.send_discord_notification")
    @patch("service.checker.fetch_latest_episode")
    def test_first_discovery_seeds_without_notifying(self, mock_fetch, mock_notify):
        mock_fetch.return_value = 24
        mock_notify.return_value = True

        sid, _ = store.create_subscriber(WEBHOOK)
        anime_url = "https://witanime.life/anime/solo-leveling/"
        store.replace_follows(sid, [{"url": anime_url, "title": "Solo Leveling"}])

        anime_row = store.due_anime()[0]
        self.assertEqual(anime_row["last_seen_max"], 0)

        sent = checker.process_anime(anime_row)
        self.assertEqual(sent, 0)
        self.assertEqual(mock_notify.call_count, 0)

        # last_seen_max should now be updated to 24 in database
        updated_row = store.due_anime()[0]
        self.assertEqual(updated_row["last_seen_max"], 24)

    @patch("service.checker.send_discord_notification")
    @patch("service.checker.fetch_latest_episode")
    def test_new_episode_triggers_notification(self, mock_fetch, mock_notify):
        mock_notify.return_value = True

        sid, _ = store.create_subscriber(WEBHOOK)
        anime_url = "https://witanime.life/anime/solo-leveling/"
        store.replace_follows(sid, [{"url": anime_url, "title": "Solo Leveling"}])
        
        # Seed to 24
        store.update_anime_progress(anime_url, 24)
        store.advance_notified_max(sid, anime_url, 24)

        # Now site publishes episode 25
        mock_fetch.return_value = 25
        anime_row = store.due_anime()[0]
        self.assertEqual(anime_row["last_seen_max"], 24)

        sent = checker.process_anime(anime_row)
        self.assertEqual(sent, 1)
        self.assertEqual(mock_notify.call_count, 1)

        # Verify notified_max was advanced to 25
        with store.get_db() as db:
            notif_max = db.execute("SELECT notified_max FROM follows WHERE subscriber_id=%s", (sid,)).fetchone()[0]
            self.assertEqual(notif_max, 25)

    @patch("service.checker.send_discord_notification")
    @patch("service.checker.fetch_latest_episode")
    def test_multiple_episodes_increase(self, mock_fetch, mock_notify):
        mock_notify.return_value = True

        sid, _ = store.create_subscriber(WEBHOOK)
        anime_url = "https://witanime.life/anime/demon-slayer/"
        store.replace_follows(sid, [{"url": anime_url, "title": "Demon Slayer"}])
        store.update_anime_progress(anime_url, 10)
        store.advance_notified_max(sid, anime_url, 10)

        # Batch release of episodes 11 and 12
        mock_fetch.return_value = 12
        anime_row = store.due_anime()[0]
        sent = checker.process_anime(anime_row)

        self.assertEqual(sent, 2)
        self.assertEqual(mock_notify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
