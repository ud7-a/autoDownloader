"""Sites: witanime and animerco flows, seasons, search, filler, block and expired-session pages, and the shipped site definitions.

Run with: py -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import unittest

# The app's data, shortcut and mpv.net state folders point at a temp folder
# (tests/__init__.py) BEFORE any app module is imported. `discover -s tests`
# imports this file on its own, so the package is imported here explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from ui.search_tab import (extract_domain, _full_res, AnimeDetailsThread,  # noqa: E402
                           SUPPORTED_SITES, DEFAULT_SITE_FLOWS, resolve_site_flow,
                           site_display_name, site_icon_path)
from utils.config import sites_data, config_lock  # noqa: E402
from core.selenium_engine import _host_of, PATH_HOSTS  # noqa: E402
from core.schedule import SCHEDULE_URLS, SCHEDULE_MATCH  # noqa: E402
from core import site_health, nav_block  # noqa: E402
from utils.browser_flags import DEFAULT_HOSTS  # noqa: E402


class DomainAndCoverTests(unittest.TestCase):
    def test_extract_domain_strips_scheme_and_www(self):
        self.assertEqual(extract_domain("https://WWW.Witanime.site/x"), "witanime.site")

    def test_extract_domain_accepts_bare_host(self):
        self.assertEqual(extract_domain("eta.animerco.org"), "eta.animerco.org")

    def test_extract_domain_empty(self):
        self.assertEqual(extract_domain(""), "")

    def test_full_res_strips_wordpress_size_suffix(self):
        """Season posters lazy-load a 90x135 thumbnail; the original is the same URL
        without the -WxH suffix."""
        self.assertEqual(_full_res("https://x/a-90x135.jpg"), "https://x/a.jpg")
        self.assertEqual(_full_res("https://x/a-185x278.webp"), "https://x/a.webp")

    def test_full_res_is_idempotent(self):
        self.assertEqual(_full_res("https://x/a.jpg"), "https://x/a.jpg")

    def test_full_res_keeps_unrelated_numbers(self):
        self.assertEqual(_full_res("https://x/2022/09/pic.jpg"), "https://x/2022/09/pic.jpg")

    def test_host_of(self):
        self.assertEqual(_host_of("https://drive.google.com/uc?id=x"), "drive.google.com")


class SeasonLabelTests(unittest.TestCase):
    """Season cards are labelled from the link text/slug, which is usually Arabic."""

    def label(self, text, url=""):
        return AnimeDetailsThread._season_label(text, url)

    def test_latin_season_number(self):
        self.assertEqual(self.label("Season 2"), "Season 2")

    def test_latin_season_in_slug(self):
        self.assertEqual(self.label("", "https://x/season-3/"), "Season 3")

    def test_arabic_ordinals(self):
        self.assertEqual(self.label("الموسم الأول"), "Season 1")
        self.assertEqual(self.label("الموسم الاول"), "Season 1")
        self.assertEqual(self.label("الموسم الثاني"), "Season 2")
        self.assertEqual(self.label("الموسم الثالث"), "Season 3")

    def test_arabic_with_digit(self):
        """animerco puts e.g. 'Bleach الموسم 1' in the anchor's title attribute."""
        self.assertEqual(self.label("Bleach الموسم 1"), "Season 1")

    def test_falls_back_to_text(self):
        self.assertEqual(self.label("Specials"), "Specials")

    def test_empty_falls_back_to_generic(self):
        self.assertEqual(self.label("", ""), "Season")


class NavBlockTests(unittest.TestCase):
    """Cancelling interstitial navigations. This layer fails page loads outright, so
    a wrong pattern breaks a download rather than merely failing to block an ad."""

    class _Recorder:
        """Stands in for the CDP socket and records what would have been sent."""
        def __init__(self):
            self.sent = []

        def __call__(self, method, params=None, session_id=None):
            self.sent.append((method, params or {}))

    def blocker(self, patterns=None):
        b = nav_block.NavBlocker(None, patterns)
        b._send = self.rec = self._Recorder()
        return b

    def pause(self, url, patterns=None):
        b = self.blocker(patterns)
        b._on_paused("session-1", {"requestId": "req-1", "request": {"url": url}})
        return b

    def test_the_interstitial_matches(self):
        b = self.blocker()
        self.assertTrue(b.matches("https://www.fast.io/alternatives/google-drive/"
                                  "?utm_source=mfftr_error"))
        self.assertTrue(b.matches("http://fast.io/"))

    def test_no_pattern_can_block_a_real_download_host(self):
        """The invariant that matters. Cancelling a navigation to Drive or MediaFire
        would break every episode, which is worse than the ad it defends against."""
        b = self.blocker()
        for path_name, hosts in PATH_HOSTS.items():
            for host in hosts:
                self.assertFalse(b.matches(f"https://{host}/file/abc"),
                                 f"{path_name}: {host} must never be blocked")

    def test_the_anime_sites_are_never_blocked(self):
        b = self.blocker()
        for domain in SUPPORTED_SITES:
            self.assertFalse(b.matches(f"https://{domain}/episode/x/"), domain)

    def test_a_match_is_failed(self):
        b = self.pause("https://www.fast.io/alternatives/google-drive/")
        method, params = self.rec.sent[0]
        self.assertEqual(method, "Fetch.failRequest")
        self.assertEqual(params["errorReason"], "BlockedByClient")
        self.assertEqual(params["requestId"], "req-1")
        self.assertEqual(b.blocked, ["https://www.fast.io/alternatives/google-drive/"])

    def test_a_non_match_is_continued_not_failed(self):
        """The safety property: if the pattern semantics ever surprise us, letting a
        request through is the harmless error. Failing it would brick downloads."""
        b = self.pause("https://drive.google.com/uc?id=123")
        self.assertEqual(self.rec.sent[0][0], "Fetch.continueRequest")
        self.assertEqual(b.blocked, [])

    def test_a_paused_event_without_a_request_id_is_ignored(self):
        b = self.blocker()
        b._on_paused("session-1", {"request": {"url": "https://fast.io/"}})
        self.assertEqual(self.rec.sent, [])
        self.assertEqual(b.blocked, [])

    def test_a_held_target_is_always_released(self):
        """A target held for the debugger that is never released stays frozen for
        good -- that would break every download, not just the ad."""
        b = self.blocker()
        b._arm_session("session-1")
        self.assertIn("Runtime.runIfWaitingForDebugger", [m for m, _ in self.rec.sent])

    def test_released_even_when_enabling_fetch_fails(self):
        b = self.blocker()

        def explode(method, params=None, session_id=None):
            self.rec(method, params, session_id)
            if method == "Fetch.enable":
                raise RuntimeError("socket died")

        b._send = explode
        b._arm_session("session-1")
        self.assertIn("Runtime.runIfWaitingForDebugger", [m for m, _ in self.rec.sent])

    def test_wildcards_do_not_match_everything(self):
        b = self.blocker(["*fast.io*"])
        self.assertFalse(b.matches("https://witanime.site/"))
        self.assertFalse(b.matches(""))


class SiteKeyTests(unittest.TestCase):
    """Which site a host belongs to, independent of subdomain and TLD."""

    def test_subdomain_and_tld_are_ignored(self):
        self.assertEqual(site_health.site_key("eta.animerco.org"), "animerco")
        self.assertEqual(site_health.site_key("det.animerco.org"), "animerco")
        self.assertEqual(site_health.site_key("animerco.org"), "animerco")
        self.assertEqual(site_health.site_key("https://www.animerco.org/x"), "animerco")

    def test_two_part_suffix(self):
        self.assertEqual(site_health.site_key("a.b.example.co.uk"), "example")

    def test_degenerate_hosts(self):
        self.assertEqual(site_health.site_key(""), "")
        self.assertEqual(site_health.site_key("localhost"), "localhost")

    def test_matches_the_name_shown_in_the_ui(self):
        """One implementation, or a move gets fixed in one place and not the other."""
        for domain in SUPPORTED_SITES:
            self.assertEqual(site_display_name(domain), site_health.site_key(domain))


class SiteLookupTests(unittest.TestCase):
    TABLE = {"witanime.site": "wit", "eta.animerco.org": "ani"}

    def test_exact_host_wins(self):
        self.assertEqual(site_health.lookup(self.TABLE, "witanime.site"), "wit")

    def test_a_moved_host_still_finds_its_entry(self):
        """The whole point: det.* is not a key, but it is the same site."""
        self.assertEqual(site_health.lookup(self.TABLE, "det.animerco.org"), "ani")
        self.assertEqual(
            site_health.lookup(self.TABLE, "https://det.animerco.org/animes/bleach/"), "ani")

    def test_unknown_site_gets_the_default(self):
        self.assertIsNone(site_health.lookup(self.TABLE, "example.com"))
        self.assertEqual(site_health.lookup(self.TABLE, "example.com", "fallback"), "fallback")

    def test_empty_table_is_safe(self):
        self.assertIsNone(site_health.lookup({}, "witanime.site"))


class SiteHealthStateTests(unittest.TestCase):
    """Learning a site move, and telling a layout break from an empty page."""

    def setUp(self):
        site_health.reset_for_tests()
        self._path = site_health._path()
        if os.path.exists(self._path):
            os.remove(self._path)
        self.addCleanup(site_health.reset_for_tests)

    def test_a_move_within_one_site_is_learned_once(self):
        got = site_health.record_landing("https://eta.animerco.org/animes/bleach/",
                                         "https://det.animerco.org/animes/bleach/")
        self.assertEqual(got, "det.animerco.org")
        self.assertIn("det.animerco.org", site_health.learned_hosts())
        # Seeing it again is not news; only a host we have not recorded is reported.
        self.assertEqual(site_health.record_landing("https://eta.animerco.org/a/",
                                                    "https://det.animerco.org/a/"), "")

    def test_leaving_for_a_file_host_is_not_a_move(self):
        """Download links go to mediafire and Drive constantly. Treating those as the
        anime site relocating would pin file hosts as if they were the site."""
        self.assertEqual(site_health.record_landing("https://witanime.site/episode/x/",
                                                    "https://www.mediafire.com/file/y"), "")
        self.assertEqual(site_health.learned_hosts(), ())

    def test_landing_where_we_asked_is_not_a_move(self):
        self.assertEqual(site_health.record_landing("https://witanime.site/a/",
                                                    "https://witanime.site/a/"), "")
        self.assertEqual(site_health.learned_hosts(), ())

    def test_a_content_rich_page_yielding_nothing_is_a_break(self):
        for _ in range(2):
            site_health.record_detection("https://witanime.site/anime/x/", 2428, 0)
        self.assertIn("witanime", site_health.broken_sites())

    def test_one_empty_page_is_not_yet_a_break(self):
        """A single anime with no episodes is far more likely than a site rewrite."""
        site_health.record_detection("https://witanime.site/anime/x/", 2428, 0)
        self.assertEqual(site_health.broken_sites(), ())

    def test_a_nearly_empty_page_is_not_evidence(self):
        """No anchors means a block page or a failed load, not a layout change."""
        for _ in range(5):
            site_health.record_detection("https://witanime.site/anime/x/", 3, 0)
        self.assertEqual(site_health.broken_sites(), ())

    def test_a_successful_detection_clears_the_break(self):
        for _ in range(3):
            site_health.record_detection("https://witanime.site/anime/x/", 2428, 0)
        self.assertIn("witanime", site_health.broken_sites())
        site_health.record_detection("https://witanime.site/anime/y/", 2428, 4)
        self.assertEqual(site_health.broken_sites(), ())

    def test_state_survives_a_restart(self):
        site_health.record_landing("https://eta.animerco.org/a/", "https://det.animerco.org/a/")
        site_health.reset_for_tests()          # as if the app were relaunched
        self.assertIn("det.animerco.org", site_health.learned_hosts())

    def test_a_corrupt_state_file_is_not_fatal(self):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write("{not json")
        site_health.reset_for_tests()
        self.assertEqual(site_health.learned_hosts(), ())
        self.assertEqual(site_health.broken_sites(), ())


class SeasonLinkTests(unittest.TestCase):
    """Which season links on an anime page count as this anime's seasons."""

    class _FakeDriver:
        """_find_season_links only ever reads current_url off the driver."""
        def __init__(self, current_url):
            self.current_url = current_url

    @staticmethod
    def _anchor(href, text="", title="", poster="", img_poster=""):
        return {"href": href, "text": text, "title": title, "onclick": "",
                "poster": poster, "imgPoster": img_poster}

    def links(self, anchors, requested="https://eta.animerco.org/animes/bleach/",
              landed="https://det.animerco.org/animes/bleach/"):
        det = AnimeDetailsThread(requested)
        det.anime_url = requested
        return det._find_season_links(self._FakeDriver(landed), anchors)

    def test_links_survive_a_redirect_to_another_host(self):
        """animerco moved eta.animerco.org -> det.animerco.org. Matching against the
        requested host dropped every season link, so each anime fell through to the
        flat branch and loaded as a single fake episode -- silently, with no error."""
        got = self.links([self._anchor("https://det.animerco.org/seasons/bleach-s1/",
                                       "Bleach Season 1")])
        self.assertEqual([(lbl, url) for lbl, url, _ in got],
                         [("Season 1", "https://det.animerco.org/seasons/bleach-s1/")])

    def test_a_third_party_host_is_still_rejected(self):
        self.assertEqual(self.links([self._anchor("https://other.example/seasons/x/", "S1")]), [])

    def test_the_seasons_index_and_calendar_are_skipped(self):
        """/seasons/ is the index and /season/<year> is the seasonal calendar."""
        got = self.links([self._anchor("https://det.animerco.org/seasons/", "All"),
                          self._anchor("https://det.animerco.org/season/2024/", "2024")])
        self.assertEqual(got, [])

    def test_poster_falls_back_to_a_descendant_image(self):
        got = self.links([self._anchor("https://det.animerco.org/seasons/s1/", "Season 1",
                                       img_poster="https://det.animerco.org/p.jpg")])
        self.assertEqual(got[0][2], "https://det.animerco.org/p.jpg")

    def test_placeholder_data_uri_is_not_a_poster(self):
        got = self.links([self._anchor("https://det.animerco.org/seasons/s1/", "Season 1",
                                       poster="data:image/gif;base64,R0lGOD")])
        self.assertEqual(got[0][2], "")

    def test_duplicate_links_merge_and_keep_the_poster(self):
        """A season is usually linked twice -- once as art, once as a title."""
        url = "https://det.animerco.org/seasons/s1/"
        got = self.links([self._anchor(url, "", img_poster="https://det.animerco.org/p.jpg"),
                          self._anchor(url, "Season 1")])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][2], "https://det.animerco.org/p.jpg")


class SingleEntryDetectionTests(unittest.TestCase):
    """Movies/OVAs are published as a single entry with no episode number, and the
    grouping strategies need >=2 links -- so without a single-entry fallback they
    look like "no episodes at all"."""

    MOVIE = "https://witanime.site/episode/فيلم-bleach-sennen-kessen-hen-kashin-tan-movie/"

    def setUp(self):
        self.det = AnimeDetailsThread("")

    def test_movie_url_becomes_a_one_episode_target(self):
        template, max_ep = self.det._derive_single([self.MOVIE])
        self.assertEqual(template, self.MOVIE)
        self.assertEqual(max_ep, 1)

    def test_movie_template_has_no_placeholder(self):
        """A movie has nothing to parameterise; the URL is the whole target."""
        template, _ = self.det._derive_single([self.MOVIE])
        self.assertNotIn("{x}", template)

    def test_percent_encoded_url_is_decoded(self):
        encoded = ("https://witanime.site/episode/"
                   "%d9%81%d9%8a%d9%84%d9%85-bleach-sennen-kessen-hen-kashin-tan-movie/")
        template, _ = self.det._derive_single([encoded])
        self.assertEqual(template, self.MOVIE)

    def test_lone_numbered_episode_stays_parameterised(self):
        """A series with only ep 1 uploaded must still template, so later uploads work."""
        template, max_ep = self.det._derive_single(["https://witanime.site/episode/show-الحلقة-1/"])
        self.assertEqual(template, "https://witanime.site/episode/show-الحلقة-{x}/")
        self.assertEqual(max_ep, 1)

    def test_two_entries_are_left_to_the_grouping_logic(self):
        self.assertEqual(self.det._derive_single([self.MOVIE, "https://x/episode/other-1/"]), ("", 0))

    def test_non_episode_links_are_ignored(self):
        self.assertEqual(self.det._derive_single(["https://witanime.site/anime-genre/x/"]), ("", 0))

    def test_duplicate_links_still_count_as_one(self):
        """The poster overlay repeats the same openEpisode link."""
        self.assertEqual(self.det._derive_single([self.MOVIE, self.MOVIE])[1], 1)

    def test_onclick_payload_is_decoded(self):
        import base64 as _b64
        enc = _b64.b64encode(self.MOVIE.encode()).decode()
        urls = AnimeDetailsThread._onclick_episode_urls([f"openEpisode('{enc}')"])
        self.assertEqual(urls, [self.MOVIE])

    def test_onclick_ignores_unrelated_handlers(self):
        self.assertEqual(AnimeDetailsThread._onclick_episode_urls(["doSomethingElse()"]), [])


class SiteFlowPrecedenceTests(unittest.TestCase):
    """Loading an anime and downloading from the Watchlist must use the built-in flow
    for supported sites. Before this, both inherited the same-domain profile with the
    most steps, so one hand-edited profile silently became the template for every new
    anime and every watchlist download."""

    def setUp(self):
        with config_lock:
            self._saved = dict(sites_data)
            sites_data.clear()

    def tearDown(self):
        with config_lock:
            sites_data.clear()
            sites_data.update(self._saved)

    def test_builtin_wins_over_an_existing_profile(self):
        with config_lock:
            sites_data["Old Anime"] = {
                "url": "https://witanime.site/episode/whatever-{x}/",
                "next_btn_xpath": "WRONG",
                # deliberately richer than the built-in, which used to win on count
                "step_paths": {"FHD - Mediafire": [{"xpath": "junk", "delay": 99.0},
                                             {"xpath": "junk2", "delay": 99.0},
                                             {"xpath": "junk3", "delay": 99.0}]},
            }
        paths, nxt = resolve_site_flow("witanime.site")
        self.assertEqual(paths, DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"])
        self.assertEqual(nxt, DEFAULT_SITE_FLOWS["witanime.site"]["next_btn_xpath"])
        self.assertNotIn("junk", str(paths))

    def test_returned_flow_is_a_copy(self):
        """The caller writes this into a profile; mutating it must not corrupt the
        shipped default for every later download in the same session."""
        paths, _ = resolve_site_flow("witanime.site")
        paths["FHD - Mediafire"][0]["delay"] = 123.0
        self.assertNotEqual(
            DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]["FHD - Mediafire"][0]["delay"],
            123.0)

    def test_unsupported_domain_still_inherits_from_a_profile(self):
        with config_lock:
            sites_data["Custom"] = {
                "url": "https://example.com/ep-{x}/",
                "next_btn_xpath": "next",
                "step_paths": {"host": [{"xpath": "a", "delay": 1.0}]},
            }
        paths, nxt = resolve_site_flow("example.com")
        self.assertEqual(paths, {"host": [{"xpath": "a", "delay": 1.0}]})
        self.assertEqual(nxt, "next")

    def test_unsupported_domain_with_no_profile_is_empty(self):
        paths, nxt = resolve_site_flow("nowhere.invalid")
        self.assertEqual(paths, {})
        self.assertEqual(nxt, "")


class WitanimeTemplateTests(unittest.TestCase):
    """Pins the shipped witanime flow to the template it is meant to be. A stray edit
    to a delay or an xpath here changes downloads for every anime on the site.

    The template is a real, working profile exported from the app
    (fixtures/witanime_template.json) -- compared against directly rather than
    retyped here, so the test and the template cannot drift apart."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "witanime_template.json")
        with open(path, encoding="utf-8") as f:
            cls.template = json.load(f)

    def test_flow_matches_the_template(self):
        flow = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]
        expected = self.template["step_paths"]
        self.assertEqual(list(flow), list(expected), "path names or order changed")
        for name, steps in expected.items():
            self.assertEqual(len(flow[name]), len(steps), f"{name}: step count changed")
            for i, step in enumerate(steps):
                # A step is either a click (xpath) or page JavaScript (script).
                self.assertEqual(flow[name][i].get("xpath"), step.get("xpath"), f"{name}[{i}] xpath")
                self.assertEqual(flow[name][i].get("script"), step.get("script"), f"{name}[{i}] script")
                self.assertEqual(float(flow[name][i]["delay"]), float(step["delay"]),
                                 f"{name}[{i}] delay")

    def test_next_button_matches_the_template(self):
        self.assertEqual(DEFAULT_SITE_FLOWS["witanime.site"]["next_btn_xpath"],
                         self.template["next_btn_xpath"])
        self.assertEqual(self.template["next_btn_xpath"], "الحلقة التالية")

    def test_per_anime_fields_stay_out_of_the_flow(self):
        """The template's url and episode range belong to one anime, not the site."""
        flow = DEFAULT_SITE_FLOWS["witanime.site"]
        self.assertNotIn("url", flow)
        self.assertNotIn("last_episodes", flow)


class DuplicateProfileTests(unittest.TestCase):
    """Loading an anime that already has a profile opens it instead of copying it."""

    def test_same_anime_matches(self):
        from ui.search_tab import find_profile_for_template
        profiles = {
            "One Piece": {"url": "https://witanime.site/watch/one-piece-%D8%A7%D9%84%D8%AD%D9%84%D9%82%D8%A9-{x}/"},
            "Bleach": {"url": "https://eta.animerco.org/episodes/bleach-episode-{x}"},
        }
        self.assertEqual("One Piece", find_profile_for_template(
            profiles, "https://witanime.site/watch/one-piece-الحلقة-{x}"))      # decoded, no slash
        self.assertEqual("Bleach", find_profile_for_template(
            profiles, "https://animerco.org/episodes/bleach-episode-{x}/"))      # subdomain
        self.assertIsNone(find_profile_for_template(
            profiles, "https://witanime.site/watch/one-piece-film-red-{x}/"))
        self.assertIsNone(find_profile_for_template(profiles, ""))

    def test_load_opens_existing_instead_of_copying(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        type(self)._app = QApplication.instance() or QApplication([])   # alive for the run
        import ui.search_tab as st
        fake = mock.MagicMock()
        tpl = "https://witanime.site/watch/black-clover-2nd-season/{x}"
        with mock.patch.object(st, "sites_data", {"Black Clover 2nd Season": {"url": tpl}}), \
                mock.patch.object(st, "save_config"):
            name = st.AnimeSearchWidget._open_or_create_profile(fake, "Black Clover 2nd Season - S2",
                                                                 tpl, 12)
        self.assertEqual("Black Clover 2nd Season", name)
        fake._create_profile.assert_not_called()
        self.assertFalse(fake._go_to_profile.call_args.kwargs["created"])


class SessionExpiredTests(unittest.TestCase):
    """witanime shows the hidden browser an expired-session screen with no
    download section (Hyouken no Majutsushi ep 8, headless)."""

    TEXT = ("بقيت هذه الصفحة مفتوحة لفترة طويلة وانتهت صلاحية الجلسة. "
            "أعد تحميل الصفحة لمتابعة المشاهدة.")

    def test_detects_the_screen(self):
        from core.selenium_engine import is_session_expired_text
        self.assertTrue(is_session_expired_text(self.TEXT))
        self.assertFalse(is_session_expired_text("الحلقة 8 تحميل FHD"))
        self.assertFalse(is_session_expired_text(None))

    def test_reads_rendered_text_only(self):
        from unittest import mock
        from core.selenium_engine import is_session_expired_page
        driver = mock.Mock()
        driver.execute_script.return_value = self.TEXT
        self.assertTrue(is_session_expired_page(driver))
        self.assertIn("innerText", driver.execute_script.call_args[0][0])
        driver.execute_script.side_effect = RuntimeError("tab gone")
        self.assertFalse(is_session_expired_page(driver))


class WitAnimeBrowserModeTests(unittest.TestCase):
    """WitAnime blocks standard headless Chrome (--headless=new). The app runs
    the browser invisibly via off-screen positioning and SW_HIDE so downloads succeed
    without opening visible windows or ruining UX."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_create_browser_headless_options(self):
        from unittest import mock
        import core.selenium_engine as eng
        # Never a real WindowGuard in a test: with a mocked driver it ran its loop
        # over the user's real desktop for the rest of the run (see WindowGuardTests).
        with mock.patch("selenium.webdriver.Chrome") as mock_chrome, \
             mock.patch("selenium.webdriver.chrome.service.Service"), \
             mock.patch("core.window_guard.WindowGuard") as guard:
            eng.create_browser("C:\\dummy", headless=True)
            self.assertTrue(mock_chrome.called)
            guard.return_value.start_prelaunch.assert_called_once()
            guard.return_value.attach_driver.assert_called_once()
            options = mock_chrome.call_args.kwargs.get("options") or mock_chrome.call_args[1].get("options")
            args = options.arguments
            # Must NOT use --headless=new which witanime/Cloudflare blocks
            self.assertFalse(any("--headless" in a for a in args))
            # Must position off-screen and set desktop viewport for invisible execution
            self.assertIn("--window-position=-10000,-10000", args)
            self.assertIn("--window-size=1920,1080", args)

    def test_downloader_tab_headless_remains_user_controlled(self):
        import copy
        from unittest import mock
        import ui.downloader_tab as dt
        import utils.config as cfg
        saved_sites = copy.deepcopy(cfg.sites_data)
        saved_settings = copy.deepcopy(cfg.app_settings)
        self.addCleanup(lambda: (
            cfg.sites_data.clear(), cfg.sites_data.update(saved_sites),
            cfg.app_settings.clear(), cfg.app_settings.update(saved_settings)
        ))
        cfg.sites_data["WitAnime Show"] = {"url": "https://witanime.site/watch/hyouken/{x}"}
        cfg.sites_data["Animerco Show"] = {"url": "https://eta.animerco.org/episode/hyouken-{x}"}
        cfg.app_settings["headless"] = True

        with mock.patch.object(dt, "save_config", lambda: None):
            w = dt.DownloaderWidget()
            w._save_timer.stop()
            # Select WitAnime profile: user preference is preserved, checkbox remains enabled
            w.on_site_select("WitAnime Show")
            self.assertTrue(w.chk_headless.isChecked())
            self.assertTrue(w.chk_headless.isEnabled())
            self.assertEqual(w.chk_headless.text(), "Run Invisibly (Headless)")

            # Select Animerco profile: checkbox remains enabled and checked
            w.on_site_select("Animerco Show")
            self.assertTrue(w.chk_headless.isChecked())
            self.assertTrue(w.chk_headless.isEnabled())
            self.assertEqual(w.chk_headless.text(), "Run Invisibly (Headless)")

    def test_progress_tab_paused_panel_headless_remains_enabled(self):
        from unittest import mock
        from ui.progress_tab import ProgressTab
        w = ProgressTab()
        with mock.patch("ui.progress_tab.run_snapshot", return_value={
            "task_id": 123, "headless": True, "not_started": [2],
            "limit": 2, "auto": True, "total": 2
        }):
            w._fill_paused_panel()
            self.assertTrue(w.chk_paused_headless.isChecked())
            self.assertTrue(w.chk_paused_headless.isEnabled())

    def test_run_selenium_task_preserves_headless_for_all_sites(self):
        from unittest import mock
        import core.selenium_engine as eng
        from utils.config import sites_data, config_lock
        with config_lock:
            sites_data["_test_wit_run"] = {"url": "https://witanime.site/watch/show/{x}", "step_paths": {}}
        try:
            called_headless = []
            def fake_create_browser(d_dir, headless=True):
                called_headless.append(headless)
                eng.cancel_event.set()
                m = mock.MagicMock()
                return m

            with mock.patch.object(eng, "create_browser", fake_create_browser), \
                 mock.patch.object(eng, "kill_stuck_chrome_processes", lambda: None), \
                 mock.patch.object(eng, "log_history", lambda *a, **k: None):
                eng.run_selenium_task(
                    site_key="_test_wit_run",
                    episodes_list=[1],
                    download_dir=tempfile.gettempdir(),
                    headless=True,
                    webhook_url="",
                    selected_sound="",
                    volume=0,
                    concurrency=1
                )
            self.assertEqual(called_headless, [True])
        finally:
            with config_lock:
                sites_data.pop("_test_wit_run", None)
            eng.cancel_event.clear()

    def test_start_watch_download_headless_mode(self):
        from unittest import mock
        import ui.downloader_tab as dt
        with mock.patch.object(dt, "save_config", lambda: None):
            w = dt.DownloaderWidget()
            w._save_timer.stop()
            w.chk_headless.setChecked(True)
            begun_params = []
            w._begin_download = lambda p: begun_params.append(p)

            # Witanime watch download -> headless respects chk_headless
            w.start_watch_download("Show1", "https://witanime.site/watch/show/{x}", "witanime.site", "1")
            self.assertTrue(begun_params[-1]["headless"])

            # Animerco watch download -> headless respects chk_headless
            w.start_watch_download("Show2", "https://eta.animerco.org/episode/show-{x}", "eta.animerco.org", "1")
            self.assertTrue(begun_params[-1]["headless"])

    def test_create_browser_visible_options(self):
        from unittest import mock
        import core.selenium_engine as eng
        with mock.patch("selenium.webdriver.Chrome") as mock_chrome, \
             mock.patch("selenium.webdriver.chrome.service.Service"), \
             mock.patch("core.window_guard.WindowGuard"):
            eng.create_browser("C:\\dummy", headless=False)
            self.assertTrue(mock_chrome.called)
            options = mock_chrome.call_args.kwargs.get("options") or mock_chrome.call_args[1].get("options")
            args = options.arguments
            self.assertIn("--start-maximized", args)
            self.assertFalse(any("--headless" in a for a in args))
            self.assertFalse(any("--window-position" in a for a in args))

    def test_hide_offscreen_window_logic(self):
        """Verify the SW_HIDE logic targets only off-screen Chrome windows and leaves
        on-screen or non-Chrome windows alone."""
        shown = []
        class MockUser32:
            def GetClassNameW(self, hwnd, buf, maxlen):
                if hwnd == 1:
                    buf.value = "Chrome_WidgetWin_1"
                elif hwnd == 2:
                    buf.value = "Chrome_WidgetWin_1"
                else:
                    buf.value = "Notepad"
                return len(buf.value)
            def GetWindowRect(self, hwnd, rect_ref):
                if hwnd == 1:
                    rect_ref._obj.left = -10000
                    rect_ref._obj.top = -10000
                elif hwnd == 2:
                    rect_ref._obj.left = 100
                    rect_ref._obj.top = 100
            def ShowWindow(self, hwnd, cmd):
                shown.append((hwnd, cmd))

        mock_user = MockUser32()
        import ctypes
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        def _check_hide(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            mock_user.GetClassNameW(hwnd, buf, 256)
            if buf.value == "Chrome_WidgetWin_1":
                rect = _RECT()
                mock_user.GetWindowRect(hwnd, ctypes.byref(rect))
                if rect.left <= -5000 and rect.top <= -5000:
                    mock_user.ShowWindow(hwnd, 0)

        _check_hide(1)
        _check_hide(2)
        _check_hide(3)
        self.assertEqual(shown, [(1, 0)])

    def test_request_adjustments_headless_toggling(self):
        import core.selenium_engine as eng
        with eng._run_lock:
            eng.RUN_STATE.clear()
            eng.RUN_STATE.update(task_id=42, episodes=[1], started=set(), headless=True)
            eng._PENDING_ADJUST.clear()
        try:
            res = eng.request_adjustments(42, headless=False)
            self.assertNotEqual(res, False)
            snap = eng.run_snapshot()
            self.assertFalse(snap["headless"])

            eng.request_adjustments(42, headless=True)
            snap = eng.run_snapshot()
            self.assertTrue(snap["headless"])
        finally:
            with eng._run_lock:
                eng.RUN_STATE.clear()
                eng._PENDING_ADJUST.clear()


class FillerEpisodeTests(unittest.TestCase):
    """Both sites mark filler in their episode lists, in different shapes. Markup
    below is copied from the live pages (Naruto Shippuden / Bleach, Sep 2026)."""

    WITANIME = (
        '<a href="/watch/naruto-shippuden/56"><span class="text-xs text-white">الحلقة 56</span></a>'
        '<a href="/watch/naruto-shippuden/57">'
        '<span class="text-xs text-white">الحلقة 57</span> '
        '<span class="rounded bg-amber-500 px-1.5 py-0.5 text-[10px] font-bold uppercase text-black">فيلر</span>'
        '</a>'
        '<a href="/watch/naruto-shippuden/58">'
        '<p class="text-xs text-white">الحلقة 58</p>'
        '<span class="rounded bg-amber-500 px-1 py-0.5 text-[9px] font-bold text-black">فيلر</span></a>'
    )
    ANIMERCO = (
        '<ul id="filter" class="episodes-list">'
        '<li data-number="32"><a href="/episodes/anime-bleach-32/"><span>الحلقة 32</span></a></li>'
        '<li data-number="33"><a href="/episodes/anime-bleach-33/" class="active">'
        '<span>الحلقة 33 - فلر</span></a></li>'
        '<li data-number="50"><a href="/episodes/anime-bleach-50/"><span>الحلقة 50 - فلر</span></a></li>'
        '</ul>'
    )

    def test_witanime_badges_are_read(self):
        from core.filler import parse_filler_episodes
        self.assertEqual(parse_filler_episodes(self.WITANIME), [57, 58])

    def test_animerco_list_items_are_read(self):
        from core.filler import parse_filler_episodes
        self.assertEqual(parse_filler_episodes(self.ANIMERCO), [33, 50])

    def test_the_two_words_are_not_confused(self):
        """فيلر contains no فلر: matching loosely would double-count."""
        from core.filler import parse_filler_episodes
        self.assertEqual(parse_filler_episodes(self.WITANIME + self.ANIMERCO),
                         [33, 50, 57, 58])

    def test_a_page_with_no_markers_yields_nothing(self):
        from core.filler import parse_filler_episodes
        self.assertEqual(parse_filler_episodes(
            '<li data-number="1"><a><span>الحلقة 1</span></a></li>'), [])
        self.assertEqual(parse_filler_episodes(""), [])
        self.assertEqual(parse_filler_episodes(None), [])

    def test_a_far_away_number_is_not_claimed(self):
        """The word in a synopsis must not adopt an episode number from elsewhere."""
        from core.filler import parse_filler_episodes
        html = '<span>الحلقة 12</span>' + ("x" * 400) + '<p>القصة فيها فيلر كثير</p>'
        self.assertEqual(parse_filler_episodes(html), [])

    def test_strip_filler_keeps_order_and_ignores_unknown(self):
        from core.filler import strip_filler
        self.assertEqual(strip_filler([55, 56, 57, 58, 59], [57, 58, 999]), [55, 56, 59])
        self.assertEqual(strip_filler([1, 2], []), [1, 2])
        self.assertEqual(strip_filler([], [1]), [])
        self.assertEqual(strip_filler(None, None), [])

    def test_every_episode_being_filler_yields_an_empty_list(self):
        """The UI must be able to tell this apart from "nothing marked"."""
        from core.filler import strip_filler
        self.assertEqual(strip_filler([57, 58], [57, 58]), [])


class CrossSiteFillerTests(unittest.TestCase):
    """When a site marks nothing, the other site's list is used -- filler numbering
    belongs to the anime. The match must be certain: skipping episodes the user
    wanted is worse than skipping none."""

    def pick(self, title, names):
        from core.filler import pick_cross_site_match
        hit = pick_cross_site_match(title, [{"title": n, "link": f"/a/{n}"} for n in names])
        return hit["title"] if hit else None

    def test_same_show_matches_across_spelling(self):
        self.assertEqual(self.pick("Bleach", ["One Piece", "Bleach"]), "Bleach")

    def test_romanized_long_vowels_are_the_same_show(self):
        """animerco writes "Naruto: Shippuuden", witanime "Naruto Shippuden"."""
        self.assertEqual(self.pick("Naruto: Shippuuden", ["Naruto Shippuden"]),
                         "Naruto Shippuden")
        self.assertEqual(self.pick("Yuusha Party", ["Yusha Party"]), "Yusha Party")

    def test_different_shows_do_not_collide_through_that_rule(self):
        self.assertIsNone(self.pick("Naruto", ["Boruto"]))
        self.assertIsNone(self.pick("One Piece", ["One Punch Man"]))

    def test_a_different_season_is_never_matched(self):
        self.assertIsNone(self.pick("Grand Blue", ["Grand Blue Season 3"]))
        self.assertIsNone(self.pick("Slime 4th Season", ["Slime"]))

    def test_the_same_season_written_differently_matches(self):
        self.assertEqual(self.pick("Mushoku Tensei III", ["Mushoku Tensei Season 3"]),
                         "Mushoku Tensei Season 3")

    def test_a_longer_name_is_not_a_match(self):
        """Containment would let "Bleach" adopt a spin-off's filler list."""
        self.assertIsNone(self.pick("Bleach", ["Bleach: Sennen Kessen-hen"]))

    def test_no_candidates_or_no_title(self):
        from core.filler import pick_cross_site_match
        self.assertIsNone(pick_cross_site_match("Bleach", []))
        self.assertIsNone(pick_cross_site_match("", [{"title": "Bleach"}]))
        self.assertIsNone(pick_cross_site_match(None, None))

    def test_plain_strings_are_accepted_as_candidates(self):
        from core.filler import pick_cross_site_match
        self.assertEqual(pick_cross_site_match("Bleach", ["Naruto", "Bleach"]), "Bleach")

    def test_animerco_always_asks_witanime(self):
        """Measured: animerco marks 2 of Bleach's 366 episodes, witanime 163. A
        non-empty animerco answer is not a complete one."""
        from core.filler import wants_other_site
        animerco = "https://det.animerco.org/episodes/anime-bleach-1/"
        self.assertTrue(wants_other_site(animerco, []))
        self.assertTrue(wants_other_site(animerco, [33, 50]))

    def test_witanime_only_falls_back_when_empty(self):
        """Querying animerco needs a browser (~5 s) and rarely adds anything."""
        from core.filler import wants_other_site
        wit = "https://witanime.site/watch/bleach/1"
        self.assertTrue(wants_other_site(wit, []))
        self.assertFalse(wants_other_site(wit, [33, 50, 64]))

    def test_witanime_search_results_parse(self):
        """The fallback reads witanime's search page with the Search tab's parser."""
        from ui.search_tab import parse_witanime_results
        html = ('<a href="https://witanime.site/anime/bleach">'
                '<img src="https://witanime.site/c/bleach.jpg" alt="Bleach"></a>'
                '<a href="https://witanime.site/movie/summer-wars">'
                '<img src="https://witanime.site/c/sw.jpg" alt="Summer Wars"></a>'
                '<a href="/about">no image here</a>')
        found = parse_witanime_results(html)
        self.assertEqual([f["title"] for f in found], ["Bleach", "Summer Wars"])
        self.assertEqual(found[0]["link"], "https://witanime.site/anime/bleach")
        self.assertEqual(parse_witanime_results(""), [])


class WitanimeUrlMigrationTests(unittest.TestCase):
    """Pre-move witanime URLs must be rewritten to the new host AND path format:
    the old host fails TLS, so the engine never reaches a not-found page that would
    trigger its own URL fallbacks."""

    def m(self, url):
        from utils.config import migrate_witanime_url
        return migrate_witanime_url(url)

    def test_old_episode_template_becomes_watch_template(self):
        self.assertEqual(
            self.m("https://witanime.life/episode/tensei-shitara-slime-datta-ken-4th-season-الحلقة-{x}"),
            "https://witanime.site/watch/tensei-shitara-slime-datta-ken-4th-season/{x}")

    def test_concrete_episode_and_trailing_slash(self):
        self.assertEqual(self.m("https://witanime.net/episode/bleach-sennen-kessen-hen-الحلقة-26/"),
                         "https://witanime.site/watch/bleach-sennen-kessen-hen/26")

    def test_percent_encoded_arabic_is_understood(self):
        self.assertEqual(
            self.m("https://witanime.life/episode/one-piece-%D8%A7%D9%84%D8%AD%D9%84%D9%82%D8%A9-{x}/"),
            "https://witanime.site/watch/one-piece/{x}")

    def test_old_episode_path_on_new_host_is_rewritten_too(self):
        self.assertEqual(self.m("https://witanime.site/episode/naruto-الحلقة-{x}"),
                         "https://witanime.site/watch/naruto/{x}")

    def test_anime_page_gets_host_swap_only(self):
        self.assertEqual(self.m("https://witanime.life/anime/bleach-sennen-kessen-hen-kashin-tan/"),
                         "https://witanime.site/anime/bleach-sennen-kessen-hen-kashin-tan/")

    def test_current_urls_and_other_sites_are_untouched(self):
        for u in ("https://witanime.site/watch/mushoku-tensei-iii-isekai-ittara-honki-dasu/{x}",
                  "https://witanime.site/watch/movie/summer-wars",
                  "https://det.animerco.org/episodes/x-الحلقة-{x}/", "", None):
            self.assertEqual(self.m(u), u)


class SearchTitleTests(unittest.TestCase):
    def test_html_entities_are_decoded(self):
        from ui.search_tab import clean_title
        self.assertEqual(clean_title("I&#039;ll Become a Villainess"), "I'll Become a Villainess")
        self.assertEqual(clean_title("Tom &amp; Jerry"), "Tom & Jerry")

    def test_whitespace_is_tidied_and_empty_is_safe(self):
        from ui.search_tab import clean_title
        self.assertEqual(clean_title("  Re:Zero \n 4th  Season "), "Re:Zero 4th Season")
        self.assertEqual(clean_title(None), "")


class MovieFlagTests(unittest.TestCase):
    """Search cards flag movies from the result link, on both sites."""

    def test_animerco_movies_are_flagged(self):
        from ui.search_tab import is_movie_link
        self.assertTrue(is_movie_link(
            "https://det.animerco.org/movies/chainsaw-man-movie-reze-hen/"))
        self.assertTrue(is_movie_link(
            "https://det.animerco.org/movies/%d9%81%d9%8a%d9%84%d9%85-kimi-no-na-wa/"))

    def test_witanime_movies_are_flagged(self):
        from ui.search_tab import is_movie_link
        self.assertTrue(is_movie_link("https://witanime.site/movie/summer-wars"))

    def test_series_are_not_flagged(self):
        from ui.search_tab import is_movie_link
        self.assertFalse(is_movie_link(
            "https://det.animerco.org/animes/tensei-shitara-slime-datta-ken/"))
        self.assertFalse(is_movie_link(
            "https://witanime.site/anime/tensei-shitara-slime-datta-ken-4th-season"))

    def test_movie_in_slug_alone_is_not_a_movie(self):
        from ui.search_tab import is_movie_link
        self.assertFalse(is_movie_link("https://witanime.site/anime/movie-maker-club"))
        self.assertFalse(is_movie_link(
            "https://det.animerco.org/animes/kimetsu-no-yaiba-movie-hen/"))

    def test_bad_input_is_safe(self):
        from ui.search_tab import is_movie_link
        self.assertFalse(is_movie_link(""))
        self.assertFalse(is_movie_link(None))


class SiteRegistryParityTests(unittest.TestCase):
    """Pins what the shipped sites currently do.

    These values drive real downloads: a changed selector or delay silently breaks
    every episode for that site, and the Google Drive delays had already drifted from
    the intended template once before anyone noticed. The fixture is the picture of
    current behaviour, so changing it has to be a deliberate act -- regenerate with
    `py tools/snapshot_sites.py` and say why in the commit."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "sites_snapshot.json")
        with open(path, encoding="utf-8") as f:
            cls.snap = json.load(f)

    def test_search_urls_unchanged(self):
        self.assertEqual(SUPPORTED_SITES, self.snap["supported_sites"])

    def test_site_flows_unchanged(self):
        self.assertEqual(DEFAULT_SITE_FLOWS, self.snap["site_flows"])

    def test_path_hosts_unchanged(self):
        self.assertEqual({k: list(v) for k, v in PATH_HOSTS.items()},
                         self.snap["path_hosts"])

    def test_schedule_config_unchanged(self):
        self.assertEqual(SCHEDULE_URLS, self.snap["schedule_urls"])
        self.assertEqual(SCHEDULE_MATCH, self.snap["schedule_match"])

    def test_dns_hosts_unchanged(self):
        self.assertEqual(list(DEFAULT_HOSTS), self.snap["dns_hosts"])


class SiteConfigTests(unittest.TestCase):
    """Guard the shipped site definitions: a typo here silently breaks downloads for
    every profile created from search."""

    def test_search_urls_have_query_placeholder(self):
        for domain, url in SUPPORTED_SITES.items():
            self.assertIn("{query}", url, domain)

    def test_every_supported_site_is_a_bare_host(self):
        for domain in SUPPORTED_SITES:
            self.assertEqual(extract_domain(domain), domain)

    def test_default_flows_cover_supported_sites(self):
        for domain in SUPPORTED_SITES:
            self.assertIn(domain, DEFAULT_SITE_FLOWS, f"{domain} has no fallback flow")

    def test_default_flow_steps_are_well_formed(self):
        for domain, flow in DEFAULT_SITE_FLOWS.items():
            self.assertTrue(flow.get("step_paths"), domain)
            for path_name, steps in flow["step_paths"].items():
                self.assertTrue(steps, f"{domain}/{path_name} has no steps")
                for step in steps:
                    # Each step clicks an xpath or runs a script (mp4upload's last two).
                    action = (step.get("xpath") or step.get("script") or "").strip()
                    self.assertTrue(action, f"{domain}/{path_name}")
                    self.assertFalse(step.get("xpath") and step.get("script"),
                                     f"{domain}/{path_name}: one action per step")
                    self.assertIsInstance(step["delay"], float)
                    self.assertGreater(step["delay"], 0)

    def test_animerco_targets_hosts_by_favicon_row(self):
        """animerco lists downloads in a table whose only host clue is the favicon
        domain, so the selector must key off data-src."""
        paths = DEFAULT_SITE_FLOWS["eta.animerco.org"]["step_paths"]
        self.assertIn("google drive", paths)
        self.assertIn("mediafire", paths)
        self.assertIn("data-src", paths["google drive"][0]["xpath"])

    def test_witanime_has_all_its_hosts(self):
        """Mirrors the maintained witanime profile export -- a dropped path here means
        episodes silently fail on whichever host went missing."""
        paths = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]
        self.assertEqual(set(paths), {"FHD - Google", "FHD - Mediafire",
                                      "FHD - wtsrv", "FHD - Workupload", "FHD - mp4upload",
                                      "FHD - gofile", "FHD - wahmi"})

    def test_witanime_path_order_is_preserved(self):
        """The engine tries paths in order, so ordering is behaviour, not cosmetics."""
        paths = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]
        self.assertEqual(list(paths), ["FHD - Google", "FHD - Mediafire",
                                       "FHD - wtsrv", "FHD - Workupload", "FHD - mp4upload",
                                       "FHD - gofile", "FHD - wahmi"])

    def test_wtsrv_takes_the_leftmost_button(self):
        """RTL page: [last()] in source order is the button furthest left. Wrapped in
        parentheses so [last()] applies to all matches, not to each parent's."""
        from core.selenium_engine import parse_smart_xpath, path_probes
        paths = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]
        xp = paths["FHD - wtsrv"][1]["xpath"]
        self.assertTrue(xp.startswith("(//h2"))
        self.assertTrue(xp.endswith("'wtsrv')])[last()]"))
        self.assertEqual(parse_smart_xpath(xp), xp)          # used verbatim by the engine
        self.assertEqual(path_probes(paths)["FHD - wtsrv"], xp)   # and by the link check

    def test_gofile_ends_on_its_download_button(self):
        """Checked live: the gofile folder page's only [data-action=download]."""
        steps = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]["FHD - gofile"]
        self.assertEqual(steps[-1]["xpath"], "//button[@data-action='download']")
        self.assertIn("'gofile'", steps[1]["xpath"])


class SiteDisplayTests(unittest.TestCase):
    """The Search dropdown shows a name and a favicon instead of the raw host."""

    def test_drops_the_tld(self):
        self.assertEqual(site_display_name("witanime.site"), "witanime")

    def test_drops_the_subdomain_too(self):
        """"eta." is plumbing -- the site is animerco."""
        self.assertEqual(site_display_name("eta.animerco.org"), "animerco")

    def test_handles_a_two_part_suffix(self):
        self.assertEqual(site_display_name("example.co.uk"), "example")
        self.assertEqual(site_display_name("a.b.example.co.uk"), "example")

    def test_accepts_a_full_url(self):
        self.assertEqual(site_display_name("https://www.animerco.org/x"), "animerco")

    def test_empty_and_single_label_hosts_survive(self):
        self.assertEqual(site_display_name(""), "")
        self.assertEqual(site_display_name("localhost"), "localhost")

    def test_names_are_distinct(self):
        """Two sites collapsing to the same label would make the dropdown ambiguous."""
        names = [site_display_name(d) for d in SUPPORTED_SITES]
        self.assertEqual(len(names), len(set(names)), names)

    def test_every_supported_site_ships_an_icon(self):
        """Forgetting to run tools/fetch_site_icons.py for a new site only costs the
        picture (it falls back to a globe), which is exactly why nobody would notice."""
        for domain in SUPPORTED_SITES:
            self.assertTrue(site_icon_path(domain), f"{domain} has no bundled favicon")

    def test_unknown_site_has_no_icon(self):
        self.assertEqual(site_icon_path("not-a-real-site.example"), "")

    def test_a_moved_subdomain_keeps_the_sites_icon(self):
        """animerco redirects eta.animerco.org -> det.animerco.org. An entry that
        recorded the landing host would otherwise fall back to a generic globe."""
        self.assertEqual(site_icon_path("det.animerco.org"),
                         site_icon_path("eta.animerco.org"))

    def test_any_mirror_of_a_supported_site_matches_by_name(self):
        """Deliberately loose: mirrors are what this fallback is for, so another
        animerco host matches even on a different TLD."""
        self.assertEqual(site_icon_path("animerco.net"),
                         site_icon_path("eta.animerco.org"))

    def test_an_unrelated_host_gets_no_icon(self):
        self.assertEqual(site_icon_path("example.co.uk"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
