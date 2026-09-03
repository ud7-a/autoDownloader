"""Unit tests for the app's pure logic (no Qt widgets, no network, no browser).

Run from the repo root with:

    py -m unittest discover -s tests -v

These cover the parsing/formatting rules that the download and search features are
built on -- the places where a silent regression would quietly break real downloads
(wrong episode ranges, unreachable episode URLs, mis-detected seasons).
"""

import json
import os
import sys
import tempfile
import unittest

# Isolate the app's data directory BEFORE importing anything from the app, so a test
# can never touch the real config/history. tests/__init__.py normally does this; the
# repeat here protects against running this module directly.
os.environ.setdefault("AED_APP_DIR",
                      os.path.join(tempfile.gettempdir(), "AutoEpisodesDownloader-tests"))
os.makedirs(os.environ["AED_APP_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.downloader_tab import (compact_episode_spec, spec_to_ranges,        # noqa: E402
                               encode_check_url)
from ui.watchlist_tab import entries_airing_today                            # noqa: E402
from ui.search_tab import (extract_domain, _full_res, AnimeDetailsThread,   # noqa: E402
                           SUPPORTED_SITES, DEFAULT_SITE_FLOWS, resolve_site_flow,
                           site_display_name, site_icon_path)
from utils.config import sites_data, config_lock            # noqa: E402
from core.selenium_engine import (_format_eta, _aria_convert_unit,          # noqa: E402
                                  parse_smart_xpath, episode_url_variants,
                                  is_block_page, _host_of, tab_matches_path,
                                  PATH_HOSTS, rotate_error_log)
from core.schedule import SCHEDULE_URLS, SCHEDULE_MATCH                     # noqa: E402
from core import site_health, nav_block                                     # noqa: E402
from utils.browser_flags import DEFAULT_HOSTS                               # noqa: E402


class IsolationGuardTests(unittest.TestCase):
    """Tests must never read or write the real user data directory. If this fails,
    stop and fix the isolation before running anything else -- a test that writes to
    the live config can wipe the user's saved profiles."""

    def test_app_dir_is_redirected(self):
        from utils import config
        self.assertTrue(config.IS_ISOLATED,
                        f"tests are pointed at the REAL data dir: {config.APP_DIR}")

    def test_paths_live_under_the_temp_dir(self):
        from utils import config
        self.assertNotEqual(config.APP_DIR, config.DEFAULT_APP_DIR)
        for path in (config.CONFIG_FILE, config.DB_FILE, config.PROFILE_DIR):
            self.assertTrue(path.startswith(config.APP_DIR), path)
            self.assertNotIn(config.DEFAULT_APP_DIR, path)


class EpisodeSpecTests(unittest.TestCase):
    """The Episodes picker serializes to/from a compact spec string, which is also
    what History stores and what Re-download replays."""

    def spec_to_episodes(self, text):
        return sorted({e for a, b in spec_to_ranges(text) for e in range(a, b + 1)})

    def test_single_range(self):
        self.assertEqual(spec_to_ranges("1-12"), [(1, 12)])

    def test_single_episode(self):
        self.assertEqual(spec_to_ranges("5"), [(5, 5)])

    def test_gapped_ranges(self):
        self.assertEqual(spec_to_ranges("1-5, 8-12"), [(1, 5), (8, 12)])

    def test_whitespace_and_trailing_single(self):
        self.assertEqual(spec_to_ranges("12 - 20 , 22"), [(12, 20), (22, 22)])

    def test_unicode_dashes_are_normalized(self):
        self.assertEqual(spec_to_ranges("1 – 4"), [(1, 4)])   # en dash
        self.assertEqual(spec_to_ranges("1 — 4"), [(1, 4)])   # em dash

    def test_episode_zero_is_valid(self):
        self.assertEqual(self.spec_to_episodes("0-3"), [0, 1, 2, 3])

    def test_garbage_tokens_are_skipped_not_crashed(self):
        self.assertEqual(spec_to_ranges("abc"), [])
        self.assertEqual(spec_to_ranges(""), [])
        self.assertEqual(spec_to_ranges(None), [])

    def test_compact_merges_adjacent_and_dedups(self):
        self.assertEqual(compact_episode_spec([1, 2, 3, 4, 5]), "1-5")
        self.assertEqual(compact_episode_spec([3, 1, 2]), "1-3")
        self.assertEqual(compact_episode_spec([1, 1, 2]), "1-2")

    def test_compact_keeps_gaps(self):
        self.assertEqual(compact_episode_spec([1, 2, 3, 8, 9, 20]), "1-3, 8-9, 20")

    def test_round_trip_preserves_selection(self):
        """History stores the compact spec; Re-download must reproduce it exactly."""
        for spec in ("1-5, 8-12", "5", "1-12", "0-3, 7", "1-3, 8-9, 20"):
            eps = self.spec_to_episodes(spec)
            self.assertEqual(self.spec_to_episodes(compact_episode_spec(eps)), eps, spec)

    def test_overlapping_ranges_collapse(self):
        self.assertEqual(compact_episode_spec(self.spec_to_episodes("1-5, 4-9")), "1-9")


class EtaFormatTests(unittest.TestCase):
    """aria2c reports ETA as XhYmZs; the card shows MM:SS with minutes never rolled
    up into an hours field."""

    def test_seconds_only(self):
        self.assertEqual(_format_eta("45s"), "00:45")

    def test_minutes_and_seconds(self):
        self.assertEqual(_format_eta("1m5s"), "01:05")
        self.assertEqual(_format_eta("9m59s"), "09:59")

    def test_hours_roll_into_minutes(self):
        self.assertEqual(_format_eta("1h5m30s"), "65:30")
        self.assertEqual(_format_eta("2h0m0s"), "120:00")

    def test_partial_forms(self):
        self.assertEqual(_format_eta("3m"), "03:00")
        self.assertEqual(_format_eta("1h"), "60:00")
        self.assertEqual(_format_eta("0s"), "00:00")

    def test_unparseable_passes_through(self):
        self.assertEqual(_format_eta("weird"), "weird")
        self.assertEqual(_format_eta(""), "")


class SizeUnitTests(unittest.TestCase):
    def test_mib_to_mb(self):
        self.assertEqual(_aria_convert_unit("12.4MiB"), "13.00 MB")

    def test_gib_to_gb(self):
        self.assertEqual(_aria_convert_unit("1.2GiB"), "1.29 GB")

    def test_kib_to_kb(self):
        self.assertEqual(_aria_convert_unit("500.0KiB"), "512.0 KB")

    def test_unknown_unit_passes_through(self):
        self.assertEqual(_aria_convert_unit("12B"), "12B")


class SmartXPathTests(unittest.TestCase):
    """Profile steps accept plain text ("mediafire"), an index ("mediafire #last")
    or a raw XPath, which must pass through untouched."""

    def test_raw_xpath_passes_through(self):
        raw = '//*[@id="downloadButton"]'
        self.assertEqual(parse_smart_xpath(raw), raw)

    def test_grouped_xpath_passes_through(self):
        raw = "(//a)[2]"
        self.assertEqual(parse_smart_xpath(raw), raw)

    def test_plain_text_becomes_case_insensitive_contains(self):
        out = parse_smart_xpath("MediaFire")
        self.assertIn("mediafire", out)
        self.assertTrue(out.startswith("//*[contains(translate("))

    def test_last_index(self):
        self.assertTrue(parse_smart_xpath("mediafire #last").endswith(")[last()]"))

    def test_numeric_index(self):
        self.assertTrue(parse_smart_xpath("mediafire #2").endswith(")[2]"))

    def test_empty_is_empty(self):
        self.assertEqual(parse_smart_xpath("   "), "")


class EpisodeUrlVariantTests(unittest.TestCase):
    """Some series split across two slug patterns, and finales carry a suffix; the
    engine retries these variants when the primary URL 404s."""

    ANIMERCO = "https://eta.animerco.org/episodes/bleach-الحلقة-5/"
    ANIMERCO_PREFIXED = "https://eta.animerco.org/episodes/انمي-bleach-الحلقة-5/"

    def test_adds_arabic_anime_prefix(self):
        self.assertIn(self.ANIMERCO_PREFIXED, episode_url_variants(self.ANIMERCO))

    def test_removes_arabic_anime_prefix(self):
        self.assertIn(self.ANIMERCO, episode_url_variants(self.ANIMERCO_PREFIXED))

    def test_prefix_toggle_comes_first(self):
        """It fixes a whole episode range, so it must be tried before finale suffixes."""
        self.assertEqual(episode_url_variants(self.ANIMERCO)[0], self.ANIMERCO_PREFIXED)

    def test_includes_finale_suffix_variants(self):
        self.assertTrue(any(v.endswith("-والاخيرة/") for v in episode_url_variants(self.ANIMERCO)))

    def test_variants_are_unique_and_exclude_the_original(self):
        variants = episode_url_variants(self.ANIMERCO)
        self.assertEqual(len(variants), len(set(variants)))
        self.assertNotIn(self.ANIMERCO, variants)


class DomainAndCoverTests(unittest.TestCase):
    def test_extract_domain_strips_scheme_and_www(self):
        self.assertEqual(extract_domain("https://WWW.Witanime.life/x"), "witanime.life")

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
        self.assertFalse(b.matches("https://witanime.life/"))
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
    TABLE = {"witanime.life": "wit", "eta.animerco.org": "ani"}

    def test_exact_host_wins(self):
        self.assertEqual(site_health.lookup(self.TABLE, "witanime.life"), "wit")

    def test_a_moved_host_still_finds_its_entry(self):
        """The whole point: det.* is not a key, but it is the same site."""
        self.assertEqual(site_health.lookup(self.TABLE, "det.animerco.org"), "ani")
        self.assertEqual(
            site_health.lookup(self.TABLE, "https://det.animerco.org/animes/bleach/"), "ani")

    def test_unknown_site_gets_the_default(self):
        self.assertIsNone(site_health.lookup(self.TABLE, "example.com"))
        self.assertEqual(site_health.lookup(self.TABLE, "example.com", "fallback"), "fallback")

    def test_empty_table_is_safe(self):
        self.assertIsNone(site_health.lookup({}, "witanime.life"))


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
        self.assertEqual(site_health.record_landing("https://witanime.life/episode/x/",
                                                    "https://www.mediafire.com/file/y"), "")
        self.assertEqual(site_health.learned_hosts(), ())

    def test_landing_where_we_asked_is_not_a_move(self):
        self.assertEqual(site_health.record_landing("https://witanime.life/a/",
                                                    "https://witanime.life/a/"), "")
        self.assertEqual(site_health.learned_hosts(), ())

    def test_a_content_rich_page_yielding_nothing_is_a_break(self):
        for _ in range(2):
            site_health.record_detection("https://witanime.life/anime/x/", 2428, 0)
        self.assertIn("witanime", site_health.broken_sites())

    def test_one_empty_page_is_not_yet_a_break(self):
        """A single anime with no episodes is far more likely than a site rewrite."""
        site_health.record_detection("https://witanime.life/anime/x/", 2428, 0)
        self.assertEqual(site_health.broken_sites(), ())

    def test_a_nearly_empty_page_is_not_evidence(self):
        """No anchors means a block page or a failed load, not a layout change."""
        for _ in range(5):
            site_health.record_detection("https://witanime.life/anime/x/", 3, 0)
        self.assertEqual(site_health.broken_sites(), ())

    def test_a_successful_detection_clears_the_break(self):
        for _ in range(3):
            site_health.record_detection("https://witanime.life/anime/x/", 2428, 0)
        self.assertIn("witanime", site_health.broken_sites())
        site_health.record_detection("https://witanime.life/anime/y/", 2428, 4)
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

    MOVIE = "https://witanime.life/episode/فيلم-bleach-sennen-kessen-hen-kashin-tan-movie/"

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
        encoded = ("https://witanime.life/episode/"
                   "%d9%81%d9%8a%d9%84%d9%85-bleach-sennen-kessen-hen-kashin-tan-movie/")
        template, _ = self.det._derive_single([encoded])
        self.assertEqual(template, self.MOVIE)

    def test_lone_numbered_episode_stays_parameterised(self):
        """A series with only ep 1 uploaded must still template, so later uploads work."""
        template, max_ep = self.det._derive_single(["https://witanime.life/episode/show-الحلقة-1/"])
        self.assertEqual(template, "https://witanime.life/episode/show-الحلقة-{x}/")
        self.assertEqual(max_ep, 1)

    def test_two_entries_are_left_to_the_grouping_logic(self):
        self.assertEqual(self.det._derive_single([self.MOVIE, "https://x/episode/other-1/"]), ("", 0))

    def test_non_episode_links_are_ignored(self):
        self.assertEqual(self.det._derive_single(["https://witanime.life/anime-genre/x/"]), ("", 0))

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


class ConcurrencyControllerTests(unittest.TestCase):
    """The auto-concurrency controller aims to keep each episode landing inside a
    target time band, and to retreat fast when a host pushes back."""

    def make(self, start=3, enabled=True):
        from core.concurrency import ConcurrencyController
        self.now = 1000.0
        return ConcurrencyController(start=start, enabled=enabled, clock=lambda: self.now)

    def feed(self, ctl, seconds_per_episode, count=3):
        """Report `count` downloads all projected to take `seconds_per_episode`."""
        for ep in range(count):
            # size / speed == projected seconds
            ctl.record_progress(ep, seconds_per_episode * 1_000_000, 1_000_000)

    def advance(self, ctl, windows=1, seconds=None):
        self.now += seconds if seconds is not None else ctl.WINDOW + 1

    def test_starts_at_the_given_value(self):
        self.assertEqual(self.make(start=3).limit, 3)

    def test_disabled_controller_never_moves(self):
        ctl = self.make(start=2, enabled=False)
        self.feed(ctl, 5)
        self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 2)

    def test_fast_episodes_add_a_download(self):
        """Finishing well inside the band means the connection has headroom."""
        ctl = self.make(start=2)
        self.feed(ctl, 20)          # 20s per episode -- far below target
        self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 3)

    def test_slow_episodes_remove_a_download(self):
        ctl = self.make(start=4)
        self.feed(ctl, 200)         # way over the band
        self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 3)

    def test_on_target_holds_steady(self):
        ctl = self.make(start=3)
        for _ in range(5):
            self.feed(ctl, 75)      # inside 60-90s
            self.advance(ctl)
            ctl.evaluate()
        self.assertEqual(ctl.limit, 3)

    def test_does_not_act_before_a_window_elapses(self):
        ctl = self.make(start=2)
        self.feed(ctl, 10)
        self.advance(ctl, seconds=1)
        self.assertEqual(ctl.evaluate(), 2)

    def test_settles_between_changes(self):
        """A change must be given time to take effect before judging it again."""
        ctl = self.make(start=2)
        self.feed(ctl, 20); self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 3)      # raised
        for _ in range(ctl.SETTLE_WINDOWS):
            self.feed(ctl, 20); self.advance(ctl)
            self.assertEqual(ctl.evaluate(), 3)  # holds while settling
        self.feed(ctl, 20); self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 4)      # free to raise again

    def test_never_exceeds_the_ceiling(self):
        ctl = self.make(start=6)
        for _ in range(20):
            self.feed(ctl, 5); self.advance(ctl); ctl.evaluate()
        self.assertEqual(ctl.limit, ctl.MAX_LIMIT)

    def test_slow_connection_floors_at_one(self):
        """If even a single download blows past the band, one is already the best
        we can do -- the connection is the limit, not the setting."""
        ctl = self.make(start=3)
        for _ in range(20):
            self.feed(ctl, 600, count=1); self.advance(ctl); ctl.evaluate()
        self.assertEqual(ctl.limit, ctl.MIN_LIMIT)
        self.assertIn("connection", ctl.last_reason)

    def test_failure_halves_immediately(self):
        ctl = self.make(start=6)
        ctl.record_failure("block page")
        self.assertEqual(ctl.limit, 3)

    def test_failure_does_not_go_below_one(self):
        ctl = self.make(start=1)
        ctl.record_failure("block page")
        self.assertEqual(ctl.limit, 1)

    def test_no_raising_during_failure_cooldown(self):
        """Rate-limit bans are expensive, so probe back slowly, not immediately."""
        ctl = self.make(start=4)
        ctl.record_failure("429")
        self.assertEqual(ctl.limit, 2)
        for _ in range(3):           # past settle, still inside the cooldown
            self.feed(ctl, 10); self.advance(ctl); ctl.evaluate()
        self.assertEqual(ctl.limit, 2)

    def test_raises_again_after_cooldown_expires(self):
        ctl = self.make(start=4)
        ctl.record_failure("429")
        self.advance(ctl, seconds=ctl.FAILURE_COOLDOWN + 1)
        for _ in range(ctl.SETTLE_WINDOWS + 1):
            self.feed(ctl, 10); self.advance(ctl); ctl.evaluate()
        self.assertGreater(ctl.limit, 2)

    def test_stalled_downloads_are_ignored(self):
        """A speed of zero says nothing about capacity."""
        ctl = self.make(start=3)
        ctl.record_progress(1, 500_000_000, 0)
        self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 3)

    def test_uses_the_median_not_one_outlier(self):
        ctl = self.make(start=3)
        for ep, secs in enumerate([70, 75, 4000]):   # one stuck download
            ctl.record_progress(ep, secs * 1_000_000, 1_000_000)
        self.advance(ctl)
        self.assertEqual(ctl.evaluate(), 3)          # median 75s -> on target

    def test_describe_mentions_mode(self):
        auto = self.make(); manual = self.make(enabled=False)
        self.assertIn("auto", auto.describe())
        self.assertIn("manual", manual.describe())


class ScheduleMatchingTests(unittest.TestCase):
    """Matching a watchlist entry to its release day. witanime can be matched by URL;
    animerco only publishes season links, so those fall back to titles."""

    def setUp(self):
        from core import schedule
        self.s = schedule
        self.items = [
            {"day": "saturday", "title": "Bleach: Sennen Kessen-hen - Kashin-tan",
             "url": "https://witanime.life/anime/bleach-sennen-kessen-hen-kashin-tan/"},
            {"day": "sunday", "title": "Mushoku Tensei III: Isekai Ittara Honki Dasu",
             "url": "https://eta.animerco.org/seasons/mushoku-tensei-iii-season-1/"},
            {"day": "friday", "title": "Tensei shitara Slime Datta Ken Season 4",
             "url": "https://eta.animerco.org/seasons/tensei-shitara-slime-datta-ken-season-4/"},
        ]

    def test_canonical_day_from_arabic(self):
        self.assertEqual(self.s.canonical_day("السبت"), "saturday")
        self.assertEqual(self.s.canonical_day("الاحد"), "sunday")   # both spellings
        self.assertEqual(self.s.canonical_day("الأحد"), "sunday")

    def test_canonical_day_from_panel_id(self):
        self.assertEqual(self.s.canonical_day("wednesday"), "wednesday")
        self.assertEqual(self.s.canonical_day("Friday"), "friday")

    def test_canonical_day_rejects_junk(self):
        self.assertIsNone(self.s.canonical_day("someday"))
        self.assertIsNone(self.s.canonical_day(""))

    def test_url_match_wins(self):
        entry = {"title": "totally different name",
                 "url": "https://witanime.life/anime/bleach-sennen-kessen-hen-kashin-tan/"}
        self.assertEqual(self.s.find_day(entry, self.items), "saturday")

    def test_url_match_ignores_trailing_slash(self):
        entry = {"title": "x",
                 "url": "https://witanime.life/anime/bleach-sennen-kessen-hen-kashin-tan"}
        self.assertEqual(self.s.find_day(entry, self.items), "saturday")

    def test_title_match_when_url_differs(self):
        """The animerco case: watchlist holds /animes/, schedule holds /seasons/."""
        entry = {"title": "Mushoku Tensei III: Isekai Ittara Honki Dasu",
                 "url": "https://eta.animerco.org/animes/mushoku-tensei-iii/"}
        self.assertEqual(self.s.find_day(entry, self.items), "sunday")

    def test_season_markers_are_ignored(self):
        """'4th Season' in the watchlist vs 'Season 4' on the schedule."""
        entry = {"title": "Tensei shitara Slime Datta Ken 4th Season",
                 "url": "https://eta.animerco.org/animes/tensei-shitara-slime-datta-ken/"}
        self.assertEqual(self.s.find_day(entry, self.items), "friday")

    def test_unknown_anime_returns_none(self):
        entry = {"title": "Something Not Airing", "url": "https://x/animes/nope/"}
        self.assertIsNone(self.s.find_day(entry, self.items))

    def test_short_titles_do_not_latch_onto_longer_ones(self):
        """A 3-letter name must not match every show containing those letters."""
        entry = {"title": "Ble", "url": ""}
        self.assertIsNone(self.s.find_day(entry, self.items))

    def test_normalize_strips_case_punctuation_and_season(self):
        n = self.s.normalize_title
        self.assertEqual(n("Bleach: Sennen Kessen-hen!"), n("bleach sennen kessen hen"))
        self.assertEqual(n("Grand Blue Season 3"), n("Grand Blue"))

    def test_witanime_matches_only_the_airing_season_page(self):
        """Each season is its own /anime/ page there, and by title they are
        indistinguishable -- so a title guess would flag every season as airing."""
        w = "https://witanime.life/anime/"
        items = [{"day": "monday", "title": "Grand Blue Season 3",
                  "url": w + "grand-blue-season-3/"}]
        airing = {"title": "Grand Blue Season 3", "url": w + "grand-blue-season-3/"}
        self.assertEqual(self.s.find_day(airing, items), "monday")
        for title, slug in [("Grand Blue", "grand-blue/"),
                            ("Grand Blue 2nd Season", "grand-blue-2nd-season/")]:
            self.assertIsNone(self.s.find_day({"title": title, "url": w + slug}, items),
                              f"{title} should not be treated as airing")

    def test_season_number_extraction(self):
        n = self.s.season_number
        self.assertEqual(n("Grand Blue Season 3"), 3)
        self.assertEqual(n("Hell Mode 2nd Season"), 2)
        self.assertEqual(n("انمي X الموسم 2"), 2)
        self.assertIsNone(n("Grand Blue"))

    def test_only_the_airing_season_of_an_animerco_show(self):
        items = [{"day": "sunday", "title": "Hell Mode Season 2",
                  "url": "https://eta.animerco.org/seasons/hell-mode-season-2/"}]
        self.assertTrue(self.s.is_season_scheduled("Hell Mode", "Season 2", items))
        for label in ("Season 1", "Season 3"):
            self.assertFalse(self.s.is_season_scheduled("Hell Mode", label, items), label)

    def test_animerco_anime_level_still_matches_by_title(self):
        """Its schedule never links the /animes/ page, so titles are all there is."""
        items = [{"day": "sunday", "title": "Hell Mode Season 2",
                  "url": "https://eta.animerco.org/seasons/hell-mode-season-2/"}]
        entry = {"title": "Hell Mode", "url": "https://eta.animerco.org/animes/hell-mode/"}
        self.assertEqual(self.s.find_day(entry, items), "sunday")

    def test_day_order_starts_on_saturday(self):
        """Both sites lay their week out starting Saturday."""
        self.assertEqual(self.s.DAY_ORDER[0], "saturday")
        self.assertEqual(len(self.s.DAY_ORDER), 7)
        self.assertEqual(set(self.s.DAY_ORDER), set(self.s.DAY_LABELS))

    def test_every_supported_site_has_a_schedule_url(self):
        from ui.search_tab import SUPPORTED_SITES
        for domain in SUPPORTED_SITES:
            self.assertIn(domain, self.s.SCHEDULE_URLS, domain)


class BlockPageTests(unittest.TestCase):
    """A tiny 'file' is really the host's rate-limit/forbidden HTML page, not a video."""

    def _file_of_size(self, size):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "wb") as f:
            f.write(b"x" * size)
        self.addCleanup(lambda: os.remove(path))
        return path

    def test_small_file_is_a_block_page(self):
        self.assertTrue(is_block_page(self._file_of_size(4096)))

    def test_large_file_is_a_real_download(self):
        self.assertFalse(is_block_page(self._file_of_size(1_500_000)))

    def test_missing_file_is_not_a_block_page(self):
        self.assertFalse(is_block_page(os.path.join(tempfile.gettempdir(), "does-not-exist.bin")))


class CheckUrlEncodingTests(unittest.TestCase):
    """The Base URL is requested to test reachability. Raw Arabic in it used to raise
    UnicodeEncodeError before any request went out, so both HTTP tiers were skipped
    and every such profile silently fell through to the DNS-only check."""

    def test_arabic_path_is_encoded(self):
        out = encode_check_url("https://witanime.life/episode/one-piece-الحلقة-444/")
        out.encode("ascii")   # must not raise -- this is what urllib does internally
        self.assertTrue(out.startswith("https://witanime.life/episode/one-piece-"))
        self.assertIn("%D8%A7", out)

    def test_already_encoded_url_is_unchanged(self):
        url = "https://witanime.life/episode/one-piece-%D8%A7%D9%84%D8%AD%D9%84%D9%82%D8%A9-444/"
        self.assertEqual(encode_check_url(url), url)

    def test_encoding_is_idempotent(self):
        raw = "https://witanime.life/episode/one-piece-الحلقة-444/"
        once = encode_check_url(raw)
        self.assertEqual(encode_check_url(once), once)

    def test_plain_ascii_url_is_unchanged(self):
        url = "https://example.com/ep/1/"
        self.assertEqual(encode_check_url(url), url)

    def test_host_and_scheme_are_preserved(self):
        out = encode_check_url("https://eta.animerco.org/episodes/بليتش-الحلقة-5/")
        self.assertTrue(out.startswith("https://eta.animerco.org/"))

    def test_query_is_kept(self):
        out = encode_check_url("https://example.com/dl?id=abc&export=download")
        self.assertEqual(out, "https://example.com/dl?id=abc&export=download")

    def test_blank_url_does_not_crash(self):
        self.assertEqual(encode_check_url(""), "")
        self.assertEqual(encode_check_url(None), "")


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
                "url": "https://witanime.life/episode/whatever-{x}/",
                "next_btn_xpath": "WRONG",
                # deliberately richer than the built-in, which used to win on count
                "step_paths": {"mediafire": [{"xpath": "junk", "delay": 99.0},
                                             {"xpath": "junk2", "delay": 99.0},
                                             {"xpath": "junk3", "delay": 99.0}]},
            }
        paths, nxt = resolve_site_flow("witanime.life")
        self.assertEqual(paths, DEFAULT_SITE_FLOWS["witanime.life"]["step_paths"])
        self.assertEqual(nxt, DEFAULT_SITE_FLOWS["witanime.life"]["next_btn_xpath"])
        self.assertNotIn("junk", str(paths))

    def test_returned_flow_is_a_copy(self):
        """The caller writes this into a profile; mutating it must not corrupt the
        shipped default for every later download in the same session."""
        paths, _ = resolve_site_flow("witanime.life")
        paths["mediafire"][0]["delay"] = 123.0
        self.assertNotEqual(
            DEFAULT_SITE_FLOWS["witanime.life"]["step_paths"]["mediafire"][0]["delay"],
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
    to a delay or an xpath here changes downloads for every anime on the site."""

    EXPECTED = {
        "mediafire": [("mediafire #last", 7.0), ('//*[@id="downloadButton"]', 3.0)],
        "google drive": [("google drive #last", 3.0), ("Download anyway", 2.0)],
        "Workupload": [("workupload #last", 5.0),
                       ('//*[@id=\\"file\\"]/div[3]/div/a', 5.0)],
        "rf": [("rf #last", 11.0), ('//*[@id="downloadButton"]', 2.0)],
    }

    def test_flow_matches_the_template(self):
        flow = DEFAULT_SITE_FLOWS["witanime.life"]["step_paths"]
        self.assertEqual(list(flow), list(self.EXPECTED), "path names or order changed")
        for name, steps in self.EXPECTED.items():
            self.assertEqual(len(flow[name]), len(steps), f"{name}: step count changed")
            for i, (xpath, delay) in enumerate(steps):
                self.assertEqual(flow[name][i]["xpath"], xpath, f"{name}[{i}] xpath")
                self.assertEqual(float(flow[name][i]["delay"]), delay, f"{name}[{i}] delay")

    def test_next_button_is_the_arabic_next_episode_label(self):
        self.assertEqual(DEFAULT_SITE_FLOWS["witanime.life"]["next_btn_xpath"],
                         "الحلقة التالية")


class ErrorLogRotationTests(unittest.TestCase):
    """Every failed download appends its full aria2c output, so on a flaky connection
    the log grows steadily. Rotation keeps at most two files, newest failures kept."""

    def log_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return os.path.join(d, "aria2c_error.log")

    def write(self, path, size):
        with open(path, "wb") as f:
            f.write(b"x" * size)

    def test_small_log_is_left_alone(self):
        p = self.log_path()
        self.write(p, 100)
        self.assertFalse(rotate_error_log(p, max_bytes=1000))
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".1"))

    def test_oversized_log_becomes_the_previous_one(self):
        p = self.log_path()
        self.write(p, 2000)
        self.assertTrue(rotate_error_log(p, max_bytes=1000))
        self.assertFalse(os.path.exists(p))          # a fresh one is opened on next write
        self.assertEqual(os.path.getsize(p + ".1"), 2000)

    def test_only_two_files_ever_exist(self):
        p = self.log_path()
        for marker in (b"a", b"b", b"c"):
            with open(p, "wb") as f:
                f.write(marker * 2000)
            rotate_error_log(p, max_bytes=1000)
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".2"))
        with open(p + ".1", "rb") as f:
            self.assertTrue(f.read().startswith(b"c"), "kept the oldest, not the newest")

    def test_missing_log_is_not_an_error(self):
        self.assertFalse(rotate_error_log(self.log_path(), max_bytes=1000))

    def test_exact_ceiling_rotates(self):
        """The check is `size < max`, so landing exactly on the ceiling rotates."""
        p = self.log_path()
        self.write(p, 1000)
        self.assertTrue(rotate_error_log(p, max_bytes=1000))


class WatchlistTodayFilterTests(unittest.TestCase):
    """The automatic check on launch covers today's anime only -- a full sweep is slow
    and mostly re-reads anime that cannot have a new episode yet. "Check all now" is
    the manual full pass."""

    def entry(self, name, day=None):
        e = {"url": f"https://x/{name}", "title": name}
        if day is not None:
            e["release_day"] = day
        return e

    def test_anime_airing_today_is_included(self):
        picked = entries_airing_today([self.entry("a", "monday")], "monday")
        self.assertEqual([e["title"] for e in picked], ["a"])

    def test_anime_airing_another_day_is_skipped(self):
        picked = entries_airing_today([self.entry("a", "friday")], "monday")
        self.assertEqual(picked, [])

    def test_unknown_day_is_included(self):
        """A newly followed anime has no day until the schedule scrape lands, and on a
        first run nothing has one -- excluding these would check nothing at all."""
        entries = [self.entry("no-key"), self.entry("blank", ""), self.entry("none", None)]
        self.assertEqual(len(entries_airing_today(entries, "monday")), 3)

    def test_mixed_watchlist_picks_only_today_and_unknown(self):
        entries = [self.entry("today", "sunday"), self.entry("other", "tuesday"),
                   self.entry("unknown"), self.entry("also-today", "sunday")]
        picked = [e["title"] for e in entries_airing_today(entries, "sunday")]
        self.assertEqual(picked, ["today", "unknown", "also-today"])

    def test_empty_and_none_watchlists_are_safe(self):
        self.assertEqual(entries_airing_today([], "monday"), [])
        self.assertEqual(entries_airing_today(None, "monday"), [])

    def test_day_comparison_is_exact(self):
        """Day keys come from the schedule's canonical set; no fuzzy matching."""
        self.assertEqual(entries_airing_today([self.entry("a", "Monday")], "monday"), [])

    def test_today_key_is_a_real_schedule_day(self):
        from ui.watchlist_tab import _today_key
        from core.schedule import DAY_ORDER
        self.assertIn(_today_key(), DAY_ORDER)


class CloudRecoveryTests(unittest.TestCase):
    """The service can lose its database. Recovery is a re-registration proving
    possession of the webhook -- never the server trusting whatever id it is handed."""

    def setUp(self):
        from utils import config
        self.config = config
        with config.config_lock:
            self.saved = dict(config.app_settings)

    def tearDown(self):
        with self.config.config_lock:
            self.config.app_settings.clear()
            self.config.app_settings.update(self.saved)

    def test_recovery_needs_a_stored_webhook(self):
        with self.config.config_lock:
            self.config.app_settings["discord_webhook"] = ""
            self.config.app_settings["cloud_service_url"] = "https://example.invalid"
        self.assertFalse(self.config.cloud_recover_identity())

    def test_recovery_needs_a_service_url(self):
        with self.config.config_lock:
            self.config.app_settings["discord_webhook"] = "https://discord.com/api/webhooks/1/aaaa"
            self.config.app_settings["cloud_service_url"] = ""
        self.assertFalse(self.config.cloud_recover_identity())

    def test_recovery_re_registers_and_keeps_the_new_credentials(self):
        calls = []

        def fake_register(service_url=None, webhook_url=None):
            calls.append((service_url, webhook_url))
            with self.config.config_lock:
                self.config.app_settings["cloud_subscriber_id"] = "new-id"
                self.config.app_settings["cloud_token"] = "new-token"
            return True, "registered"

        original = self.config.cloud_register_and_sync
        self.config.cloud_register_and_sync = fake_register
        try:
            with self.config.config_lock:
                self.config.app_settings["discord_webhook"] = "https://discord.com/api/webhooks/1/aaaa"
                self.config.app_settings["cloud_service_url"] = "https://example.invalid"
                self.config.app_settings["cloud_subscriber_id"] = "stale-id"
            self.assertTrue(self.config.cloud_recover_identity())
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.config.app_settings["cloud_subscriber_id"], "new-id")
        finally:
            self.config.cloud_register_and_sync = original

    def test_a_failed_re_registration_reports_false(self):
        original = self.config.cloud_register_and_sync
        self.config.cloud_register_and_sync = lambda service_url=None, webhook_url=None: (False, "down")
        try:
            with self.config.config_lock:
                self.config.app_settings["discord_webhook"] = "https://discord.com/api/webhooks/1/aaaa"
                self.config.app_settings["cloud_service_url"] = "https://example.invalid"
            self.assertFalse(self.config.cloud_recover_identity())
        finally:
            self.config.cloud_register_and_sync = original


class CloudRetryTests(unittest.TestCase):
    """One retry, only on 401, only after proving who we are."""

    def setUp(self):
        from utils import config
        self.config = config
        with config.config_lock:
            self.saved = dict(config.app_settings)
            config.app_settings["cloud_subscriber_id"] = "old-id"
            config.app_settings["cloud_token"] = "old-token"
        self.original_recover = config.cloud_recover_identity

    def tearDown(self):
        self.config.cloud_recover_identity = self.original_recover
        with self.config.config_lock:
            self.config.app_settings.clear()
            self.config.app_settings.update(self.saved)

    def http_error(self, code):
        import urllib.error
        return urllib.error.HTTPError("https://example.invalid", code, "err", None, None)

    def test_a_successful_call_does_not_recover(self):
        recovered = []
        self.config.cloud_recover_identity = lambda: recovered.append(1) or True
        seen = []
        result = self.config.cloud_request_with_recovery(
            lambda sid, token: seen.append((sid, token)) or "done")
        self.assertEqual(result, "done")
        self.assertEqual(recovered, [])
        self.assertEqual(seen, [("old-id", "old-token")])

    def test_a_401_recovers_once_and_retries_with_new_credentials(self):
        def recover():
            with self.config.config_lock:
                self.config.app_settings["cloud_subscriber_id"] = "new-id"
                self.config.app_settings["cloud_token"] = "new-token"
            return True
        self.config.cloud_recover_identity = recover

        calls = []

        def do_request(sid, token):
            calls.append((sid, token))
            if len(calls) == 1:
                raise self.http_error(401)
            return "ok"

        self.assertEqual(self.config.cloud_request_with_recovery(do_request), "ok")
        self.assertEqual(calls, [("old-id", "old-token"), ("new-id", "new-token")])

    def test_a_non_401_error_propagates_without_recovering(self):
        import urllib.error
        recovered = []
        self.config.cloud_recover_identity = lambda: recovered.append(1) or True

        def do_request(sid, token):
            raise self.http_error(500)

        with self.assertRaises(urllib.error.HTTPError):
            self.config.cloud_request_with_recovery(do_request)
        self.assertEqual(recovered, [], "a 500 is not an identity problem")

    def test_a_failed_recovery_raises_rather_than_retrying_blindly(self):
        self.config.cloud_recover_identity = lambda: False
        calls = []

        def do_request(sid, token):
            calls.append(1)
            raise self.http_error(401)

        with self.assertRaises(RuntimeError):
            self.config.cloud_request_with_recovery(do_request)
        self.assertEqual(len(calls), 1, "must not retry when re-registration failed")

    def test_it_retries_only_once(self):
        self.config.cloud_recover_identity = lambda: True
        calls = []

        def do_request(sid, token):
            calls.append(1)
            raise self.http_error(401)

        with self.assertRaises(Exception):
            self.config.cloud_request_with_recovery(do_request)
        self.assertEqual(len(calls), 2, "one original attempt plus exactly one retry")

    def test_sync_cannot_recurse_through_registration(self):
        """A 401 during sync re-registers, and registration syncs again. Without the
        guard a service stuck on 401 would drive that loop forever."""
        import inspect
        sig = inspect.signature(self.config.cloud_sync_watchlist)
        self.assertIn("_allow_recovery", sig.parameters)
        self.assertTrue(sig.parameters["_allow_recovery"].default)
        source = inspect.getsource(self.config.cloud_register_and_sync)
        self.assertIn("_allow_recovery=False", source,
                      "registration must disable recovery on its own sync call")


class ScipyDeferralTests(unittest.TestCase):
    """scipy is excluded from the frozen build (48 MB that never executes), so in the
    packaged app the lazy import cannot be satisfied. Blur is decoration; losing it
    must never take the window down."""

    def test_blur_returns_the_unblurred_image_when_scipy_is_absent(self):
        from utils import fast_start
        names = ("scipy", "scipy.ndimage", "scipy.ndimage.filters")
        saved = [(n, n in sys.modules, sys.modules.get(n)) for n in names]
        try:
            for n in names:
                sys.modules[n] = None      # makes `from scipy...` raise ImportError
            image = ["untouched"]
            self.assertIs(fast_start._lazy_gaussian_filter(image, 3), image)
        finally:
            for n, existed, mod in saved:
                if existed:
                    sys.modules[n] = mod
                else:
                    sys.modules.pop(n, None)

    def test_blur_with_no_arguments_does_not_raise(self):
        from utils import fast_start
        names = ("scipy", "scipy.ndimage", "scipy.ndimage.filters")
        saved = [(n, n in sys.modules, sys.modules.get(n)) for n in names]
        try:
            for n in names:
                sys.modules[n] = None
            self.assertIsNone(fast_start._lazy_gaussian_filter())
        finally:
            for n, existed, mod in saved:
                if existed:
                    sys.modules[n] = mod
                else:
                    sys.modules.pop(n, None)


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


class DownloadDestinationTests(unittest.TestCase):
    """A download button on these sites can be wrapped in an ad redirect and land on
    an interstitial instead of the file. Landing there wastes the whole 35s
    interception window and reports the episode as failed, so the engine checks where
    the click actually went before committing to it."""

    def test_real_drive_download_url_is_accepted(self):
        # The URL witanime's Google Drive button actually opens.
        self.assertTrue(tab_matches_path(
            "https://drive.usercontent.google.com/download?id=1o_XPNYu&export=download",
            "google drive"))

    def test_drive_preview_url_is_accepted(self):
        self.assertTrue(tab_matches_path(
            "https://drive.google.com/file/d/1o_XPNYu/view", "google drive"))

    def test_fast_io_interstitial_is_rejected(self):
        # The page reported in the wild instead of the file.
        self.assertFalse(tab_matches_path(
            "https://www.fast.io/alternatives/google-drive/?utm_source=mfftr_error",
            "google drive"))

    def test_a_different_mirror_is_rejected_for_this_path(self):
        self.assertFalse(tab_matches_path(
            "https://www.mediafire.com/file/abc/ep.mp4", "google drive"))
        self.assertTrue(tab_matches_path(
            "https://www.mediafire.com/file/abc/ep.mp4", "mediafire"))

    def test_subdomains_of_an_allowed_host_are_accepted(self):
        self.assertTrue(tab_matches_path(
            "https://f54.workupload.com/download/xyz", "workupload"))

    def test_lookalike_domain_is_rejected(self):
        # endswith() on a bare name would wrongly accept this.
        self.assertFalse(tab_matches_path(
            "https://mediafire.com.evil.example/file", "mediafire"))

    def test_unknown_path_name_is_allowed_through(self):
        # Profiles are user-editable; an unrecognised path must not be blocked.
        self.assertTrue(tab_matches_path("https://example.com/x", "some custom host"))

    def test_blank_url_is_allowed_through(self):
        # about:blank while the tab is still opening -- the normal waits handle it.
        self.assertTrue(tab_matches_path("", "google drive"))
        self.assertTrue(tab_matches_path("about:blank", "google drive"))

    def test_path_name_matching_ignores_case_and_padding(self):
        self.assertTrue(tab_matches_path(
            "https://drive.google.com/uc?export=download&id=1", "  Google Drive  "))

    def test_major_shipped_paths_are_covered(self):
        """The hosts worth guarding must stay mapped as flows are edited. Paths whose
        host isn't known (witanime's 'rf') are deliberately absent: an unmapped path
        passes the check rather than being blocked, so guessing its host would risk
        rejecting a download that was actually fine."""
        shipped = {p.strip().lower()
                   for flow in DEFAULT_SITE_FLOWS.values()
                   for p in flow["step_paths"]}
        for name in ("google drive", "mediafire", "workupload"):
            if name in shipped:
                self.assertIn(name, PATH_HOSTS, f"'{name}' lost its expected hosts")

    def test_mapped_hosts_are_bare_domains(self):
        """Hosts are compared with == and endswith('.'+host), so a scheme, path or
        leading dot in this table would silently never match anything."""
        for name, hosts in PATH_HOSTS.items():
            for h in hosts:
                self.assertNotIn("/", h, f"{name}: '{h}' should be a bare domain")
                self.assertFalse(h.startswith("."), f"{name}: '{h}' has a leading dot")
                self.assertEqual(h, h.lower(), f"{name}: '{h}' should be lowercase")


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
                    self.assertTrue(step.get("xpath", "").strip(), f"{domain}/{path_name}")
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
        paths = DEFAULT_SITE_FLOWS["witanime.life"]["step_paths"]
        self.assertEqual(set(paths), {"mediafire", "google drive", "Workupload", "rf"})

    def test_witanime_path_order_is_preserved(self):
        """The engine tries paths in order, so ordering is behaviour, not cosmetics."""
        paths = DEFAULT_SITE_FLOWS["witanime.life"]["step_paths"]
        self.assertEqual(list(paths), ["mediafire", "google drive", "Workupload", "rf"])


class SiteDisplayTests(unittest.TestCase):
    """The Search dropdown shows a name and a favicon instead of the raw host."""

    def test_drops_the_tld(self):
        self.assertEqual(site_display_name("witanime.life"), "witanime")

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


class CloudSyncHelperTests(unittest.TestCase):
    """Guards client-side cloud notification sync logic and settings."""

    def setUp(self):
        from utils import config
        with config.config_lock:
            self._saved_settings = dict(config.app_settings)

    def tearDown(self):
        from utils import config
        with config.config_lock:
            config.app_settings.clear()
            config.app_settings.update(self._saved_settings)

    def test_cloud_settings_defaults(self):
        from utils import config
        with config.config_lock:
            self.assertIn("cloud_notify_enabled", config.app_settings)
            self.assertIn("cloud_service_url", config.app_settings)
            self.assertIn("cloud_subscriber_id", config.app_settings)
            self.assertIn("cloud_token", config.app_settings)

    def test_sync_unconfigured_fails_gracefully(self):
        from utils import config
        with config.config_lock:
            config.app_settings["cloud_subscriber_id"] = ""
            config.app_settings["cloud_token"] = ""
        ok, msg = config.cloud_sync_watchlist()
        self.assertFalse(ok)
        self.assertIn("not registered", msg)

    def test_cloud_unsubscribe_clears_credentials(self):
        from utils import config
        with config.config_lock:
            config.app_settings["cloud_notify_enabled"] = True
            config.app_settings["cloud_subscriber_id"] = "test-sub-123"
            config.app_settings["cloud_token"] = "test-tok-456"

        ok, msg = config.cloud_unsubscribe()
        self.assertTrue(ok)
        with config.config_lock:
            self.assertFalse(config.app_settings["cloud_notify_enabled"])
            self.assertEqual(config.app_settings["cloud_subscriber_id"], "")
            self.assertEqual(config.app_settings["cloud_token"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
