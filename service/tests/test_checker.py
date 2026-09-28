import base64
import json
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

    def test_encoded_arabic_slug_still_matches_its_episodes(self):
        """The base URL keeps its percent-encoding; the links are compared decoded.
        Comparing the two as-is filtered out every episode, silently."""
        base = "https://det.animerco.org/movies/%d9%81%d9%8a%d9%84%d9%85-kimi-no-na-wa/"
        html = """
        <a href="https://det.animerco.org/episodes/%d9%81%d9%8a%d9%84%d9%85-kimi-no-na-wa-%d8%a7%d9%84%d8%ad%d9%84%d9%82%d8%a9-1/">1</a>
        <a href="https://det.animerco.org/episodes/other-show-%d8%a7%d9%84%d8%ad%d9%84%d9%82%d8%a9-9/">other</a>
        """
        self.assertEqual(checker.extract_episodes_from_html(html, base), [1])

    def test_plain_slug_still_filters_other_shows(self):
        base = "https://eta.animerco.org/animes/jujutsu-kaisen/"
        html = """
        <a href="https://eta.animerco.org/episodes/jujutsu-kaisen-الحلقة-3/">3</a>
        <a href="https://eta.animerco.org/episodes/one-piece-الحلقة-1100/">sidebar</a>
        """
        self.assertEqual(checker.extract_episodes_from_html(html, base), [3])

    def test_season_page_without_season_in_episode_links(self):
        """animerco season 1: page is ...-season-1/, links are ...-الحلقة-N/. The
        slug filter used to drop them all (live: Slime season 1 read as 0)."""
        base = "https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-1/"
        html = """
        <a href="https://det.animerco.org/episodes/tensei-shitara-slime-datta-ken-الحلقة-1/">1</a>
        <a href="https://det.animerco.org/episodes/tensei-shitara-slime-datta-ken-الحلقة-24/">24</a>
        <a href="https://det.animerco.org/episodes/one-piece-الحلقة-1100/">sidebar</a>
        """
        self.assertEqual(checker.extract_episodes_from_html(html, base), [1, 24])

    def test_episode_number_is_not_the_trailing_season(self):
        """Boruto-style links end in the season: ...-الحلقة-14-الموسم-1/."""
        base = "https://det.animerco.org/seasons/boruto-naruto-next-generations-season-1/"
        html = '<a href="https://det.animerco.org/episodes/boruto-naruto-next-generations-الحلقة-14-الموسم-1/">x</a>'
        self.assertEqual(checker.extract_episodes_from_html(html, base), [14])

    def test_handles_empty_or_non_episode_html(self):
        html = "<html><body><h1>No episodes here</h1></body></html>"
        self.assertEqual(checker.extract_episodes_from_html(html), [])


class SeasonAndFallbackTests(unittest.TestCase):
    ANIME = "https://det.animerco.org/animes/tensei-shitara-slime-datta-ken/"
    ANIME_PAGE = """
    <a href="https://det.animerco.org/seasons/">all seasons</a>
    <a href="https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-ova/">OVA</a>
    <a href="https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-1/">S1</a>
    <a href="https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-3/">S3</a>
    <a href="https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-2/">S2</a>
    <a href="https://det.animerco.org/seasons/one-piece-season-20/">sidebar: other show</a>
    """

    def test_newest_season_of_this_show_is_picked(self):
        self.assertEqual(checker.latest_season_url(self.ANIME_PAGE, self.ANIME),
                         "https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-3/")

    def test_ova_only_show_takes_the_last_listed(self):
        page = '<a href="/seasons/kimi-no-na-wa-ova/">OVA</a>'
        self.assertEqual(checker.latest_season_url(page, "https://det.animerco.org/animes/kimi-no-na-wa/"),
                         "https://det.animerco.org/seasons/kimi-no-na-wa-ova/")

    def test_no_seasons_or_other_shows_only(self):
        self.assertEqual(checker.latest_season_url("", self.ANIME), "")
        self.assertEqual(checker.latest_season_url(
            '<a href="https://det.animerco.org/seasons/one-piece-season-20/">x</a>', self.ANIME), "")

    def _only_httpx(self, pages):
        """Run fetch_latest_episode with every fetcher but httpx failing, and httpx
        answering from `pages` {url: (status, html)}."""
        def fake_httpx(url, headers, client, seen):
            status, html = pages[url]
            seen["status"], seen["html"] = status, html
            eps = checker.extract_episodes_from_html(html, url) if status == 200 else []
            return max(eps) if eps else 0
        calls = []

        def fail(name):
            def f(*a, **k):
                calls.append(name)
                seen = k.get("seen")
                if seen is not None:
                    seen["error"] = "blocked"
                return (0, {}) if name == "playwright" else 0
            return f
        return fake_httpx, fail, calls

    def test_anime_page_follows_its_newest_season_and_stops_early(self):
        season3 = "https://det.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-3/"
        pages = {self.ANIME: (200, self.ANIME_PAGE),
                 season3: (200, '<a href="https://det.animerco.org/episodes/tensei-shitara-slime-datta-ken-season-3-الحلقة-7/">7</a>')}
        fake_httpx, fail, calls = self._only_httpx(pages)
        trace = []
        with patch.dict(os.environ, {"SCRAPER_API_KEY": ""}), \
             patch("service.checker._fetch_curl_cffi", fail("curl_cffi")), \
             patch("service.checker._fetch_httpx", fake_httpx), \
             patch("service.checker.fetch_with_proxy_rotation", fail("proxy")), \
             patch("service.checker.fetch_with_playwright", fail("playwright")):
            self.assertEqual(checker.fetch_latest_episode(self.ANIME, trace=trace), 7)
        # The anime page was genuine, so the slow proxy/playwright steps never ran for it.
        self.assertNotIn("proxy", calls)
        self.assertNotIn("playwright", calls)
        self.assertTrue(any(t.get("following_season") == season3 for t in trace))

    def test_a_404_ends_the_search_immediately(self):
        bad = "https://det.animerco.org/animes/no-such-show/"
        fake_httpx, fail, calls = self._only_httpx({bad: (404, "<html>not found</html>")})
        with patch.dict(os.environ, {"SCRAPER_API_KEY": ""}), \
             patch("service.checker._fetch_curl_cffi", fail("curl_cffi")), \
             patch("service.checker._fetch_httpx", fake_httpx), \
             patch("service.checker.fetch_with_proxy_rotation", fail("proxy")), \
             patch("service.checker.fetch_with_playwright", fail("playwright")):
            self.assertEqual(checker.fetch_latest_episode(bad), 0)
        self.assertEqual(calls, ["curl_cffi"])       # httpx 404 -> stop; no proxies, no browser

    def test_a_blocked_page_still_tries_every_fetcher(self):
        """Unchanged behaviour when nothing genuine came back."""
        url = "https://witanime.site/anime/x"
        fake_httpx, fail, calls = self._only_httpx({url: (403, "<title>Just a moment...</title>")})
        with patch.dict(os.environ, {"SCRAPER_API_KEY": ""}), \
             patch("service.checker._fetch_curl_cffi", fail("curl_cffi")), \
             patch("service.checker._fetch_httpx", fake_httpx), \
             patch("service.checker.fetch_with_proxy_rotation", fail("proxy")), \
             patch("service.checker.fetch_with_playwright", fail("playwright")):
            self.assertEqual(checker.fetch_latest_episode(url), 0)
        self.assertEqual(calls, ["curl_cffi", "proxy", "playwright"])

    def test_scraperapi_errors_never_carry_the_key(self):
        seen = {}

        class Boom(Exception):
            pass

        with patch("service.checker.httpx.Client", side_effect=Boom("http://api.scraperapi.com?api_key=SECRET123")), \
             self.assertLogs("aed_checker", level="WARNING") as logs:
            checker.fetch_with_scraperapi("https://x/", "SECRET123", seen=seen)
        self.assertNotIn("SECRET123", json.dumps(seen))
        self.assertNotIn("SECRET123", "\n".join(logs.output))     # nor in Render's logs


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
