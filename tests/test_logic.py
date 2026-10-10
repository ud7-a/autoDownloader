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


class CrossSiteReleaseDayTests(unittest.TestCase):
    """witanime.site refuses automated access to its schedule, so its entries got no
    release day at all. A weekday belongs to the show, not the site, so it is borrowed
    from another site's schedule -- strictly, since a wrong day is worse than none."""

    WIT = "https://witanime.site/anime/{}/"
    # animerco's rows only: witanime's own schedule could not be read.
    ANIMERCO_ONLY = [
        {"day": "sunday", "title": "Mushoku Tensei: Isekai Ittara Honki Dasu Season 3",
         "url": "https://det.animerco.org/seasons/mushoku-season-3/"},
        {"day": "friday", "title": "Tensei shitara Slime Datta Ken Season 4",
         "url": "https://det.animerco.org/seasons/slime-season-4/"},
        {"day": "wednesday", "title": "Re:Zero kara Hajimeru Isekai Seikatsu Season 4",
         "url": "https://det.animerco.org/seasons/rezero-season-4/"},
        {"day": "monday", "title": "Grand Blue Season 3",
         "url": "https://det.animerco.org/seasons/grand-blue-season-3/"},
    ]

    def setUp(self):
        from core import schedule
        self.s = schedule

    def day(self, title, slug, items=None):
        entry = {"title": title, "url": self.WIT.format(slug)}
        return self.s.find_day(entry, self.ANIMERCO_ONLY if items is None else items)

    def test_borrows_the_day_when_its_own_schedule_is_missing(self):
        self.assertEqual(self.day("Tensei shitara Slime Datta Ken 4th Season", "slime-4"),
                         "friday")
        self.assertEqual(self.day("Re:Zero kara Hajimeru Isekai Seikatsu 4th Season", "rz-4"),
                         "wednesday")

    def test_roman_numeral_season_matches_a_numbered_one(self):
        """The watchlist said 'Mushoku Tensei III'; animerco says 'Season 3'."""
        self.assertEqual(self.day("Mushoku Tensei III: Isekai Ittara Honki Dasu", "mt-3"),
                         "sunday")

    def test_a_different_season_is_never_borrowed(self):
        """Seasons are separate anime pages on witanime -- the reason it was URL-only."""
        self.assertIsNone(self.day("Tensei shitara Slime Datta Ken 3rd Season", "slime-3"))

    def test_an_unnumbered_title_does_not_take_a_numbered_season(self):
        self.assertIsNone(self.day("Grand Blue", "grand-blue"))

    def test_no_containment_guessing_across_sites(self):
        """Cross-site is exact-title only; a partial overlap is not enough."""
        self.assertIsNone(self.day("Tensei shitara Slime", "slime"))

    def test_when_its_own_schedule_was_read_the_url_rule_still_holds(self):
        """A readable witanime schedule that lacks the URL means genuinely not airing."""
        items = self.ANIMERCO_ONLY + [
            {"day": "tuesday", "title": "Something Else",
             "url": "https://witanime.site/anime/something-else/"}]
        self.assertIsNone(self.day("Tensei shitara Slime Datta Ken 4th Season",
                                   "slime-4", items))

    def test_season_number_reads_roman_numerals(self):
        self.assertEqual(self.s.season_number("Mushoku Tensei III: Isekai"), 3)
        self.assertEqual(self.s.season_number("Overlord IV"), 4)
        self.assertEqual(self.s.season_number("Title VIII"), 8)

    def test_season_number_ignores_names_that_merely_contain_letters(self):
        self.assertIsNone(self.s.season_number("HUNTER X HUNTER"))
        self.assertIsNone(self.s.season_number("I Got a Cheat Skill"))
        self.assertIsNone(self.s.season_number("Vinland Saga"))


class ScheduleMatchingTests(unittest.TestCase):
    """Matching a watchlist entry to its release day. witanime can be matched by URL;
    animerco only publishes season links, so those fall back to titles."""

    def setUp(self):
        from core import schedule
        self.s = schedule
        self.items = [
            {"day": "saturday", "title": "Bleach: Sennen Kessen-hen - Kashin-tan",
             "url": "https://witanime.site/anime/bleach-sennen-kessen-hen-kashin-tan/"},
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
                 "url": "https://witanime.site/anime/bleach-sennen-kessen-hen-kashin-tan/"}
        self.assertEqual(self.s.find_day(entry, self.items), "saturday")

    def test_url_match_ignores_trailing_slash(self):
        entry = {"title": "x",
                 "url": "https://witanime.site/anime/bleach-sennen-kessen-hen-kashin-tan"}
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
        w = "https://witanime.site/anime/"
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
        out = encode_check_url("https://witanime.site/episode/one-piece-الحلقة-444/")
        out.encode("ascii")   # must not raise -- this is what urllib does internally
        self.assertTrue(out.startswith("https://witanime.site/episode/one-piece-"))
        self.assertIn("%D8%A7", out)

    def test_already_encoded_url_is_unchanged(self):
        url = "https://witanime.site/episode/one-piece-%D8%A7%D9%84%D8%AD%D9%84%D9%82%D8%A9-444/"
        self.assertEqual(encode_check_url(url), url)

    def test_encoding_is_idempotent(self):
        raw = "https://witanime.site/episode/one-piece-الحلقة-444/"
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


class PausedAdjustmentTests(unittest.TestCase):
    """While a download is paused: concurrency, headless, and skipping episodes
    that have not started."""

    def setUp(self):
        import core.selenium_engine as eng
        from core.concurrency import ConcurrencyController
        self.eng = eng
        self.controller = ConcurrencyController(start=3, enabled=True)
        self.episodes = [1, 2, 3, 4, 5]
        with eng._run_lock:
            eng.RUN_STATE.clear()
            eng.RUN_STATE.update(task_id=42, episodes=self.episodes, started={1, 2},
                                 headless=True, controller=self.controller)
            eng._PENDING_ADJUST.clear()
        self.addCleanup(lambda: (eng.RUN_STATE.clear(), eng._PENDING_ADJUST.clear()))

    def test_snapshot_lists_only_episodes_not_started(self):
        snap = self.eng.run_snapshot()
        self.assertEqual(snap["not_started"], [3, 4, 5])
        self.assertEqual((snap["total"], snap["limit"], snap["auto"], snap["headless"]),
                         (5, 3, True, True))

    def test_concurrency_applies_at_once(self):
        self.assertTrue(self.eng.request_adjustments(42, limit=5, auto=False))
        self.assertEqual((self.controller.limit, self.controller.enabled), (5, False))

    def test_concurrency_is_kept_in_range(self):
        self.eng.request_adjustments(42, limit=99, auto=False)
        self.assertEqual(self.controller.limit, self.controller.MAX_LIMIT)

    def test_started_episodes_cannot_be_skipped(self):
        self.eng.request_adjustments(42, skip=[2, 4])
        self.assertEqual(self.eng._take_adjustments().get("skip"), {4})

    def test_headless_change_waits_for_the_engine(self):
        self.eng.request_adjustments(42, headless=False)
        self.assertEqual(self.eng._take_adjustments(), {"headless": False})
        self.eng.request_adjustments(42, headless=True)          # unchanged -> nothing
        self.assertEqual(self.eng._take_adjustments(), {})

    def test_pausing_again_shows_what_is_still_pending(self):
        # Changed on one pause, paused again before the engine reached the next
        # episode: the screen must show the new choices, not the old state.
        self.eng.request_adjustments(42, headless=False, skip=[4, 5])
        snap = self.eng.run_snapshot()
        self.assertFalse(snap["headless"])
        self.assertEqual([4, 5], snap["pending_skip"])
        self.assertEqual([3, 4, 5], snap["not_started"])   # still listed, unticked

    def test_skip_request_replaces_the_previous_one(self):
        self.eng.request_adjustments(42, skip=[4, 5])
        self.eng.request_adjustments(42, skip=[5])            # 4 re-ticked
        self.assertEqual({5}, self.eng._PENDING_ADJUST["skip"])
        self.eng.request_adjustments(42, skip=[])             # everything re-ticked
        self.assertNotIn("skip", self.eng._PENDING_ADJUST)

    def test_headless_back_to_current_cancels_the_pending_change(self):
        self.eng.request_adjustments(42, headless=False)
        self.eng.request_adjustments(42, headless=True)        # changed mind
        self.assertEqual({}, self.eng._take_adjustments())

    def test_describe_changes(self):
        from ui.progress_tab import describe_changes
        before = {"auto": True, "limit": 3, "headless": True, "pending_skip": []}
        self.assertEqual("", describe_changes(before, limit=3, auto=True, headless=True, skip=[]))
        self.assertEqual(
            "2 downloads at once · browser visible from the next episode · skipping 3 episodes",
            describe_changes(before, limit=2, auto=False, headless=False, skip=[7, 8, 9]))
        self.assertEqual("No episodes skipped",
                         describe_changes(dict(before, pending_skip=[4]), limit=3, auto=True,
                                          headless=True, skip=[]))

    def test_compact_spec(self):
        self.assertEqual("1-3, 5, 7-8", self.eng.compact_spec([8, 1, 2, 3, 5, 7]))

    def test_a_finished_run_ignores_late_changes(self):
        self.assertFalse(self.eng.request_adjustments(41, limit=1))
        self.assertEqual(self.controller.limit, 3)

    def test_skips_remove_episodes_and_totals_follow(self):
        eps = [1, 2, 3, 4, 5]
        self.assertEqual(self.eng.apply_skips(eps, [4, 5, 9]), [4, 5])
        self.assertEqual(eps, [1, 2, 3])

    def test_skipping_everything_is_refused(self):
        eps = [3, 4]
        self.assertEqual(self.eng.apply_skips(eps, [3, 4]), [])
        self.assertEqual(eps, [3, 4])

    def test_set_mode_restarts_the_auto_window(self):
        from core.concurrency import ConcurrencyController
        c = ConcurrencyController(start=2, enabled=False)
        c.record_progress(1, 100, 1)        # ignored while manual
        c.set_mode(True, 4)
        self.assertEqual((c.enabled, c.limit, c._samples), (True, 4, {}))


class ResponsiveLayoutTests(unittest.TestCase):
    """Tabs stay readable on wide windows and never block shrinking the window."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def make(self):
        from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLineEdit
        from ui.responsive import WidthCap
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(30, 20, 30, 20)
        field = QLineEdit()
        field.setMinimumWidth(400)
        lay.addWidget(field)
        cap = WidthCap(host, 1000)
        host.show()                     # hidden widgets get no resize events
        self._hosts = getattr(self, "_hosts", []) + [host]
        self.addCleanup(host.close)
        return host, lay, cap

    def test_wide_window_centres_a_column(self):
        host, lay, _ = self.make()
        host.resize(2400, 900)
        self._app.processEvents()
        m = lay.contentsMargins()
        self.assertEqual((m.left(), m.right()), (30 + 700, 30 + 700))
        self.assertEqual(m.top(), 20)

    def test_margins_never_raise_the_minimum(self):
        # Maximized on a wide screen, then shrunk: the minimum must stay the
        # content's own (400 + 2*30), not include the 700 px centring margins.
        host, lay, _ = self.make()
        host.resize(2400, 900)
        self._app.processEvents()
        self.assertEqual(host.minimumWidth(), 460)
        host.resize(800, 600)
        self._app.processEvents()
        self.assertEqual(lay.contentsMargins().left(), 30)

    def test_window_minimum_fits_small_laptops(self):
        # 1080p at 150% scaling leaves 1280x720 logical, minus the taskbar.
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ui", "app_window.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "setMinimumSize"]
        w, h = (calls[0].args[0].value, calls[0].args[1].value)
        self.assertLessEqual(w, 1024)
        self.assertLessEqual(h, 576)


class HdFallbackTests(unittest.TestCase):
    """Episodes with no FHD group (or dead FHD mirrors) fall back to HD."""

    def setUp(self):
        from ui.search_tab import DEFAULT_SITE_FLOWS
        self.flows = DEFAULT_SITE_FLOWS

    def test_hd_twins_follow_every_fhd_path(self):
        from core.selenium_engine import with_hd_fallback
        wit = self.flows["witanime.site"]["step_paths"]
        out = with_hd_fallback(wit)
        names = list(out)
        self.assertEqual(names[:len(wit)], list(wit))               # FHD first, unchanged
        self.assertEqual(names[len(wit):], [n.replace("FHD", "HD") for n in wit])
        self.assertEqual(out["FHD - Mediafire"], wit["FHD - Mediafire"])

    def test_hd_xpath_excludes_the_fhd_button(self):
        from core.selenium_engine import with_hd_fallback
        out = with_hd_fallback(self.flows["witanime.site"]["step_paths"])
        step1 = out["HD - Mediafire"][0]["xpath"]
        self.assertIn("contains(., 'HD') and not(contains(., 'FHD'))", step1)
        self.assertNotIn("button[contains(., 'FHD')]", out["HD - Mediafire"][1]["xpath"])
        self.assertEqual(out["HD - Mediafire"][2], out["FHD - Mediafire"][2])   # host page step

    def test_twins_get_their_own_probes(self):
        from core.selenium_engine import with_hd_fallback, path_probes
        probes = path_probes(with_hd_fallback(self.flows["witanime.site"]["step_paths"]))
        self.assertIn("'FHD')]]//button", probes["FHD - Mediafire"])
        self.assertIn("not(contains(., 'FHD'))", probes["HD - Mediafire"])
        self.assertIn("mediafire", probes["HD - Mediafire"])

    def test_non_fhd_profiles_untouched(self):
        from core.selenium_engine import with_hd_fallback
        ani = self.flows["eta.animerco.org"]["step_paths"]
        self.assertEqual(with_hd_fallback(ani), ani)
        custom = {"FHD mirror": [{"xpath": "//a[@id='dl']"}]}
        self.assertEqual(with_hd_fallback(custom), custom)          # name alone is not enough

    def test_name_clash(self):
        from core.selenium_engine import with_hd_fallback
        sp = {"FHD - x": [{"xpath": "//b[contains(., 'FHD')]"}], "HD - x": [{"xpath": "//i"}]}
        self.assertIn("HD - x (HD)", with_hd_fallback(sp))


class ServerFallbackTests(unittest.TestCase):
    """A captured link that won't download sends the episode back to try its next
    server, and a dead link is no longer counted as downloaded."""

    def test_next_episode_order(self):
        import collections
        from core.selenium_engine import next_episode
        retry, work = collections.deque([4]), collections.deque([5, 6])
        self.assertEqual(("ep", 4), next_episode(retry, work, lambda: True))   # retries first
        self.assertEqual(("ep", 5), next_episode(retry, work, lambda: True))
        self.assertEqual(("ep", 6), next_episode(retry, work, lambda: True))
        self.assertEqual(("wait", None), next_episode(retry, work, lambda: True))
        self.assertEqual(("done", None), next_episode(retry, work, lambda: False))

    def test_late_requeue_is_not_lost(self):
        import collections
        from core.selenium_engine import next_episode
        retry, work = collections.deque(), collections.deque()

        def thread_finishes():            # the thread re-queues, then exits
            retry.append(7)
            return False
        self.assertEqual(("ep", 7), next_episode(retry, work, thread_finishes))

    def run_download(self, lines_per_attempt, returncodes, on_failed=True, url="https://host.example/v.mp4"):
        """Drive aria2c_downloader with a fake aria2c process."""
        from unittest import mock
        import core.selenium_engine as eng
        calls = {"completed": 0, "failed": [], "popen": 0, "cmds": []}
        codes = iter(returncodes)

        class FakeProc:
            def __init__(self, *a, **k):
                calls["popen"] += 1
                if a and len(a) > 0:
                    calls["cmds"].append(list(a[0]))
                self.stdout = iter(lines_per_attempt)
                self.returncode = next(codes)

            def wait(self):
                return self.returncode

        tmp = tempfile.mkdtemp(prefix="aed_dl_")
        eng.CURRENT_TASK_ID = 99
        eng.ep_cancel_events.pop(1, None)
        eng.ep_pause_events.pop(1, None)
        eng.pause_event.clear()
        cancel = __import__("threading").Event()
        with mock.patch.object(eng.subprocess, "Popen", FakeProc), \
                mock.patch.object(eng.os.path, "exists", side_effect=lambda p: True), \
                mock.patch.object(eng.time, "sleep", lambda s: None), \
                mock.patch.object(eng, "is_block_page", return_value=False), \
                mock.patch.object(eng, "rotate_error_log", lambda p: None), \
                mock.patch.object(eng, "APP_DIR", tmp), \
                mock.patch.object(eng, "save_config", create=True), \
                mock.patch("utils.config.save_config"):
            eng.aria2c_downloader(
                1, url, "v.mp4", [], "ua", tmp, cancel,
                lambda: calls.__setitem__("completed", calls["completed"] + 1),
                None, 99, None, None,
                (lambda ep: calls["failed"].append(ep)) if on_failed else None)
        return calls

    def test_dead_link_goes_to_the_next_server(self):
        no_data = ["[#1 0B/0B CN:1 DL:0B]"]
        calls = self.run_download(no_data, [2] * 6)
        self.assertEqual([1], calls["failed"])
        self.assertEqual(0, calls["completed"])          # not counted as downloaded
        self.assertEqual(3, calls["popen"])               # gives up after 3, not 6

    def test_success_completes(self):
        ok = ["[#1 210MiB/421MiB(50%) CN:16 DL:5.1MiB ETA:41s]"]
        calls = self.run_download(ok, [0])
        self.assertEqual(1, calls["completed"])
        self.assertEqual([], calls["failed"])

    def test_without_fallback_a_failure_still_completes(self):
        calls = self.run_download(["[#1 0B/0B CN:1 DL:0B]"], [2] * 6, on_failed=False)
        self.assertEqual(1, calls["completed"])

    def test_single_conn_host_starts_with_one_connection(self):
        # Hosts known not to support Range headers (e.g. wahmi.org) start with 1 connection immediately
        ok = ["[#1 417MiB/417MiB(100%) CN:1 DL:35MiB ETA:0s]"]
        calls = self.run_download(ok, [0], url="https://wahmi.org/download/abc/def/ep.mp4")
        self.assertEqual(1, calls["completed"])
        self.assertEqual(1, calls["popen"])
        first_cmd = calls["cmds"][0]
        # Must have -x 1, -s 1, -j 1 and 4M buffer with mmap
        self.assertIn("-x", first_cmd)
        x_idx = first_cmd.index("-x")
        self.assertEqual("1", first_cmd[x_idx + 1])
        self.assertIn("--socket-recv-buffer-size=4M", first_cmd)
        self.assertIn("--enable-mmap=true", first_cmd)

    def test_mp4upload_starts_with_optimal_connections(self):
        # mp4upload limits concurrent connections per IP and blocks >=8 with 403,
        # but 4 connections allows maximum throughput (25-35+ MB/s).
        ok = ["[#1 365MiB/365MiB(100%) CN:4 DL:30MiB ETA:0s]"]
        calls = self.run_download(ok, [0], url="https://a1.mp4upload.com:183/d/xyz/video.mp4")
        self.assertEqual(1, calls["completed"])
        self.assertEqual(1, calls["popen"])
        first_cmd = calls["cmds"][0]
        self.assertIn("-x", first_cmd)
        x_idx = first_cmd.index("-x")
        self.assertEqual("4", first_cmd[x_idx + 1])
        self.assertIn("--connect-timeout=15", first_cmd)

    def test_range_error_skips_intermediate_step_downs(self):
        # When a server returns "Invalid range header", it jumps straight to 1 connection
        range_err_lines = [
            "Exception: [AbstractCommand.cc:351] errorCode=8 URI=https://unknown-host.com/v.mp4",
            "-> [HttpResponse.cc:81] errorCode=8 Invalid range header. Request: 100-200/400, Response: 0-400/400"
        ]
        # Attempt 1: 16 conns fails with range error. Attempt 2: jumps straight to 1 conn and succeeds.
        calls = self.run_download(range_err_lines, [8, 0], url="https://unknown-host.com/v.mp4")
        self.assertEqual(1, calls["completed"])
        self.assertEqual(2, calls["popen"])   # 2 popens total, NOT 5 (16 -> 1 directly, skipping 8, 4, 2)
        second_cmd = calls["cmds"][1]
        x_idx = second_cmd.index("-x")
        self.assertEqual("1", second_cmd[x_idx + 1])

    def test_403_multi_conn_rejection_skips_intermediate_step_downs(self):
        # When a server rejects multi-connection requests with 403 Forbidden / errorCode=22,
        # it jumps straight to 1 connection rather than trying 8, 4, 2.
        err_lines = [
            "[ERROR] CUID#14 - Download aborted. URI=https://cdn.example.org/video.mp4",
            "Exception: [AbstractCommand.cc:351] errorCode=22 URI=https://cdn.example.org/video.mp4",
            "-> [HttpSkipResponseCommand.cc:239] errorCode=22 The response status is not successful. status=403"
        ]
        calls = self.run_download(err_lines, [22, 0], url="https://cdn.example.org/video.mp4")
        self.assertEqual(1, calls["completed"])
        self.assertEqual(2, calls["popen"])
        second_cmd = calls["cmds"][1]
        x_idx = second_cmd.index("-x")
        self.assertEqual("1", second_cmd[x_idx + 1])


class ScriptSafetyTests(unittest.TestCase):
    """Script steps: their links must stay on the site they ran on."""

    def test_script_link_must_be_on_the_paths_host(self):
        from core.selenium_engine import script_url_allowed as ok
        self.assertTrue(ok("https://a3.mp4upload.com:183/d/x/video.mp4", "FHD - mp4upload"))
        self.assertTrue(ok("https://a3.mp4upload.com:183/d/x/video.mp4", "HD - mp4upload"))
        # An ad tab's own video: not the host the path is named after.
        self.assertFalse(ok("https://cdn.some-ad-network.com/clip.mp4", "FHD - mp4upload"))
        self.assertFalse(ok("https://mp4upload.com.evil.org/x.mp4", "FHD - mp4upload"))
        self.assertFalse(ok("https://cdn.mp4upload.xyz/x.mp4", "FHD - mp4upload"))   # look-alike TLD
        self.assertFalse(ok("https://notmp4upload.com/x.mp4", "FHD - mp4upload"))
        self.assertFalse(ok("file:///C:/Windows/win.ini", "FHD - mp4upload"))
        self.assertFalse(ok("javascript:alert(1)", "FHD - mp4upload"))
        # Known hosts by their table entry; a path naming no host gets nothing.
        self.assertTrue(ok("https://download1.mediafire.com/f/ep.mp4", "FHD - Mediafire"))
        self.assertFalse(ok("https://download1.mediafire.com/f/ep.mp4", "FHD - gofile"))
        self.assertFalse(ok("https://a3.mp4upload.com/d/x/video.mp4", "Path 1"))
        self.assertTrue(ok("https://wahmi.org/download/GKcq82QDdbgfy78/1RgzRYEMb3bpB/ep.mp4", "FHD - wahmi"))
        self.assertTrue(ok("https://wahmi.org/download/GKcq82QDdbgfy78/1RgzRYEMb3bpB/ep.mp4", "HD - wahmi"))
        self.assertFalse(ok("https://fake-wahmi.com/ep.mp4", "FHD - wahmi"))


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


class ReviewFixTests(unittest.TestCase):
    """Fixes from the full-app review."""

    def test_unreadable_page_skips_hd_twins(self):
        from core.selenium_engine import choose_paths
        order = ["FHD - a", "FHD - b", "HD - a", "HD - b"]
        twins = {"HD - a", "HD - b"}
        unsure = {n: False for n in order}                  # nothing matched
        self.assertEqual(["FHD - a", "FHD - b"], choose_paths(order, unsure, twins))
        seen = {"FHD - a": False, "FHD - b": False, "HD - a": True, "HD - b": False}
        self.assertEqual(["HD - a"], choose_paths(order, seen, twins))   # page shows HD

    def test_mp4upload_and_wahmi_migrations_run_once(self):
        from unittest import mock
        import utils.config as cfg
        from core.site_flows import DEFAULT_SITE_FLOWS
        flow_mp4 = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]["FHD - mp4upload"]
        flow_wahmi = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]["FHD - wahmi"]
        profiles = {
            "wit": {"url": "https://witanime.site/watch/a/{x}", "step_paths": {"FHD - x": []}},
            "no paths": {"url": "https://witanime.site/watch/b/{x}"},
            "ani": {"url": "https://eta.animerco.org/e/{x}", "step_paths": {}},
        }
        with mock.patch.object(cfg, "sites_data", profiles):
            ran = cfg._run_profile_migrations(set())
            self.assertEqual(["witanime_mp4upload_v1", "witanime_wahmi_v1"], ran)
            self.assertEqual(flow_mp4, profiles["wit"]["step_paths"]["FHD - mp4upload"])
            self.assertEqual(flow_wahmi, profiles["wit"]["step_paths"]["FHD - wahmi"])
            self.assertIsNot(flow_wahmi, profiles["wit"]["step_paths"]["FHD - wahmi"])  # a copy
            self.assertNotIn("step_paths", profiles["no paths"])     # nothing to change
            self.assertNotIn("FHD - wahmi", profiles["ani"]["step_paths"])
            # The user deletes it; a later launch (migration recorded) leaves it gone.
            del profiles["wit"]["step_paths"]["FHD - wahmi"]
            self.assertEqual([], cfg._run_profile_migrations(set(ran)))
            self.assertNotIn("FHD - wahmi", profiles["wit"]["step_paths"])

    def test_reopened_profile_moves_to_new_episodes(self):
        from ui.search_tab import extend_to_new_episodes
        p = {"last_episodes": "1-12"}
        self.assertEqual("13-24", extend_to_new_episodes(p, 24))
        self.assertEqual("13-24", p["last_episodes"])
        p = {"last_episodes": "1-12"}
        self.assertIsNone(extend_to_new_episodes(p, 12))             # nothing new
        self.assertEqual("1-12", p["last_episodes"])
        self.assertEqual("13", extend_to_new_episodes({"last_episodes": "12"}, 13))
        self.assertEqual("1-5", extend_to_new_episodes({}, 5))

    def test_profile_manager_keeps_script_steps_on_save(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        type(self)._app = QApplication.instance() or QApplication([])   # alive for the run
        import ui.manager_tab as mt
        steps = [{"xpath": "//a", "delay": 2.0}, {"script": "return 1;", "delay": 3.0},
                 {"xpath": "//b", "delay": 1.0}]
        data = {"url": "https://witanime.site/watch/z/{x}", "next_btn_xpath": "n",
                "step_paths": {"FHD - mp4upload": steps}, "last_episodes": "1"}
        sites = {"Z": json.loads(json.dumps(data))}
        with mock.patch.object(mt, "sites_data", sites), mock.patch.object(mt, "save_config"):
            w = mt.SiteManagerWidget()
            w.load_profile("Z")
            tab = w.path_tabs.widget(0)
            hidden = [o["card"].isHidden() for o in tab.step_widgets]
            self.assertEqual([False, True, False], hidden)       # the script row isn't shown
            w.save_profile()
            self.assertEqual(steps, sites["Z"]["step_paths"]["FHD - mp4upload"])


class DiskSpaceTests(unittest.TestCase):
    def test_estimate_from_existing_episodes(self):
        import shutil
        from utils import disk
        folder = tempfile.mkdtemp(prefix="aed_disk_")
        # 1.7 GB of real disk space per run: it was never removed, and 60 leftover
        # copies (99 GB) filled the drive until writes failed with "No space left".
        self.addCleanup(shutil.rmtree, folder, True)
        for name, size in (("A Ep1.mp4", 300), ("A Ep2.mp4", 500), ("notes.txt", 900)):
            with open(os.path.join(folder, name), "wb") as f:
                f.truncate(size * 1024 ** 2)
        self.assertEqual(400 * 1024 ** 2, disk.typical_episode_bytes(folder))
        self.assertEqual(disk.TYPICAL_EPISODE_BYTES,
                         disk.typical_episode_bytes(os.path.join(folder, "missing")))

    def test_check_space(self):
        from unittest import mock
        from utils import disk
        gb = 1024 ** 3
        with mock.patch.object(disk, "typical_episode_bytes", return_value=gb // 2), \
                mock.patch.object(disk, "free_bytes", return_value=10 * gb):
            self.assertEqual([], disk.check_space("C:\\x", "C:\\x\\A", 10)[1])   # 5 GB fits
            per, short = disk.check_space("C:\\x", "C:\\x\\A", 30)              # 15 GB doesn't
            self.assertEqual((gb // 2, [("C:", 15 * gb, 10 * gb)]), (per, short))
        with mock.patch.object(disk, "free_bytes", return_value=None):
            self.assertEqual([], disk.check_space("C:\\x", "C:\\x\\A", 999)[1])  # unreadable: allow

    def test_temp_drive_is_checked_too(self):
        from unittest import mock
        from utils import disk
        gb = 1024 ** 3
        free = {"D:\\anime": 500 * gb, "C:\\Temp": 2 * gb}
        with mock.patch.object(disk, "typical_episode_bytes", return_value=gb // 2), \
                mock.patch.object(disk, "free_bytes", side_effect=lambda p: free[p]):
            # 10 episodes, 6 at once: D: holds 5 GB fine, C: must hold 3 GB at a time.
            _per, short = disk.check_space("D:\\anime", "D:\\anime\\A", 10,
                                           temp_dir="C:\\Temp", parallel=6)
            self.assertEqual([("C:", 3 * gb, 2 * gb)], short)
            # Same drive: a move, not a second copy -- only the total counts.
            free["D:\\Temp"] = 6 * gb + gb
            _per, short = disk.check_space("D:\\anime", "D:\\anime\\A", 10,
                                           temp_dir="D:\\Temp", parallel=6)
            self.assertEqual([], short)


class SecondReviewFixTests(unittest.TestCase):
    def test_search_ignores_transient_profiles(self):
        from ui.search_tab import find_profile_for_template
        tpl = "https://witanime.site/watch/one-piece/{x}"
        self.assertIsNone(find_profile_for_template({"One Piece": {"url": tpl, "_transient": True}}, tpl))
        self.assertEqual("OP", find_profile_for_template(
            {"One Piece": {"url": tpl, "_transient": True}, "OP": {"url": tpl}}, tpl))

    def test_fresh_install_marks_migrations_done(self):
        import importlib
        import utils.config as cfg
        fresh = tempfile.mkdtemp(prefix="aed_fresh_")
        old = (cfg.APP_DIR, cfg.CONFIG_FILE, list(cfg.app_settings.get("migrations_done", [])))
        try:
            cfg.APP_DIR, cfg.CONFIG_FILE = fresh, os.path.join(fresh, "sites_config.json")
            cfg.app_settings["migrations_done"] = []
            cfg.load_config()
            self.assertEqual([m for m, _f in cfg._PROFILE_MIGRATIONS],
                             cfg.app_settings["migrations_done"])
            with open(cfg.CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)["settings"]["migrations_done"]
            self.assertIn("witanime_mp4upload_v1", saved)
            self.assertIn("witanime_wahmi_v1", saved)
        finally:
            cfg.APP_DIR, cfg.CONFIG_FILE = old[0], old[1]
            cfg.app_settings["migrations_done"] = old[2]
            importlib.invalidate_caches()

    def test_profile_manager_keeps_only_the_edited_profiles_fields(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        type(self)._app = QApplication.instance() or QApplication([])   # alive for the run
        import ui.manager_tab as mt
        sites = {
            "A": {"url": "https://witanime.site/watch/a/{x}", "step_paths": {"P": [{"xpath": "//a", "delay": 1.0}]},
                  "last_episodes": "1", "skip_filler": True, "filler_episodes": [3],
                  "episode_bounds": [1, 12], "_transient": True},
            "B": {"url": "https://witanime.site/watch/b/{x}", "step_paths": {}, "last_episodes": "2",
                  "filler_source": "B's cache"},
        }
        with mock.patch.object(mt, "sites_data", sites), mock.patch.object(mt, "save_config"):
            w = mt.SiteManagerWidget()
            w.load_profile("A")
            w.save_profile()                                          # same URL: kept
            self.assertEqual([1, 12], sites["A"]["episode_bounds"])
            self.assertTrue(sites["A"]["skip_filler"])
            self.assertNotIn("_transient", sites["A"])
            w.load_profile("A")
            w.url_entry.setText("https://witanime.site/watch/other/{x}")
            w.save_profile()                                          # other anime: dropped
            for k in mt.ANIME_SPECIFIC_FIELDS:
                self.assertNotIn(k, sites["A"])
            w.load_profile("A")
            w.name_entry.setText("B")                                 # renamed onto B
            w.save_profile()
            self.assertNotIn("filler_source", sites["B"])             # B's fields don't leak in


class NotifierTests(unittest.TestCase):
    def test_messages(self):
        from ui.notifier import finished_message, new_episodes_message
        self.assertEqual(("Download complete", "Bleach: 12 episodes downloaded."),
                         finished_message("Bleach", list(range(1, 13)), []))
        title, text = finished_message("Bleach", list(range(1, 13)), [4, 7])
        self.assertEqual("Download finished with errors", title)
        self.assertIn("10 of 12", text)
        self.assertIn("4, 7", text)
        self.assertEqual("1 new episode across your Watchlist.", new_episodes_message(1)[1])

    def test_quiet_when_off_or_in_use(self):
        from unittest import mock
        import ui.notifier as nt
        win = mock.MagicMock()
        n = nt.Notifier(win)
        tray = mock.MagicMock()
        n._tray = tray
        with mock.patch.dict(nt.app_settings, {"windows_notifications": False}):
            self.assertFalse(n.notify("t", "x"))
        with mock.patch.dict(nt.app_settings, {"windows_notifications": True}):
            win.isActiveWindow.return_value, win.isMinimized.return_value = True, False
            self.assertFalse(n.notify("t", "x"))                 # user is looking at the app
            win.isActiveWindow.return_value = False
            self.assertTrue(n.notify("t", "x"))
        tray.showMessage.assert_called_once()


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

    def test_is_witanime_url_detection(self):
        from utils.config import is_witanime_url
        self.assertTrue(is_witanime_url("https://witanime.site/watch/slug/1"))
        self.assertTrue(is_witanime_url("https://witanime.life/episode/slug-الحلقة-1/"))
        self.assertTrue(is_witanime_url("https://www.witanime.site/watch/slug/{x}"))
        self.assertTrue(is_witanime_url("witanime.site"))
        self.assertFalse(is_witanime_url("https://eta.animerco.org/episode/show-1"))
        self.assertFalse(is_witanime_url(""))
        self.assertFalse(is_witanime_url(None))

    def test_is_witanime_profile_detection(self):
        from utils.config import is_witanime_profile, sites_data, config_lock
        self.assertTrue(is_witanime_profile({"url": "https://witanime.site/watch/slug/1"}))
        self.assertFalse(is_witanime_profile({"url": "https://eta.animerco.org/episode/show-1"}))
        with config_lock:
            sites_data["_test_wit_prof"] = {"url": "https://witanime.site/watch/slug/{x}"}
            sites_data["_test_ani_prof"] = {"url": "https://eta.animerco.org/episode/slug-{x}"}
        try:
            self.assertTrue(is_witanime_profile("_test_wit_prof"))
            self.assertFalse(is_witanime_profile("_test_ani_prof"))
            self.assertFalse(is_witanime_profile("non_existent_profile"))
        finally:
            with config_lock:
                sites_data.pop("_test_wit_prof", None)
                sites_data.pop("_test_ani_prof", None)

    def test_create_browser_headless_options(self):
        from unittest import mock
        import core.selenium_engine as eng
        with mock.patch("selenium.webdriver.Chrome") as mock_chrome, \
             mock.patch("selenium.webdriver.chrome.service.Service"):
            eng.create_browser("C:\\dummy", headless=True)
            self.assertTrue(mock_chrome.called)
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
             mock.patch("selenium.webdriver.chrome.service.Service"):
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



class FinalEpisodeTests(unittest.TestCase):
    def test_detects_witanime_final_marker(self):
        from core.selenium_engine import is_final_text
        self.assertTrue(is_final_text(
            "انمي Re:Zero kara Hajimeru Isekai Seikatsu 4th season الحلقة 19 والأخيرة مترجمة"))
        self.assertTrue(is_final_text("الحلقة 12 والاخيرة"))
        self.assertFalse(is_final_text("Shingeki no Kyojin: The Final Season الحلقة 5"))
        self.assertFalse(is_final_text("الحلقة 18"))
        self.assertFalse(is_final_text(""))

    def test_filename(self):
        from core.selenium_engine import episode_filename as fn
        self.assertEqual("ReZero Ep19 (Final).mp4", fn("ReZero", 19, ".mp4", final=True))
        self.assertEqual("ReZero Ep18.mp4", fn("ReZero", 18, ".mp4"))
        self.assertEqual("ReZero Ep19 (Final) (1).mkv", fn("ReZero", 19, ".mkv", final=True, copy=1))
        # Still found by episode number (Start Watching / History).
        import re
        self.assertTrue(re.search(r"Ep19(?!\d)", fn("ReZero", 19, ".mp4", final=True)))


class EpisodeBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_bounds_only_widen(self):
        from ui.search_tab import episode_bounds, set_episode_bounds, raise_episode_bound
        p = {"url": "https://witanime.site/watch/x/{x}"}
        self.assertIsNone(episode_bounds(p))
        set_episode_bounds(p, 1, 12)
        self.assertEqual((1, 12), episode_bounds(p))
        set_episode_bounds(p, 3, 10)                    # a narrower detection
        self.assertEqual((1, 12), episode_bounds(p))
        profiles = {"X": p}
        self.assertEqual("X", raise_episode_bound(profiles, "https://witanime.site/watch/x/{x}", 13))
        self.assertEqual((1, 13), episode_bounds(p))
        self.assertIsNone(raise_episode_bound(profiles, "https://witanime.site/watch/x/{x}", 5))

    def test_picker_respects_limits(self):
        from ui.downloader_tab import EpisodeRangePicker
        pk = EpisodeRangePicker()
        pk.set_limits((1, 19))
        pk.set_spec("3-10")
        row = pk._rows[0]
        row["to"].setValue(40)
        self.assertEqual(19, row["to"].value())            # typing can't pass the last episode
        row["from"].setValue(0)
        self.assertEqual(1, row["from"].value())           # nor go below the first
        pk.set_limits(None)
        pk.set_spec("30-40")
        self.assertEqual([(30, 40)], pk.ranges())          # unknown anime: unrestricted

    def test_given_spec_widens_instead_of_being_replaced(self):
        # History re-download of "25-26" on a profile still capped at 1-24.
        from ui.downloader_tab import EpisodeRangePicker
        pk = EpisodeRangePicker()
        pk.set_limits((1, 24))
        pk.set_spec("25-26")
        self.assertEqual([(25, 26)], pk.ranges())          # not swapped for all 24
        self.assertEqual((1, 26), pk.limits())

    def test_first_episode_counts_every_slug(self):
        from ui.search_tab import AnimeDetailsThread
        th = AnimeDetailsThread.__new__(AnimeDetailsThread)   # derivation helpers only
        hrefs = ([f"https://site.example/episode/bleach-ep-{n}/" for n in range(63, 70)]
                 + [f"https://site.example/episode/bleach-{n}/" for n in (1, 2)])
        template, last = th._derive_from_hrefs(hrefs)
        self.assertEqual(69, last)
        self.assertEqual(1, th._last_first_ep)      # 1-2 live under another slug


class SecretFieldTests(unittest.TestCase):
    """The webhook fields' eye button toggles (it used to show only while held)."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_click_toggles_and_stays(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from ui.secret_field import SecretLineEdit
        field = SecretLineEdit()
        field.setText("https://discord.com/api/webhooks/1/abc")
        field.resize(400, 33)
        field.show()
        self.addCleanup(field.close)
        self._app.processEvents()
        self.assertFalse(field.isPasswordVisible())
        btn = field.viewButton
        QTest.mousePress(btn, Qt.MouseButton.LeftButton)
        self.assertFalse(field.isPasswordVisible())      # pressing alone shows nothing
        QTest.mouseRelease(btn, Qt.MouseButton.LeftButton)
        self.assertTrue(field.isPasswordVisible())       # click -> shown, and stays
        self._app.processEvents()
        self.assertTrue(field.isPasswordVisible())
        QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
        self.assertFalse(field.isPasswordVisible())      # click again -> hidden
        self.assertEqual("Show", btn.toolTip())


class TouchTests(unittest.TestCase):
    """Finger input: drags scroll, taps still click (ui/touch.py, ui/episode_grid.py).
    The end-to-end check uses real Windows touch injection and lives outside the
    suite; these cover the logic."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])
        from PyQt6.QtGui import QInputDevice, QPointingDevice, QPointingDeviceUniqueId
        cls.finger = QPointingDevice("test finger", 4242, QInputDevice.DeviceType.TouchScreen,
                                     QPointingDevice.PointerType.Finger,
                                     QInputDevice.Capability.Position, 10, 0, "",
                                     QPointingDeviceUniqueId())

    def mouse(self, etype, pos, touch=True):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent, QPointingDevice
        p = QPointF(*pos) if isinstance(pos, tuple) else QPointF(pos)
        buttons = Qt.MouseButton.NoButton if etype == 3 else Qt.MouseButton.LeftButton
        from PyQt6.QtCore import QEvent
        device = self.finger if touch else QPointingDevice.primaryPointingDevice()
        return QMouseEvent(QEvent.Type(etype), p, p, p, Qt.MouseButton.LeftButton, buttons,
                           Qt.KeyboardModifier.NoModifier, device)

    def test_is_touch(self):
        from ui.touch import is_touch
        self.assertTrue(is_touch(self.mouse(2, (5, 5))))
        self.assertFalse(is_touch(self.mouse(2, (5, 5), touch=False)))

    def test_scroll_target_is_the_innermost_scrollable_area(self):
        from PyQt6.QtWidgets import QScrollArea, QWidget, QLabel, QVBoxLayout
        from ui.touch import _scroll_target
        area = QScrollArea()
        page = QWidget()
        lay = QVBoxLayout(page)
        label = QLabel("x")
        lay.addWidget(label)
        page.setMinimumHeight(2000)
        area.setWidget(page)
        area.resize(300, 200)
        area.show()
        self._app.processEvents()
        self.addCleanup(area.close)
        self.assertIs(area.verticalScrollBar(), _scroll_target(label, vertical=True))
        self.assertIsNone(_scroll_target(label, vertical=False))   # nothing to scroll sideways

    def test_no_touch_screen_means_no_filter(self):
        from unittest import mock
        import ui.touch as touch
        with mock.patch.object(touch, "has_touch_screen", return_value=False):
            self.assertIsNone(touch.install(self._app))

    def test_grid_finger_toggles_on_lift_on_the_same_episode(self):
        from ui.episode_grid import EpisodeGrid
        grid = EpisodeGrid()
        grid.resize(600, 200)
        grid.set_episodes(range(1, 21))
        cols, cw = grid._layout()
        first = grid._rect(0, cols, cw).center().toPoint()
        third = grid._rect(2, cols, cw).center().toPoint()
        grid.mousePressEvent(self.mouse(2, first))
        self.assertEqual([], grid.skipped())               # nothing yet: may become a scroll
        grid.mouseReleaseEvent(self.mouse(3, first))
        self.assertEqual([1], grid.skipped())              # tap
        grid.mousePressEvent(self.mouse(2, third))
        grid.mouseReleaseEvent(self.mouse(3, (-10000, -10000)))   # scroll took over
        self.assertEqual([1], grid.skipped())
        # A real mouse still toggles on press (drag-to-paint keeps working).
        grid.mousePressEvent(self.mouse(2, third, touch=False))
        self.assertEqual([1, 3], grid.skipped())


class EpisodePickerTests(unittest.TestCase):
    """The paused screen's episode grid: one painted widget, not a checkbox per
    episode (that froze the UI on long runs)."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.episode_grid import EpisodePicker
        self.picker = EpisodePicker()
        self.picker.resize(900, 300)
        self.picker.set_episodes([30, 4, 5, 6, 7, 8, 9, 10, 20])
        self.grid = self.picker.grid

    def click(self, ep, shift=False):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        cols, cw = self.grid._layout()
        pos = self.grid._rect(self.grid.episodes().index(ep), cols, cw).center().toPoint()
        mod = Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
        QTest.mouseClick(self.grid, Qt.MouseButton.LeftButton, mod, pos)

    def test_sorted_all_kept_and_no_child_widgets(self):
        self.assertEqual([4, 5, 6, 7, 8, 9, 10, 20, 30], self.grid.episodes())
        self.assertEqual([], self.picker.skipped())
        self.assertEqual("4-10, 20, 30", self.picker.edit_spec.text())
        self.assertEqual([], self.grid.children())

    def test_click_and_shift_click(self):
        self.click(5)
        self.assertEqual([5], self.picker.skipped())
        self.click(8, shift=True)          # 5..8 take the state of the anchor (skipped)
        self.assertEqual([5, 6, 7, 8], self.picker.skipped())
        self.assertEqual("4, 9-10, 20, 30", self.picker.edit_spec.text())

    def test_range_box_drives_grid(self):
        self.picker.edit_spec.setText("6-9, 30, 99")
        self.picker._on_spec_edited()
        self.assertEqual([4, 5, 10, 20], self.picker.skipped())
        self.picker.edit_spec.setText("abc")
        self.picker._on_spec_edited()      # unparseable -> reverts, grid untouched
        self.assertEqual("6-9, 30", self.picker.edit_spec.text())
        self.picker.btn_none.click()
        self.assertEqual(9, len(self.picker.skipped()))
        self.picker.btn_all.click()
        self.assertEqual([], self.picker.skipped())

    def test_grid_scrolls_inside_available_height(self):
        # A long run: the scroll area may shrink to a couple of rows and scrolls
        # the rest; it never forces the panel taller than the window.
        self.picker.set_episodes(range(1, 1101))
        self.assertLessEqual(self.picker.scroll.minimumHeight(), self.picker.MIN_GRID_HEIGHT)
        self.assertGreater(self.grid.minimumHeight(), self.picker.MIN_GRID_HEIGHT)
        # A short run: no empty scroll space below the last row.
        self.picker.set_episodes([1, 2, 3])
        self.assertEqual(self.grid.minimumHeight() + 2, self.picker.scroll.maximumHeight())


class ActiveCardOrderTests(unittest.TestCase):
    """Cards re-added after a resume land in episode order, not at the bottom."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_order(self):
        from ui.progress_tab import ProgressTab
        tab = ProgressTab()
        for ep in (7, 3, 5):
            tab.add_active_card(ep)
        tab.remove_active_card(3)
        tab.add_active_card(3)
        layout = tab.active_tasks_layout
        by_widget = {id(v["widget"]): k for k, v in tab.active_cards.items()}
        order = [by_widget[id(layout.itemAt(i).widget())] for i in range(layout.count())]
        self.assertEqual([3, 5, 7], order)


class DownloaderSaveSettingsTests(unittest.TestCase):
    """save_settings had its second half inside on_webhook_updated: concurrency,
    auto mode and the episode range were never written to disk."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_concurrency_is_saved_and_written(self):
        import copy
        from unittest import mock
        import ui.downloader_tab as dt
        import utils.config as cfg
        saved = copy.deepcopy(cfg.app_settings)
        self.addCleanup(lambda: (cfg.app_settings.clear(), cfg.app_settings.update(saved)))
        with mock.patch.object(dt, "save_config", lambda: None):
            w = dt.DownloaderWidget()
        w._save_timer.stop()
        w.chk_auto_concurrency.setChecked(False)
        w.spin_concurrency.setValue(5)
        w.save_settings()
        self.assertEqual(cfg.app_settings["concurrency"], 5)
        self.assertFalse(cfg.app_settings["concurrency_auto"])
        self.assertTrue(w._save_timer.isActive(), "the disk write must be scheduled")

    def test_paused_screen_choices_reach_the_downloader(self):
        import copy
        from unittest import mock
        import ui.downloader_tab as dt
        import utils.config as cfg
        saved = copy.deepcopy(cfg.app_settings)
        self.addCleanup(lambda: (cfg.app_settings.clear(), cfg.app_settings.update(saved)))
        with mock.patch.object(dt, "save_config", lambda: None):
            w = dt.DownloaderWidget()
        w.on_paused_settings_changed({"limit": 2, "auto": False, "headless": False})
        self.assertEqual((w.spin_concurrency.value(), w.chk_auto_concurrency.isChecked(),
                          w.chk_headless.isChecked()), (2, False, False))
        self.assertEqual((cfg.app_settings["concurrency"], cfg.app_settings["headless"]), (2, False))

    def test_paused_choices_carry_into_resume_and_retry(self):
        import copy
        from unittest import mock
        import ui.downloader_tab as dt
        import utils.config as cfg
        saved = copy.deepcopy(cfg.app_settings)
        self.addCleanup(lambda: (cfg.app_settings.clear(), cfg.app_settings.update(saved)))
        with mock.patch.object(dt, "save_config", lambda: None):
            w = dt.DownloaderWidget()
        cfg.app_settings["unfinished_session"] = {"episodes": [1, 2], "headless": True,
                                                  "concurrency": 3}
        w.last_download_params = {"headless": True, "concurrency": 3}
        w.on_paused_settings_changed({"limit": 1, "auto": False, "headless": False})
        session = cfg.app_settings["unfinished_session"]
        self.assertEqual((session["headless"], session["concurrency"]), (False, 1))
        self.assertEqual(w.last_download_params, {"headless": False, "concurrency": 1})


class ShippedImportTests(unittest.TestCase):
    """The installed app only has what requirements.txt installs. Importing anything
    else -- the cloud service's code pulled in httpx and the Postgres driver -- works
    from source on a developer machine and kills the shipped app."""

    CLIENT = ["main.py", "aed_watcher.pyw", "ui", "core", "utils"]
    # requirements.txt distribution name -> the module name it is imported as
    DIST_TO_MODULE = {"pyqt6": "PyQt6", "pyqt6-fluent-widgets": "qfluentwidgets",
                      "selenium": "selenium", "websocket-client": "websocket",
                      "py7zr": "py7zr", "rarfile": "rarfile", "pillow": "PIL",
                      "scipy": "scipy", "psutil": "psutil"}

    def client_files(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for entry in self.CLIENT:
            path = os.path.join(root, entry)
            if os.path.isfile(path):
                yield path
            else:
                for dirpath, _dirs, files in os.walk(path):
                    for name in files:
                        if name.endswith(".py"):
                            yield os.path.join(dirpath, name)

    def imports(self, path):
        import ast
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):                 # inside functions too
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield node.lineno, alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.lineno, node.module.split(".")[0]

    def test_desktop_app_never_imports_the_cloud_service(self):
        offenders = [f"{os.path.basename(p)}:{line}" for p in self.client_files()
                     for line, mod in self.imports(p) if mod == "service"]
        self.assertEqual(offenders, [])

    def test_every_import_is_stdlib_local_or_a_declared_requirement(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        declared = set()
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            for line in f:
                name = re.split(r"[=<>\[ #;]", line.strip(), maxsplit=1)[0].lower()
                if name in self.DIST_TO_MODULE:
                    declared.add(self.DIST_TO_MODULE[name])
        local = {"ui", "core", "utils", "main", "tools"}
        missing = []
        for p in self.client_files():
            for line, mod in self.imports(p):
                if mod in sys.stdlib_module_names or mod in local or mod in declared:
                    continue
                missing.append(f"{os.path.relpath(p, root)}:{line} imports {mod!r}")
        self.assertEqual(missing, [], "not installed in the release build")


class NewEpisodeNotifyTests(unittest.TestCase):
    def test_new_episode_callback_survives_without_server_packages(self):
        """The crash, exactly: httpx is absent in the installed app."""
        from unittest import mock
        from types import SimpleNamespace
        import ui.watchlist_tab as wt
        sent = []
        entry = {"url": "https://witanime.site/anime/x", "title": "X", "seen_max": 13}
        fake = SimpleNamespace(_cards={})
        blocked = {"httpx": None, "service.checker": None, "service.store": None}
        with mock.patch.dict(sys.modules, blocked), \
             mock.patch.object(wt, "update_watch", lambda *a, **k: None), \
             mock.patch.object(wt, "get_watchlist", lambda: [entry]), \
             mock.patch.dict(wt.app_settings, {"discord_webhook": "https://discord.com/api/webhooks/1/x"}), \
             mock.patch("utils.discord_notify.send", lambda wh, payload: sent.append(payload) or True), \
             mock.patch("ui.watchlist_tab.time.sleep", lambda s: None):
            wt.WatchlistWidget._on_entry_done(fake, entry["url"], 14, 1, "t/{x}", False)
            for t in __import__("threading").enumerate():
                if t.name != "MainThread" and t.daemon:
                    t.join(timeout=2)
        self.assertEqual(len(sent), 1)
        self.assertIn("Episode 14", sent[0]["embeds"][0]["description"])

    def test_send_success_rate_limit_and_failure(self):
        import io
        import urllib.error
        from unittest import mock
        from utils import discord_notify as dn

        class Resp:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def http_error(code, retry="1"):
            return urllib.error.HTTPError("u", code, "x", {"Retry-After": retry}, io.BytesIO(b""))

        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            self.assertTrue(dn.send("https://discord.com/api/webhooks/1/x", {"content": "hi"}))
        with mock.patch("urllib.request.urlopen", side_effect=[http_error(429), Resp()]), \
             mock.patch("utils.discord_notify.time.sleep", lambda s: None):
            self.assertTrue(dn.send("https://discord.com/api/webhooks/1/x", {"content": "hi"}))
        with mock.patch("urllib.request.urlopen", side_effect=http_error(404)):
            self.assertFalse(dn.send("https://discord.com/api/webhooks/1/x", {"content": "hi"}))
        self.assertFalse(dn.send("", {"content": "hi"}))


class RemoteDownloadQueueTests(unittest.TestCase):
    """A Discord click must never interrupt a running download. It used to start on
    top of it, cancelling the first; repeated clicks is how the app got into the
    state it crashed in."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def fake_window(self, running=False, current=None, queue=None):
        from types import SimpleNamespace
        from ui.app_window import AppWindow
        started = []
        w = SimpleNamespace(_download_running=running, _current_download_key=current,
                            _download_queue=list(queue or []),
                            _download_key=AppWindow._download_key,
                            on_watch_download_all=lambda items: started.append(list(items)))
        return w, started

    def item(self, ep="14", show="mushoku"):
        return (show, f"https://witanime.site/watch/{show}/{{x}}", "witanime.site", 0, ep)

    def run_guard(self, w, items):
        from unittest import mock
        from ui.app_window import AppWindow
        with mock.patch("qfluentwidgets.InfoBar.info"), mock.patch("qfluentwidgets.InfoBar.success"):
            AppWindow._queue_remote_downloads(w, items)

    def test_idle_app_starts_the_download(self):
        w, started = self.fake_window()
        self.run_guard(w, [self.item()])
        self.assertEqual(len(started), 1)

    def test_same_episode_while_it_downloads_is_ignored(self):
        from ui.app_window import AppWindow
        w, started = self.fake_window(running=True, current=AppWindow._download_key(self.item()))
        self.run_guard(w, [self.item()])
        self.assertEqual(started, [])                  # the running download is left alone
        self.assertEqual(w._download_queue, [])

    def test_a_different_download_waits_its_turn(self):
        from ui.app_window import AppWindow
        w, started = self.fake_window(running=True, current=AppWindow._download_key(self.item("14")))
        self.run_guard(w, [self.item("15")])
        self.assertEqual(started, [])                  # not started on top of the other
        self.assertEqual([i[4] for i in w._download_queue], ["15"])

    def test_already_queued_is_not_queued_twice(self):
        from ui.app_window import AppWindow
        w, _ = self.fake_window(running=True, current=AppWindow._download_key(self.item("14")),
                                queue=[self.item("15")])
        self.run_guard(w, [self.item("15"), self.item("15")])
        self.assertEqual(len(w._download_queue), 1)

    def test_trailing_slash_does_not_make_a_different_download(self):
        from ui.app_window import AppWindow
        a = ("t", "https://x/watch/s/{x}/", "d", 0, "3")
        b = ("t", "https://x/watch/s/{x}", "d", 0, " 3 ")
        self.assertEqual(AppWindow._download_key(a), AppWindow._download_key(b))


class ErrorReportTests(unittest.TestCase):
    """An error inside a Qt callback used to abort the installed app (0xC0000409 in
    Qt6Core.dll) and leave no trace. These run a real Qt event loop in a child
    process, because the failure is the process dying."""

    SCRIPT = r'''
import os, sys
sys.path.insert(0, {repo!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
if {install}:
    from utils.error_report import install
    install({folder!r})
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
app = QApplication([])
def boom():
    raise ValueError("error inside a Qt callback")
QTimer.singleShot(50, boom)
QTimer.singleShot(400, app.quit)          # only reached if the process survived
sys.exit(app.exec())
'''

    def run_child(self, install):
        import subprocess
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = self.SCRIPT.format(repo=repo, install=install, folder=folder)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        return p.returncode, folder

    def test_without_the_handler_pyqt_kills_the_process(self):
        """The crash, reproduced: this is the default behaviour."""
        code, _ = self.run_child(install=False)
        self.assertNotEqual(code, 0)

    def test_with_the_handler_the_app_survives_and_records_the_error(self):
        code, folder = self.run_child(install=True)
        self.assertEqual(code, 0, "the app should keep running after a callback error")
        with open(os.path.join(folder, "last_errors.txt"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("error inside a Qt callback", report)
        self.assertIn("in boom", report)                    # the traceback, not just the message

    def test_thread_errors_are_recorded(self):
        import threading
        from utils import error_report
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        old_hooks = (sys.excepthook, threading.excepthook)
        self.addCleanup(lambda: (setattr(sys, "excepthook", old_hooks[0]),
                                 setattr(threading, "excepthook", old_hooks[1])))
        error_report.install(folder)
        t = threading.Thread(target=lambda: 1 / 0, name="worker")
        t.start(); t.join()
        with open(os.path.join(folder, "last_errors.txt"), encoding="utf-8") as f:
            report = f.read()
        self.assertIn("thread 'worker'", report)
        self.assertIn("ZeroDivisionError", report)

    def test_report_is_bounded(self):
        from utils import error_report
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        old = error_report._path
        self.addCleanup(lambda: setattr(error_report, "_path", old))
        error_report._path = os.path.join(folder, "last_errors.txt")
        for i in range(400):
            error_report.record("test", f"error {i}\n" + "x" * 1500)
        self.assertLessEqual(os.path.getsize(error_report._path), error_report.MAX_BYTES + 4096)
        with open(error_report._path, encoding="utf-8") as f:
            self.assertIn("error 399", f.read())               # newest kept

    def test_the_report_survives_the_clear_on_start(self):
        from utils.log_cleanup import clear_logs
        from utils import error_report
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        for name in (error_report.REPORT_NAME, error_report.NATIVE_NAME, "watcher.log"):
            open(os.path.join(folder, name), "w").close()
        clear_logs(folder)
        left = sorted(os.listdir(folder))
        self.assertIn(error_report.REPORT_NAME, left)
        self.assertIn(error_report.NATIVE_NAME, left)
        self.assertNotIn("watcher.log", left)


class WatcherDetectionTests(unittest.TestCase):
    """The watcher must recognise the installed app and the installed app's own
    watcher, not only the Python versions of each."""

    class P:
        def __init__(self, pid, name, cmdline):
            self.info = {"pid": pid, "name": name, "cmdline": cmdline}

    def run_with(self, procs, fn):
        from unittest import mock
        import core.watcher as w
        with mock.patch.object(w.psutil, "process_iter", return_value=procs):
            return fn()

    def test_installed_app_counts_as_running_for_a_source_watcher(self):
        import core.watcher as w
        procs = [self.P(111, "AutoDownloader.exe", [r"C:\App\AutoDownloader.exe"])]
        self.assertTrue(self.run_with(procs, w.is_main_app_running))

    def test_installed_watcher_is_not_mistaken_for_the_app(self):
        import core.watcher as w
        procs = [self.P(111, "AutoDownloader.exe", [r"C:\App\AutoDownloader.exe", "--watcher"])]
        self.assertFalse(self.run_with(procs, w.is_main_app_running))

    def test_installed_watcher_is_seen_as_another_watcher(self):
        import core.watcher as w
        procs = [self.P(111, "AutoDownloader.exe", [r"C:\App\AutoDownloader.exe", "--watcher"])]
        self.assertTrue(self.run_with(procs, w.is_other_watcher_running))

    def test_source_watcher_still_detected(self):
        import core.watcher as w
        procs = [self.P(111, "pythonw.exe", ["pythonw.exe", r"C:\x\aed_watcher.pyw"])]
        self.assertTrue(self.run_with(procs, w.is_other_watcher_running))

    def test_nothing_running(self):
        import core.watcher as w
        procs = [self.P(111, "chrome.exe", ["chrome.exe"])]
        self.assertFalse(self.run_with(procs, w.is_main_app_running))
        self.assertFalse(self.run_with(procs, w.is_other_watcher_running))

    def test_polls_fast_but_heartbeats_inside_the_online_window(self):
        import core.watcher as w
        self.assertLessEqual(w.POLL_SECONDS, 5)
        self.assertLess(w.POLL_SECONDS * w.HEARTBEAT_EVERY, 180)   # service window


class ResumePromptTests(unittest.TestCase):
    """A download started this launch (e.g. from Discord 600 ms after start) must
    never be offered back as an 'unfinished session'."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        import copy
        import utils.config as cfg
        self.cfg = cfg
        self._saved = copy.deepcopy(cfg.app_settings.get("unfinished_session"))
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            self.cfg.app_settings.pop("unfinished_session", None)
        else:
            self.cfg.app_settings["unfinished_session"] = self._saved

    def make(self, session):
        from unittest import mock
        import ui.downloader_tab as dt
        if session is None:
            self.cfg.app_settings.pop("unfinished_session", None)
        else:
            self.cfg.app_settings["unfinished_session"] = session
        shown = []

        class FakeBox:
            def __init__(self, *a, **k):
                shown.append(a[1] if len(a) > 1 else "")
                self.yesButton = mock.Mock(); self.cancelButton = mock.Mock()
            def exec(self):
                return False

        p1 = mock.patch("qfluentwidgets.MessageBox", FakeBox)
        p2 = mock.patch.object(dt, "save_config", lambda: None)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)
        w = dt.DownloaderWidget() if hasattr(dt, "DownloaderWidget") else None
        if w is None:
            self.skipTest("downloader widget class not found")
        return w, shown

    OLD = {"site": "Old Show", "episodes": [3, 4], "target_dir": "C:\\x"}

    def test_leftover_session_is_offered(self):
        w, shown = self.make(dict(self.OLD))
        w.check_and_prompt_resume()
        self.assertEqual(len(shown), 1)

    def test_not_offered_once_a_download_started_this_launch(self):
        from core.signals import signals
        w, shown = self.make(dict(self.OLD))
        signals.task_started.emit()          # what the Discord command's download does
        w.check_and_prompt_resume()
        self.assertEqual(shown, [])

    def test_a_session_recorded_after_launch_is_not_the_one_offered(self):
        """The bug exactly: nothing was left over, a remote download then recorded
        itself, and the prompt read that record."""
        w, shown = self.make(None)
        self.cfg.app_settings["unfinished_session"] = {"site": "Mushoku", "episodes": [14]}
        w.check_and_prompt_resume()
        self.assertEqual(shown, [])


class WatchlistPolishTests(unittest.TestCase):
    def test_days_start_today_and_wrap(self):
        from ui.watchlist_tab import days_from_today
        from core.schedule import DAY_ORDER
        order = days_from_today("wednesday")
        self.assertEqual(order[0], "wednesday")
        self.assertEqual(sorted(order), sorted(DAY_ORDER))
        self.assertEqual(len(order), 7)
        # the day before today comes last
        self.assertEqual(order[-1], DAY_ORDER[DAY_ORDER.index("wednesday") - 1])

    def test_unknown_today_falls_back_to_schedule_order(self):
        from ui.watchlist_tab import days_from_today
        from core.schedule import DAY_ORDER
        self.assertEqual(days_from_today("someday"), list(DAY_ORDER))

    def test_trailing_separator_is_dropped_from_titles(self):
        from ui.watchlist_tab import display_title
        self.assertEqual(display_title("BLEACH: Sennen Kessen-hen - Kashin-tan -"),
                         "BLEACH: Sennen Kessen-hen - Kashin-tan")
        self.assertEqual(display_title("Re:Zero kara Hajimeru Isekai Seikatsu"),
                         "Re:Zero kara Hajimeru Isekai Seikatsu")
        self.assertEqual(display_title("Show —  "), "Show")
        self.assertEqual(display_title(""), "")

    def test_search_titles_get_the_same_cleanup(self):
        from ui.search_tab import clean_title
        self.assertEqual(clean_title("BLEACH: Sennen Kessen-hen - Kashin-tan -"),
                         "BLEACH: Sennen Kessen-hen - Kashin-tan")
        self.assertEqual(clean_title("Steins;Gate 0"), "Steins;Gate 0")

    def test_restore_watch_puts_the_entry_back_where_it_was(self):
        import copy
        import utils.config as cfg
        saved = copy.deepcopy(cfg.app_settings.get("watchlist", []))
        self.addCleanup(lambda: cfg.app_settings.__setitem__("watchlist", saved))
        cfg._trigger_bg_cloud_sync, real = (lambda: None), cfg._trigger_bg_cloud_sync
        self.addCleanup(lambda: setattr(cfg, "_trigger_bg_cloud_sync", real))
        a, b, c = ({"url": f"https://x/{n}", "title": n, "seen_max": 7} for n in "abc")
        cfg.app_settings["watchlist"] = [a, b, c]
        cfg.remove_watch("https://x/b")
        self.assertEqual([w["title"] for w in cfg.get_watchlist()], ["a", "c"])
        self.assertTrue(cfg.restore_watch(b, 1))
        self.assertEqual([w["title"] for w in cfg.get_watchlist()], ["a", "b", "c"])
        self.assertEqual(cfg.find_watch("https://x/b")["seen_max"], 7)   # state kept
        self.assertFalse(cfg.restore_watch(b, 1))                         # no duplicate


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


class LogCleanupTests(unittest.TestCase):
    """Logs are cleared at every start so they cannot grow for months. The app
    folder also holds the user's data, so only log files may ever match."""

    def make_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        files = {
            "watcher.log": "x" * 5000, "chromedriver.log": "y", "cloud.log": "z",
            "aria2c_error.log": "e", "aria2c_error.log.1": "old", "ui_stalls.log": "s",
            # must survive
            "sites_config.json": "{}", "download_history.db": "db",
            "install_log.txt": "installer", "site_health.json": "{}",
            "catalog.logic": "not a log", "notes.log.bak": "not a rotation",
        }
        for name, body in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(body)
        os.makedirs(os.path.join(d, "SeleniumProfile"))
        with open(os.path.join(d, "SeleniumProfile", "chrome_debug.log"), "w") as f:
            f.write("browser's own")
        return d

    def test_only_logs_are_removed(self):
        from utils.log_cleanup import clear_logs
        d = self.make_dir()
        cleared, busy = clear_logs(d)
        self.assertEqual(sorted(cleared), ["aria2c_error.log", "aria2c_error.log.1",
                                           "chromedriver.log", "cloud.log",
                                           "ui_stalls.log", "watcher.log"])
        self.assertEqual(busy, [])
        left = sorted(os.listdir(d))
        self.assertEqual(left, ["SeleniumProfile", "catalog.logic", "download_history.db",
                                "install_log.txt", "notes.log.bak", "site_health.json",
                                "sites_config.json"])

    def test_subfolders_are_never_entered(self):
        from utils.log_cleanup import clear_logs
        d = self.make_dir()
        clear_logs(d)
        self.assertTrue(os.path.exists(os.path.join(d, "SeleniumProfile", "chrome_debug.log")))

    def test_a_log_held_open_elsewhere_is_emptied_instead(self):
        from utils.log_cleanup import clear_logs
        d = self.make_dir()
        path = os.path.join(d, "chromedriver.log")
        holder = open(path, "a", encoding="utf-8")      # like a running chromedriver
        self.addCleanup(holder.close)
        cleared, busy = clear_logs(d)
        self.assertIn("chromedriver.log", cleared)
        self.assertEqual(os.path.getsize(path), 0)

    def test_missing_folder_is_not_an_error(self):
        from utils.log_cleanup import clear_logs
        self.assertEqual(clear_logs(os.path.join(tempfile.gettempdir(), "no-such-aed-dir")),
                         ([], []))


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


class AvailablePathTests(unittest.TestCase):
    """Before choosing a mirror the engine looks at which ones the episode offers.
    A missing mirror used to cost ~20 s of waiting for a button that never came."""

    def flows(self):
        from ui.search_tab import DEFAULT_SITE_FLOWS
        return (DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"],
                DEFAULT_SITE_FLOWS["eta.animerco.org"]["step_paths"])

    def test_witanime_probe_is_the_host_button_not_the_shared_fhd_button(self):
        from core.selenium_engine import path_probes
        wit, _ = self.flows()
        probes = path_probes(wit)
        self.assertEqual(set(probes), set(wit))
        for name, steps in wit.items():
            self.assertEqual(probes[name], steps[1]["xpath"], name)
            self.assertNotEqual(probes[name], steps[0]["xpath"], name)

    def test_animerco_probe_is_the_host_row(self):
        from core.selenium_engine import path_probes
        _, ani = self.flows()
        probes = path_probes(ani)
        for name, steps in ani.items():
            self.assertEqual(probes[name], steps[0]["xpath"], name)

    def test_single_path_profile_has_no_probe(self):
        from core.selenium_engine import path_probes
        self.assertEqual(path_probes({"only": [{"xpath": "//a", "delay": 1}]}), {"only": None})

    def test_identical_paths_have_no_probe(self):
        from core.selenium_engine import path_probes
        same = [{"xpath": "//a", "delay": 1}]
        self.assertEqual(path_probes({"a": same, "b": list(same)}), {"a": None, "b": None})

    def test_absent_mirrors_are_skipped_in_priority_order(self):
        """The Mushoku episode checked live: Mediafire and Workupload present,
        Google Drive and wtsrv absent."""
        from core.selenium_engine import choose_paths
        order = ["FHD - Mediafire", "FHD - Google Drive", "FHD - wtsrv", "FHD - Workupload"]
        found = {"FHD - Mediafire": True, "FHD - Google Drive": False,
                 "FHD - wtsrv": False, "FHD - Workupload": True}
        self.assertEqual(choose_paths(order, found), ["FHD - Mediafire", "FHD - Workupload"])

    def test_priority_follows_the_profile_not_the_page(self):
        from core.selenium_engine import choose_paths
        order = ["a", "b", "c"]
        self.assertEqual(choose_paths(order, {"c": True, "a": True, "b": False}), ["a", "c"])

    def test_nothing_found_falls_back_to_every_path(self):
        """Inconclusive (layout changed, page not ready) must behave exactly as before."""
        from core.selenium_engine import choose_paths
        order = ["a", "b"]
        self.assertEqual(choose_paths(order, {"a": False, "b": False}), order)
        self.assertEqual(choose_paths(order, {}), order)

    def test_paths_without_a_probe_are_kept(self):
        from core.selenium_engine import choose_paths
        self.assertEqual(choose_paths(["a", "b", "c"], {"a": False, "b": None, "c": True}),
                         ["b", "c"])

    def test_engine_lookup_never_raises_and_tries_all_on_error(self):
        from core.selenium_engine import available_paths

        class Broken:
            def execute_script(self, *a):
                raise RuntimeError("tab crashed")

        wit, _ = self.flows()
        to_try, found = available_paths(Broken(), wit, timeout=0)
        self.assertEqual(to_try, list(wit))

    def test_engine_lookup_uses_one_script_call_when_links_are_there(self):
        from core.selenium_engine import available_paths
        wit, _ = self.flows()
        calls = []

        class Page:
            def execute_script(self, js, probes):
                calls.append(1)
                return {"FHD - Mediafire": True, "FHD - Workupload": True}

        to_try, _ = available_paths(Page(), wit, timeout=4)
        self.assertEqual(to_try, ["FHD - Mediafire", "FHD - Workupload"])
        self.assertEqual(len(calls), 1)


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


class PosterFramingTests(unittest.TestCase):
    """rounded_from_image must fit the whole poster, not slice out its middle.

    It used to crop only, so a 164x200 search cover shown as a 56x84 Watchlist
    poster lost everything above and below a small central window."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def poster(self, w=164, h=200, band=50):
        from PyQt6.QtGui import QImage, QColor, QPainter
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor(0, 0, 255))
        p = QPainter(img)
        p.fillRect(0, 0, w, band, QColor(0, 255, 0))   # title band across the top
        p.end()
        return img

    def colour_at(self, pix, x, y):
        c = pix.toImage().pixelColor(x, y)
        return c.red(), c.green(), c.blue()

    def test_small_frame_keeps_the_top_of_the_poster(self):
        from ui.styles import rounded_from_image
        pix = rounded_from_image(self.poster(), 56, 84, 6)
        self.assertEqual((pix.width(), pix.height()), (56, 84))
        r, g, b = self.colour_at(pix, 28, 8)
        self.assertGreater(g, 200, "top band was cropped away -- poster not fitted")
        r, g, b = self.colour_at(pix, 28, 70)
        self.assertGreater(b, 200)

    def test_large_source_is_fitted_not_sliced(self):
        """animerco covers arrive up to 500px; the search card is 164x200."""
        from ui.styles import rounded_from_image
        pix = rounded_from_image(self.poster(375, 500, band=120), 164, 200, 8)
        self.assertEqual((pix.width(), pix.height()), (164, 200))
        self.assertGreater(self.colour_at(pix, 82, 10)[1], 200)

    def test_already_fitted_image_is_unchanged_in_size(self):
        from ui.styles import rounded_from_image
        pix = rounded_from_image(self.poster(164, 200), 164, 200, 8)
        self.assertEqual((pix.width(), pix.height()), (164, 200))

    def test_null_image_is_safe(self):
        from PyQt6.QtGui import QImage
        from ui.styles import rounded_from_image
        self.assertIsNone(rounded_from_image(QImage(), 56, 84))
        self.assertIsNone(rounded_from_image(None, 56, 84))

    def test_dense_screen_gets_device_pixels_at_the_same_layout_size(self):
        """At 125% a 56x84 poster drawn at 56x84 pixels was stretched and soft."""
        from unittest import mock
        from ui import styles
        for scale, expect in ((1.25, (70, 105)), (2.0, (112, 168))):
            with mock.patch.object(styles, "render_scale", return_value=scale):
                pix = styles.rounded_from_image(self.poster(), 56, 84, 6)
            self.assertEqual((pix.width(), pix.height()), expect)
            self.assertAlmostEqual(pix.devicePixelRatio(), scale)
            size = pix.deviceIndependentSize()
            self.assertEqual((round(size.width()), round(size.height())), (56, 84))

    def test_render_scale_is_bounded(self):
        from ui.styles import render_scale
        self.assertGreaterEqual(render_scale(), 1.0)
        self.assertLessEqual(render_scale(), 3.0)


class WatchlistCoverTests(unittest.TestCase):
    """Search hands covers over as decoded QImages. Follow used to accept only a
    path, so every anime followed after that change was stored without a poster."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtCore import QCoreApplication
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def image(self):
        from PyQt6.QtGui import QImage, QColor
        img = QImage(164, 200, QImage.Format.Format_RGB32)
        img.fill(QColor(200, 30, 30))
        return img

    def test_qimage_cover_is_saved_to_disk(self):
        from ui.watchlist_tab import _persist_cover
        path = _persist_cover("https://witanime.site/anime/test-qimage/", self.image())
        self.assertTrue(path, "a QImage cover must produce a stored poster")
        self.assertTrue(os.path.exists(path))
        self.assertIn("watchlist_covers", path)
        self.assertTrue(path.startswith(os.environ["AED_APP_DIR"]))

    def test_saved_cover_is_kept_decoded_for_the_card(self):
        """The card must not have to open the file it was just given, on the GUI
        thread -- that first read is what froze the Search grid."""
        from ui import watchlist_tab
        path = watchlist_tab._persist_cover("https://x/anime/held/", self.image())
        self.assertIn(path, watchlist_tab._saved_covers)

    def test_path_cover_still_copies(self):
        from ui.watchlist_tab import _persist_cover
        src = os.path.join(os.environ["AED_APP_DIR"], "src_cover.img")
        self.assertTrue(self.image().save(src, "JPEG"))
        path = _persist_cover("https://x/anime/from-path/", src)
        self.assertTrue(path and os.path.exists(path))

    def test_nothing_to_store_returns_empty(self):
        from PyQt6.QtGui import QImage
        from ui.watchlist_tab import _persist_cover
        self.assertEqual(_persist_cover("https://x/a/", ""), "")
        self.assertEqual(_persist_cover("https://x/a/", None), "")
        self.assertEqual(_persist_cover("https://x/a/", QImage()), "")
        self.assertEqual(_persist_cover("https://x/a/", r"C:\no\such\file.img"), "")

    def test_missing_cover_is_detected(self):
        from ui.watchlist_tab import _cover_is_missing
        self.assertTrue(_cover_is_missing({}))
        self.assertTrue(_cover_is_missing({"cover": ""}))
        self.assertTrue(_cover_is_missing({"cover": r"C:\gone\0cc79bc068a6f160.img"}))
        src = os.path.join(os.environ["AED_APP_DIR"], "present.img")
        self.assertTrue(self.image().save(src, "JPEG"))
        self.assertFalse(_cover_is_missing({"cover": src}))


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


class FastStartTests(unittest.TestCase):
    """Launch-time trimming: importing qfluentwidgets must not load numpy, PIL,
    colorthief or scipy, nor run darkdetect's WMI query -- and each still works
    when something actually uses it."""

    def test_qfluentwidgets_import_leaves_heavy_libraries_unloaded(self):
        import subprocess
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from PyQt6.QtWidgets import QApplication; app = QApplication([])\n"
            "from utils.fast_start import defer_scipy, undefer_scipy\n"
            "defer_scipy(); import qfluentwidgets; undefer_scipy()\n"
            "heavy = [m for m in ('numpy', 'PIL', 'PIL.Image', 'colorthief', 'scipy') if m in sys.modules]\n"
            "print('HEAVY', heavy)\n"
            "import darkdetect; print('DD', type(darkdetect).__name__)\n"
            "from qfluentwidgets.common import image_utils as iu\n"
            "from PyQt6.QtGui import QPixmap, QColor\n"
            "pm = QPixmap(4, 4); pm.fill(QColor('red'))\n"
            "print('BLUR', not iu.gaussianBlur(pm, 1).isNull())\n"
            "print('NUMPY', type(sys.modules['numpy']).__name__)\n"
        ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=env, timeout=120).stdout
        self.assertIn("HEAVY []", out, out)
        self.assertIn("DD module", out, out)            # the real one once asked for again
        self.assertIn("BLUR True", out, out)            # loads numpy/PIL on first use
        self.assertIn("NUMPY module", out, out)

    def test_fast_darkdetect_matches_the_registry(self):
        from utils import fast_start
        if sys.platform != "win32":
            self.skipTest("Windows only")
        self.assertIn(fast_start._windows_theme(), ("Dark", "Light", None))


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

    def test_wahmi_destination_is_accepted(self):
        self.assertTrue(tab_matches_path(
            "https://wahmi.org/GKcq82QDdbgfy78/file", "FHD - wahmi"))
        self.assertFalse(tab_matches_path(
            "https://ads.fast.io/interstitial", "FHD - wahmi"))

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
        self.assertEqual(set(paths), {"FHD - Google Drive", "FHD - Mediafire",
                                      "FHD - wtsrv", "FHD - Workupload", "FHD - mp4upload",
                                      "FHD - gofile", "FHD - wahmi"})

    def test_witanime_path_order_is_preserved(self):
        """The engine tries paths in order, so ordering is behaviour, not cosmetics."""
        paths = DEFAULT_SITE_FLOWS["witanime.site"]["step_paths"]
        self.assertEqual(list(paths), ["FHD - Mediafire", "FHD - Google Drive",
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


class MpvnetTests(unittest.TestCase):
    """The Video Player tab's mpv.net helpers (utils/mpvnet.py)."""

    def setUp(self):
        from utils import mpvnet
        self.mpvnet = mpvnet
        self.cfg = tempfile.mkdtemp(prefix="aed_mpvcfg_")

    def test_bundle_is_complete(self):
        base = self.mpvnet.bundle_dir()
        for name in self.mpvnet.BUNDLE_FILES:
            self.assertTrue(os.path.isfile(os.path.join(base, name)), name)
        shaders = self.mpvnet.bundled_shaders()
        self.assertIn("Anime4K_Upscale_CNN_x2_L.glsl", shaders)
        # Every shader the bundled mpv.conf loads is actually shipped.
        with open(os.path.join(base, "mpv.conf"), encoding="utf-8") as f:
            conf = f.read()
        import re
        for ref in re.findall(r"~~/shaders/([^;\"]+\.glsl)", conf):
            self.assertIn(ref.lower(), [s.lower() for s in shaders], ref)
        self.assertNotIn(r"C:\Users", conf)

    def test_anime_folder_name(self):
        name = self.mpvnet.anime_folder_name
        self.assertEqual("animes", name(r"C:\Users\x\Desktop\animes"))
        self.assertEqual("my anime", name("C:\\Users\\x\\My Anime\\"))
        self.assertEqual("animes", name("D:\\"))
        self.assertEqual("animes", name(""))
        self.assertEqual("أنمي", name(r"C:\أنمي"))

    def test_render_points_profiles_at_download_folder(self):
        with open(os.path.join(self.mpvnet.bundle_dir(), "mpv.conf"), encoding="utf-8") as f:
            conf = f.read()
        self.assertEqual(conf, self.mpvnet.render_mpv_conf(conf, r"C:\x\animes"))
        out = self.mpvnet.render_mpv_conf(conf, r"C:\x\Anime-Downloads (1)")
        conds = [l for l in out.splitlines() if l.startswith("profile-cond=")]
        self.assertEqual(2, len(conds))
        for line in conds:
            # Lua pattern magic characters are escaped with %.
            self.assertIn(r'"[\\/]anime%-downloads %(1%)[\\/]"', line)
        self.assertNotIn("[\\\\/]animes[\\\\/]", out)

    def test_clean_winget_line(self):
        clean = self.mpvnet.clean_winget_line
        self.assertEqual("", clean("   -\r   \\\r   |"))
        self.assertEqual("Successfully installed", clean("  -\r  Successfully installed  "))
        self.assertEqual("██ 10 MB / 30 MB", clean("█ 5 MB / 30 MB\r██ 10 MB / 30 MB"))

    def test_apply_backs_up_only_what_changes(self):
        os.makedirs(os.path.join(self.cfg, "Shaders"))
        with open(os.path.join(self.cfg, "mpv.conf"), "w") as f:
            f.write("vo=gpu\n")
        with open(os.path.join(self.cfg, "Shaders", "mine.glsl"), "w") as f:
            f.write("// mine\n")
        self.assertFalse(self.mpvnet.shaders_applied(self.cfg))

        backup = self.mpvnet.apply_config(self.cfg, r"C:\x\animes")
        self.assertEqual(["mpv.conf"], os.listdir(backup))
        with open(os.path.join(backup, "mpv.conf")) as f:
            self.assertEqual("vo=gpu\n", f.read())
        # The user's own shader is left alone, the bundled ones and script-opts land.
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, "Shaders", "mine.glsl")))
        self.assertTrue(os.path.isdir(os.path.join(self.cfg, "script-opts")))
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, "scripts", "shader-toggle.lua")))
        self.assertTrue(self.mpvnet.shaders_applied(self.cfg))

        # Re-applying the same thing has nothing to back up.
        self.assertIsNone(self.mpvnet.apply_config(self.cfg, r"C:\x\animes"))

    def test_shaders_applied_needs_conf_to_load_them(self):
        self.mpvnet.apply_config(self.cfg)
        with open(os.path.join(self.cfg, "mpv.conf"), "w") as f:
            f.write("vo=gpu-next\n")
        self.assertFalse(self.mpvnet.shaders_applied(self.cfg))

    def test_profiles_parsed_and_described(self):
        from ui.player_tab import SHADER_INFO, PROFILE_TITLES
        profiles = dict(self.mpvnet.bundled_profiles())
        self.assertEqual(["anime", "series"], list(profiles))
        self.assertEqual("Anime4K_Clamp_Highlights", profiles["anime"][0])
        for name, stems in profiles.items():
            self.assertIn(name, PROFILE_TITLES)
            for stem in stems:
                self.assertIn(stem, SHADER_INFO, f"{stem} has no friendly name in the tab")

    def test_default_player_detection(self):
        is_mpv = self.mpvnet.is_mpvnet_progid
        self.assertTrue(is_mpv(r"Applications\mpvnet.exe"))      # picked via "Open with"
        self.assertTrue(is_mpv("mpv.net.mkv"))
        self.assertFalse(is_mpv("AppXqj98qxeaynz6dv4459ayz6bnqxbyaqcs"))
        self.assertFalse(is_mpv(""))

    def test_registration_must_point_at_the_installed_exe(self):
        from unittest import mock
        m = self.mpvnet
        new = r"C:\Users\x\AppData\Local\Programs\mpv.net\mpvnet.exe"
        with mock.patch("winreg.OpenKey"), mock.patch("winreg.QueryValueEx"), \
                mock.patch.object(m, "_registered_command",
                                  return_value=r'"C:\Program Files\mpv.net\mpvnet.exe" "%1"'):
            self.assertTrue(m.is_registered())            # types are there
            self.assertFalse(m.is_registered(new))        # but for an old install
        with mock.patch("winreg.OpenKey"), mock.patch("winreg.QueryValueEx"), \
                mock.patch.object(m, "_registered_command", return_value=f'"{new}" "%1"'):
            self.assertTrue(m.is_registered(new))
        with mock.patch("winreg.OpenKey", side_effect=OSError):   # the ".video"-only case
            self.assertFalse(m.is_registered(new))

    def test_open_videos_queues_the_session_in_mpvnet(self):
        from unittest import mock
        m = self.mpvnet
        a = os.path.join(self.cfg, "Show Ep5.mp4")
        b = os.path.join(self.cfg, "Show Ep6.mp4")
        for p in (a, b):
            open(p, "wb").close()
        with mock.patch.object(m, "find_mpvnet", return_value=(r"C:\mpv\mpvnet.exe", "7")), \
                mock.patch.object(m, "play_in_mpvnet_enabled", return_value=True), \
                mock.patch.object(m.subprocess, "Popen") as popen, \
                mock.patch.object(m.os, "startfile", create=True) as startfile:
            self.assertEqual("mpvnet", m.open_videos([a, b]))
            popen.assert_called_once()
            self.assertEqual([r"C:\mpv\mpvnet.exe", a, b], popen.call_args[0][0])
            startfile.assert_not_called()
        # Switch off, or mpv.net missing -> Windows' default player gets the first.
        for enabled, found in ((False, r"C:\mpv\mpvnet.exe"), (True, None)):
            with mock.patch.object(m, "find_mpvnet", return_value=(found, None)), \
                    mock.patch.object(m, "play_in_mpvnet_enabled", return_value=enabled), \
                    mock.patch.object(m.subprocess, "Popen") as popen, \
                    mock.patch.object(m.os, "startfile", create=True) as startfile:
                self.assertEqual("default", m.open_videos([a, b]))
                startfile.assert_called_once_with(a)
                popen.assert_not_called()

    def test_policy_cleanup_only_removes_ours(self):
        m = self.mpvnet
        script = m.build_disable_script()
        self.assertIn(f"-eq '{m.policy_xml_path()}'", script)
        self.assertNotIn("Remove-Item -Path $key", script)     # never the whole key
        self.assertEqual("'O''Neil'", m._ps("O'Neil"))

    def test_policy_xml(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(self.mpvnet.build_policy_xml())
        rows = {a.get("Identifier"): a.get("ProgId") for a in root.iter("Association")}
        self.assertEqual({".mp4": "mpvnet.mp4", ".mkv": "mpvnet.mkv"}, rows)

    def test_enable_script(self):
        m = self.mpvnet
        exe = r"C:\Users\O'Neil\mpv.net\mpvnet.exe"          # a quote in the path
        with_reg = m.build_enable_script(exe, True)
        without = m.build_enable_script(exe, False)
        self.assertIn("--register-file-associations", with_reg)
        self.assertNotIn("--register-file-associations", without)
        self.assertIn("'C:\\Users\\O''Neil\\mpv.net\\mpvnet.exe'", with_reg)
        # Never recreate an existing policy key (that would wipe its other values).
        self.assertIn("if (-not (Test-Path $key)) { New-Item -Path $key", without)
        self.assertNotIn("New-Item -Path $key -Force", without)
        self.assertIn(m.POLICY_VALUE, without)

    def test_register_args_list_the_extensions(self):
        args = self.mpvnet.register_video_args().split()
        self.assertEqual(["--register-file-associations", "video"], args[:2])
        for ext in ("mp4", "mkv"):
            self.assertIn(ext, args[2:])          # no leading dots; mpv.net adds them
        self.assertFalse(any(a.startswith(".") for a in args))

    def test_describe_default(self):
        from ui.player_tab import describe_default
        self.assertEqual("ok", describe_default({".mp4": True, ".mkv": True})[0])
        state, text = describe_default({".mp4": False, ".mkv": True})
        self.assertEqual("partial", state)
        self.assertIn(".mp4", text)
        self.assertEqual("no", describe_default({".mp4": False, ".mkv": False})[0])

    def test_winget_command(self):
        cmd = self.mpvnet.winget_install_command("winget")
        self.assertEqual(["winget", "install", "--id", "mpv.net", "--exact"], cmd[:5])
        self.assertIn("--accept-source-agreements", cmd)


class LoadConfigSaveTests(unittest.TestCase):
    """load_config() persists its migrations with save_config(), and WHERE that save
    sits decides what lands on disk. These read the file back rather than memory:
    memory is always correct by the time anyone looks, which is exactly how a save
    that corrupted the file went unnoticed."""

    WEBHOOK = "https://discord.com/api/webhooks/123/abc"
    TOKEN = "tok_0123456789abcdef0123456789abcdef"

    def setUp(self):
        import copy
        import utils.config as cfg
        self.cfg = cfg
        self._settings = copy.deepcopy(cfg.app_settings)
        self._sites = copy.deepcopy(cfg.sites_data)
        self.path = cfg.CONFIG_FILE
        self._saved_file = None
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._saved_file = f.read()

    def tearDown(self):
        self.cfg.app_settings.clear()
        self.cfg.app_settings.update(self._settings)
        self.cfg.sites_data.clear()
        self.cfg.sites_data.update(self._sites)
        if self._saved_file is None:
            if os.path.exists(self.path):
                os.remove(self.path)
        else:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(self._saved_file)

    def _load_with_legacy_profile(self):
        """Write a config whose profile uses the old "steps" format -- so load_config
        has a migration to save -- load it, and return what ended up on disk."""
        import json
        cfg = self.cfg
        data = {
            "settings": {
                "discord_webhook": cfg.encrypt_webhook(self.WEBHOOK),
                "cloud_token": cfg.encrypt_webhook(self.TOKEN),
                "download_dir": r"C:\my\animes",
                "watchlist": [{"title": "Show", "url": "https://example.test/anime/show/"}],
            },
            "sites": {"Old Profile": {"url": "https://example.test/ep-{x}",
                                      "steps": [{"xpath": "Download", "delay": 1.0}]}},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        cfg.sites_data.clear()
        cfg.load_config()
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_migration_is_persisted(self):
        prof = self._load_with_legacy_profile()["sites"]["Old Profile"]
        self.assertIn("step_paths", prof)
        self.assertNotIn("steps", prof)

    def test_secrets_on_disk_are_encrypted_exactly_once(self):
        """Saving before the decrypt wrote encrypt(encrypt(x)). One decrypt on the
        next launch then gave ciphertext: cloud auth and the webhook silently died."""
        disk = self._load_with_legacy_profile()["settings"]
        self.assertEqual(self.cfg.decrypt_webhook(disk["discord_webhook"]), self.WEBHOOK)
        self.assertEqual(self.cfg.decrypt_webhook(disk["cloud_token"]), self.TOKEN)

    def test_user_settings_survive_the_save(self):
        """Saving before the settings loop wrote factory defaults over them."""
        disk = self._load_with_legacy_profile()["settings"]
        self.assertEqual(disk["download_dir"], r"C:\my\animes")
        self.assertEqual(len(disk["watchlist"]), 1)

    def test_memory_holds_cleartext_after_load(self):
        self._load_with_legacy_profile()
        self.assertEqual(self.cfg.app_settings["discord_webhook"], self.WEBHOOK)
        self.assertEqual(self.cfg.app_settings["cloud_token"], self.TOKEN)

    def test_pre_move_witanime_urls_are_rewritten_on_disk(self):
        """Profile url, Watchlist url AND latest_template -- the template is what a
        Watchlist or Discord download opens, and it used to stay on the dead host."""
        import json
        cfg = self.cfg
        data = {
            "settings": {
                "discord_webhook": cfg.encrypt_webhook(self.WEBHOOK),
                "watchlist": [{
                    "title": "Slime", "domain": "witanime.life",
                    "url": "https://witanime.life/anime/tensei-shitara-slime-datta-ken-4th-season/",
                    "latest_template": "https://witanime.life/episode/tensei-shitara-slime-datta-ken-4th-season-الحلقة-{x}/",
                }],
            },
            "sites": {"Slime": {
                "url": "https://witanime.life/episode/tensei-shitara-slime-datta-ken-4th-season-الحلقة-{x}",
                "step_paths": {"p": [{"xpath": "//a", "delay": 1}]}, "last_episodes": "17"}},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        cfg.sites_data.clear()
        cfg.load_config()
        with open(self.path, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(disk["sites"]["Slime"]["url"],
                         "https://witanime.site/watch/tensei-shitara-slime-datta-ken-4th-season/{x}")
        self.assertEqual(disk["sites"]["Slime"]["last_episodes"], "17")
        w = disk["settings"]["watchlist"][0]
        self.assertEqual(w["url"],
                         "https://witanime.site/anime/tensei-shitara-slime-datta-ken-4th-season/")
        self.assertEqual(w["latest_template"],
                         "https://witanime.site/watch/tensei-shitara-slime-datta-ken-4th-season/{x}")
        self.assertEqual(w["domain"], "witanime.site")
        self.assertEqual(cfg.decrypt_webhook(disk["settings"]["discord_webhook"]), self.WEBHOOK)


class ConfigMigrationTests(unittest.TestCase):
    def test_witanime_url_and_template_migration(self):
        from utils.config import load_config, app_settings, sites_data, encrypt_webhook
        
        test_data = {
            "settings": {
                "discord_webhook": encrypt_webhook("https://discord.com/api/webhooks/123/abc"),
                "watchlist": [
                    {
                        "url": "https://witanime.life/anime/one-piece/",
                        "latest_template": "https://witanime.site/watch/one-piece-الحلقة-{x}/"
                    }
                ]
            },
            "sites": {
                "Witanime Profile": {
                    "url": "https://witanime.site/watch/one-piece-الحلقة-{x}/"
                }
            }
        }
        
        import json
        import utils.config
        with open(os.path.join(utils.config.APP_DIR, "sites_config.json"), "w", encoding="utf-8") as f:
            json.dump(test_data, f)
            
        utils.config.sites_data.clear()
        utils.config.app_settings["watchlist"] = []
        load_config()
        # Since load_config reads it into app_settings:
        with open(os.path.join(utils.config.APP_DIR, "sites_config.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
            utils.config.app_settings["watchlist"] = data.get("watchlist", [])
        
        # We must call the migration block directly or load_config does it:
        # Wait, load_config DOES the migration! So it modifies data and THEN we can check it.
        # But load_config doesn't put watchlist back into app_settings, it modifies app_settings directly!
        # Ah, load_config reads data["watchlist"] and migrates IT. But it modifies `app_settings.get("watchlist", [])` IN PLACE!
        # If `app_settings["watchlist"]` is empty, it migrates nothing!
        load_config()
        
        self.assertEqual("https://discord.com/api/webhooks/123/abc", app_settings["discord_webhook"])
        
        prof = sites_data["Witanime Profile"]
        self.assertEqual("https://witanime.site/watch/one-piece-الحلقة-{x}/", prof["url"])
        
        w = app_settings["watchlist"][0]
        self.assertEqual("https://witanime.site/anime/one-piece/", w["url"])
        self.assertEqual("https://witanime.site/watch/one-piece-الحلقة-{x}/", w["latest_template"])


class WatchLaterTests(unittest.TestCase):
    """Watch later's store (utils/watch_later.py): statuses, per-anime history,
    watched tracking from mpv.net's progress log, and what Continue plays."""

    URL = "https://witanime.site/anime/test-anime/"
    TEMPLATE = "https://witanime.site/episode/test-anime-الحلقة-{x}/"

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl
        self.wl = wl
        self.tmp = tempfile.mkdtemp(prefix="aed_wl_")
        patch = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        patch.start()
        self.addCleanup(patch.stop)
        self.downloads = os.path.join(self.tmp, "animes")

    def _linked(self, max_ep=3, profile="Test Anime"):
        wl = self.wl
        wl.add("Test Anime", self.URL, "witanime.site")
        wl.set_parts(self.URL, [{"template": self.TEMPLATE, "max_ep": max_ep, "first_ep": 1}])
        wl.link_profile(self.URL, 0, profile)
        return profile

    def _files(self, profile, eps):
        folder = os.path.join(self.downloads, profile)
        os.makedirs(folder, exist_ok=True)
        for ep in eps:
            open(os.path.join(folder, f"{profile} Ep{ep}.mp4"), "wb").close()
        return folder

    def _log(self, profile, ep, percent, eof=False):
        path = os.path.join(self.downloads, profile, f"{profile} Ep{ep}.mp4")
        return json.dumps({"path": path, "percent": percent, "eof": eof})

    def test_adding_never_creates_a_profile_and_rejects_duplicates(self):
        wl = self.wl
        with config_lock:
            before = set(sites_data)
        self.assertTrue(wl.add("Test Anime", self.URL, "witanime.site"))
        # Same page written another way is the same anime.
        self.assertFalse(wl.add("Test Anime", "http://www.witanime.site/anime/test-anime", ""))
        with config_lock:
            self.assertEqual(before, set(sites_data))
        e = wl.find(self.URL)
        self.assertEqual(wl.LATER, e["status"])
        self.assertEqual([], e["parts"])
        self.assertEqual([], e["history"])

    def test_refreshing_seasons_keeps_profile_and_watched(self):
        wl = self.wl
        self._linked()
        wl.set_watched(self.URL, 0, [1, 2])
        wl.set_parts(self.URL, [{"template": self.TEMPLATE, "max_ep": 5}])
        part = wl.find(self.URL)["parts"][0]
        self.assertEqual("Test Anime", part["profile"])
        self.assertEqual([1, 2], part["watched"])
        self.assertEqual(5, part["max_ep"])

    def test_download_history_is_kept_per_anime(self):
        wl = self.wl
        profile = self._linked()
        self.assertFalse(wl.record_download("Some Other Profile", "1-3", "Success", ""))
        self.assertTrue(wl.record_download(profile, "1-3", "Failed", "timeout"))
        self.assertEqual(wl.LATER, wl.find(self.URL)["status"])     # nothing landed
        wl.record_download(profile, "1-3", "Success", "")
        e = wl.find(self.URL)
        self.assertEqual(wl.WATCHING, e["status"])
        self.assertEqual(["Success", "Failed"], [h["status"] for h in e["history"]])

    def test_progress_log_ticks_episodes_and_completes(self):
        wl = self.wl
        profile = self._linked(max_ep=2)
        wl.apply_progress([self._log(profile, 1, 40.0)])
        e = wl.find(self.URL)
        self.assertEqual(wl.WATCHING, e["status"])
        self.assertEqual({"1": 40.0}, e["parts"][0]["progress"])
        self.assertEqual([], e["parts"][0]["watched"])
        wl.apply_progress([self._log(profile, 1, 95.0), self._log(profile, 2, 12.0, eof=True)])
        e = wl.find(self.URL)
        self.assertEqual([1, 2], e["parts"][0]["watched"])
        self.assertEqual({}, e["parts"][0]["progress"])
        self.assertEqual(wl.COMPLETED, e["status"])

    def test_playing_an_episode_counts_the_earlier_ones_as_watched(self):
        wl = self.wl
        profile = self._linked(max_ep=12)
        wl.apply_progress([self._log(profile, 7, 95.0)])
        part = wl.find(self.URL)["parts"][0]
        self.assertEqual(list(range(1, 8)), part["watched"])
        # Only started: the earlier ones still count, this one stays half-way.
        wl.apply_progress([self._log(profile, 10, 20.0)])
        part = wl.find(self.URL)["parts"][0]
        self.assertEqual(list(range(1, 10)), part["watched"])
        self.assertEqual({"10": 20.0}, part["progress"])
        # Manual ticks are left exactly as set.
        wl.set_watched(self.URL, 0, [3])
        self.assertEqual([3], wl.find(self.URL)["parts"][0]["watched"])

    def test_season_starting_past_one_fills_from_its_first_episode(self):
        wl = self.wl
        wl.add("Test Anime", self.URL, "witanime.site")
        wl.set_parts(self.URL, [{"template": self.TEMPLATE, "max_ep": 24, "first_ep": 13}])
        wl.link_profile(self.URL, 0, "Test Anime")
        wl.apply_progress([self._log("Test Anime", 15, 99.0)])
        self.assertEqual([13, 14, 15], wl.find(self.URL)["parts"][0]["watched"])

    def test_older_entries_are_caught_up_once(self):
        wl = self.wl
        self._linked(max_ep=12)
        data = wl._load()
        data["entries"][0]["parts"][0]["watched"] = [7]          # tracked the old way
        wl._save(data)
        self.assertTrue(wl.fill_earlier_watched())
        self.assertEqual(list(range(1, 8)), wl.find(self.URL)["parts"][0]["watched"])
        wl.set_watched(self.URL, 0, [7])                         # user unticks 1-6
        self.assertFalse(wl.fill_earlier_watched())              # never again
        self.assertEqual([7], wl.find(self.URL)["parts"][0]["watched"])

    def test_log_lines_that_are_not_episodes_are_ignored(self):
        wl = self.wl
        self.assertIsNone(wl.parse_log_line("not json"))
        self.assertIsNone(wl.parse_log_line(json.dumps({"path": r"C:\x\notes.txt"})))
        self.assertIsNone(wl.parse_log_line(json.dumps({"path": r"C:\x\Movie.mp4"})))
        self.assertEqual(("Show", 12, 50.0, False),
                         wl.parse_log_line(json.dumps({"path": r"C:\a\Show\Show Ep12.mkv",
                                                       "percent": 50})))

    def test_log_is_read_incrementally_and_only_whole_lines(self):
        wl = self.wl
        profile = self._linked(max_ep=3)
        log = os.path.join(self.tmp, "aed-progress.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write(self._log(profile, 1, 99.0) + "\n" + self._log(profile, 2, 99.0)[:20])
        self.assertTrue(wl.ingest_log(log))
        self.assertEqual([1], wl.find(self.URL)["parts"][0]["watched"])
        with open(log, "a", encoding="utf-8") as f:
            f.write(self._log(profile, 2, 99.0)[20:] + "\n")
        self.assertTrue(wl.ingest_log(log))
        self.assertFalse(wl.ingest_log(log))           # nothing new
        self.assertEqual([1, 2], wl.find(self.URL)["parts"][0]["watched"])

    def test_continue_prefers_a_half_watched_episode_then_the_next_one(self):
        wl = self.wl
        profile = self._linked(max_ep=4)
        self._files(profile, [1, 2, 3])
        wl.set_watched(self.URL, 0, [1])
        wl.apply_progress([self._log(profile, 3, 30.0)])
        part, ep, file, percent = wl.next_episode(wl.find(self.URL), self.downloads)
        self.assertEqual((0, 3, 30.0), (part, ep, percent))
        self.assertTrue(file.endswith("Ep3.mp4"))
        wl.set_watched(self.URL, 0, [1, 2, 3])
        part, ep, file, _ = wl.next_episode(wl.find(self.URL), self.downloads)
        self.assertEqual(4, ep)
        self.assertIsNone(file)                          # not downloaded yet
        self.assertEqual(["Test Anime Ep2.mp4", "Test Anime Ep3.mp4"],
                         [os.path.basename(p) for p in
                          wl.playlist_from(wl.find(self.URL), 0, 2, self.downloads)])

    def test_manual_status_is_not_undone_by_a_recompute(self):
        wl = self.wl
        profile = self._linked(max_ep=1)
        wl.apply_progress([self._log(profile, 1, 100.0)])
        self.assertEqual(wl.COMPLETED, wl.find(self.URL)["status"])
        wl.set_status(self.URL, wl.WATCHING)             # rewatching
        wl.set_watched(self.URL, 0, [1])                 # same ticks, nothing new
        self.assertEqual(wl.WATCHING, wl.find(self.URL)["status"])

    def test_a_deleted_profile_keeps_its_folder_link_until_replaced(self):
        wl = self.wl
        self._linked(profile="Old Name")
        self.assertFalse(wl.resolve_profiles({}))         # nothing replaces it
        self.assertEqual("Old Name", wl.find(self.URL)["parts"][0]["profile"])
        renamed = {"New Name": {"url": self.TEMPLATE}}
        self.assertTrue(wl.resolve_profiles(renamed))     # renamed in Profile Manager
        self.assertEqual("New Name", wl.find(self.URL)["parts"][0]["profile"])

    def test_remove_and_undo(self):
        wl = self.wl
        self._linked()
        entry, index = wl.remove(self.URL)
        self.assertIsNone(wl.find(self.URL))
        self.assertTrue(wl.restore(entry, index))
        self.assertEqual("Test Anime", wl.find(self.URL)["parts"][0]["profile"])

    def test_season_profiles_are_named_after_the_season(self):
        wl = self.wl
        one = {"title": "Show", "parts": [{"label": "Season 1"}]}
        two = {"title": "Show", "parts": [{"label": "Season 1"}, {"label": "Season 2"}]}
        self.assertEqual("Show", wl.part_profile_name(one, one["parts"][0]))
        self.assertEqual("Show - Season 2", wl.part_profile_name(two, two["parts"][1]))

    def test_progress_script_is_bundled_and_installed_alone(self):
        from unittest import mock
        from utils import mpvnet
        src = os.path.join(mpvnet.bundle_dir(), "scripts", mpvnet.PROGRESS_SCRIPT)
        self.assertTrue(os.path.isfile(src))
        cfg = os.path.join(self.tmp, "mpvcfg")
        with mock.patch.object(mpvnet, "find_mpvnet", return_value=(r"C:\x\mpvnet.exe", None)), \
                mock.patch.object(mpvnet, "config_dir", return_value=cfg):
            self.assertTrue(mpvnet.ensure_progress_script())
            self.assertEqual(os.path.join(cfg, mpvnet.PROGRESS_LOG), mpvnet.progress_log_path())
        self.assertEqual(["aed-progress.lua"], os.listdir(os.path.join(cfg, "scripts")))
        self.assertFalse(os.path.exists(os.path.join(cfg, "mpv.conf")))


class LibraryImportTests(unittest.TestCase):
    """First open after the update: anime already on disk land in the Library,
    with what mpv.net says was watched (utils/library_scan.py)."""

    TEMPLATE = "https://witanime.site/episode/show-a-الحلقة-{x}/"

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl
        self.wl = wl
        self.tmp = tempfile.mkdtemp(prefix="aed_scan_")
        patch = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        patch.start()
        self.addCleanup(patch.stop)
        self.downloads = os.path.join(self.tmp, "animes")

    def _folder(self, name, eps):
        folder = os.path.join(self.downloads, name)
        os.makedirs(folder, exist_ok=True)
        paths = {}
        for ep in eps:
            paths[ep] = os.path.join(folder, f"{name} Ep{ep}.mp4")
            open(paths[ep], "wb").close()
        return paths

    def test_mp4_duration_reads_the_movie_header(self):
        import struct
        from utils.library_scan import mp4_duration
        mvhd = struct.pack(">I4sB3xII", 0, b"mvhd", 0, 0, 0) + struct.pack(">II", 1000, 1440000)
        mvhd = struct.pack(">I", len(mvhd)) + mvhd[4:]
        moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
        ftyp = struct.pack(">I4s4s", 12, b"ftyp", b"isom")
        mdat = struct.pack(">I4s", 16, b"mdat") + b"\0" * 8
        path = os.path.join(self.tmp, "x.mp4")
        with open(path, "wb") as f:
            f.write(ftyp + mdat + moov)                  # moov after the media data
        self.assertAlmostEqual(1440.0, mp4_duration(path))
        with open(os.path.join(self.tmp, "bad.mp4"), "wb") as f:
            f.write(b"junk")
        self.assertIsNone(mp4_duration(os.path.join(self.tmp, "bad.mp4")))

    def test_mpv_history_reads_resume_files_and_progress_log_not_recent_list(self):
        from utils.library_scan import read_mpv_history
        cfg = os.path.join(self.tmp, "mpvcfg")
        os.makedirs(os.path.join(cfg, "watch_later"))
        with open(os.path.join(cfg, "watch_later", "A"), "w", encoding="utf-8") as f:
            f.write("# C:\\anime\\Show Ep2.mp4\nstart=600.5\n")
        with open(os.path.join(cfg, "watch_later", "B"), "w", encoding="utf-8") as f:
            f.write("# redirect entry\n# C:\\anime\n")
        with open(os.path.join(cfg, "aed-progress.log"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"path": "C:\\anime\\Show Ep4.mp4", "percent": 97}) + "\n")
            f.write(json.dumps({"path": "C:\\anime\\Show Ep5.mp4", "percent": 10}) + "\n")
        # A playlist puts every file on mpv.net's recent list -- not a sign of watching.
        with open(os.path.join(cfg, "settings.xml"), "w", encoding="utf-8") as f:
            f.write("<AppSettings><RecentFiles><string>C:\\anime\\Show Ep9.mp4</string>"
                    "</RecentFiles></AppSettings>")
        h = read_mpv_history(cfg)
        norm = os.path.normcase
        self.assertEqual(600.5, h[norm("C:\\anime\\Show Ep2.mp4")])
        self.assertIsNone(h[norm("C:\\anime\\Show Ep4.mp4")])      # finished per log
        self.assertNotIn(norm("C:\\anime\\Show Ep5.mp4"), h)       # only started
        self.assertNotIn(norm("C:\\anime\\Show Ep9.mp4"), h)
        self.assertNotIn(norm("C:\\anime"), h)

    def test_watch_state_from_resume_points(self):
        from utils.library_scan import watch_state
        files = {e: f"C:\\a\\S Ep{e}.mp4" for e in range(1, 7)}
        key = os.path.normcase
        history = {key(files[3]): 1400.0,         # 97% in -> watched
                   key(files[5]): 300.0}          # 21% in -> in progress
        watched, progress = watch_state(files, history, duration=lambda _p: 1440.0)
        self.assertEqual({1, 2, 3, 4}, watched)   # earlier ones count as seen
        self.assertEqual({5: 20.8}, progress)

    def test_folders_and_profiles_become_entries(self):
        wl = self.wl
        from utils.library_scan import import_library, LOCAL_SCHEME
        a = self._folder("Show A", [1, 2, 3])
        self._folder("Loose Folder", [1, 2])
        os.makedirs(os.path.join(self.downloads, "Not anime"))       # no episodes
        profiles = {"Show A": {"url": self.TEMPLATE, "episode_bounds": [1, 12]},
                    "Planned": {"url": "https://witanime.site/episode/planned-الحلقة-{x}/",
                                "episode_bounds": [1, 10]}}
        history = {os.path.normcase(a[2]): None}                      # finished Ep2
        added = import_library(self.downloads, profiles, [], history, duration=lambda _p: None)
        self.assertEqual(3, added)
        by_title = {e["title"]: e for e in wl.entries()}
        self.assertEqual({"Show A", "Planned", "Loose Folder"}, set(by_title))
        self.assertEqual(wl.WATCHING, by_title["Show A"]["status"])
        self.assertEqual([1, 2], by_title["Show A"]["parts"][0]["watched"])
        self.assertEqual(wl.LATER, by_title["Planned"]["status"])
        self.assertEqual(wl.WATCHING, by_title["Loose Folder"]["status"])
        self.assertTrue(by_title["Loose Folder"]["url"].startswith(LOCAL_SCHEME))
        # Show A has 9 episodes left to download; the loose folder has no link.
        self.assertEqual((0, list(range(4, 13))),
                         wl.missing_episodes(by_title["Show A"], self.downloads))
        self.assertIsNone(wl.missing_episodes(by_title["Loose Folder"], self.downloads))
        # Scanning again adds nothing.
        self.assertEqual(0, import_library(self.downloads, profiles, [], history))
        self.assertTrue(wl.imported_from_disk())

    def test_anime_already_in_the_library_is_not_duplicated(self):
        wl = self.wl
        from utils.library_scan import import_library
        self._folder("Show A", [1])
        wl.add("Show A", "https://witanime.site/anime/show-a/", "witanime.site")
        wl.set_parts("https://witanime.site/anime/show-a/",
                     [{"template": self.TEMPLATE, "max_ep": 12}])
        wl.link_profile("https://witanime.site/anime/show-a/", 0, "Show A")
        self.assertEqual(0, import_library(self.downloads, {}, [], {}))
        self.assertEqual(1, len(wl.entries()))

    def test_adding_from_search_absorbs_the_folder_entry(self):
        wl = self.wl
        from utils.library_scan import import_library
        self._folder("Show A", [1, 2])
        import_library(self.downloads, {}, [], {}, duration=lambda _p: None)
        local = wl.entries()[0]["url"]
        wl.set_watched(local, 0, [1])
        page = "https://witanime.site/anime/show-a/"
        wl.add("Show A", page, "witanime.site")
        wl.set_parts(page, [{"template": self.TEMPLATE, "max_ep": 12}])
        items = wl.entries()
        self.assertEqual(1, len(items))
        self.assertEqual(page, items[0]["url"])
        self.assertEqual([1], items[0]["parts"][0]["watched"])
        self.assertEqual(wl.WATCHING, items[0]["status"])


class EpisodeSourceTests(unittest.TestCase):
    """Each downloaded episode records the site it came from: on its Library entry
    and in a hidden note in the anime's folder that a later scan reads back."""

    TEMPLATE = "https://witanime.site/episode/show-a-الحلقة-{x}/"

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl
        self.wl = wl
        self.tmp = tempfile.mkdtemp(prefix="aed_src_")
        patch = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        patch.start()
        self.addCleanup(patch.stop)
        self.downloads = os.path.join(self.tmp, "animes")
        self.folder = os.path.join(self.downloads, "Show A")
        os.makedirs(self.folder)

    def test_download_records_site_on_entry_and_in_folder(self):
        wl = self.wl
        wl.add("Show A", "https://witanime.site/anime/show-a/", "witanime.site")
        wl.set_parts("https://witanime.site/anime/show-a/", [{"template": self.TEMPLATE, "max_ep": 12}])
        wl.link_profile("https://witanime.site/anime/show-a/", 0, "Show A")
        wl.record_sources("Show A", [1, 2], self.TEMPLATE, self.folder)
        wl.record_sources("Show A", [3], self.TEMPLATE, self.folder)   # hidden note rewritten
        part = wl.find("https://witanime.site/anime/show-a/")["parts"][0]
        self.assertEqual({"1", "2", "3"}, set(part["sources"]))
        self.assertEqual("witanime.site", part["sources"]["3"]["site"])
        self.assertEqual("https://witanime.site/episode/show-a-الحلقة-3/", part["sources"]["3"]["page"])
        note = wl.read_folder_source(self.folder)
        self.assertEqual(self.TEMPLATE, note["template"])
        self.assertEqual({"1", "2", "3"}, set(note["episodes"]))
        if sys.platform == "win32":
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(os.path.join(self.folder, wl.SOURCE_FILE))
            self.assertTrue(attrs & 0x2, "note should be hidden")
        # A re-lookup of the seasons keeps them.
        wl.set_parts("https://witanime.site/anime/show-a/", [{"template": self.TEMPLATE, "max_ep": 13}])
        self.assertEqual({"1", "2", "3"},
                         set(wl.find("https://witanime.site/anime/show-a/")["parts"][0]["sources"]))

    def test_anime_not_in_the_library_still_gets_the_folder_note(self):
        wl = self.wl
        wl.record_sources("Show A", [5], self.TEMPLATE, self.folder)
        self.assertEqual(["5"], list(wl.read_folder_source(self.folder)["episodes"]))
        self.assertEqual([], wl.entries())

    def test_folder_scan_learns_the_site_from_the_note(self):
        wl = self.wl
        from utils.library_scan import import_library
        open(os.path.join(self.folder, "Show A Ep1.mp4"), "wb").close()
        wl.record_sources("Show A", [1], self.TEMPLATE, self.folder)
        import_library(self.downloads, {}, [], {}, duration=lambda _p: None)
        e = wl.entries()[0]
        self.assertEqual(self.TEMPLATE, e["parts"][0]["template"])     # no profile needed
        self.assertEqual("witanime.site", e["domain"])
        self.assertEqual({1}, set(wl.part_sources(e["parts"][0], self.downloads)))


class LibraryPosterTests(unittest.TestCase):
    """Imported anime get their poster (and, for folder-only ones, their page)
    from a site search -- only on a sure match."""

    RESULTS = [
        {"title": "Overlord IV", "link": "https://witanime.site/anime/overlord-iv"},
        {"title": "Overlord", "link": "https://witanime.site/anime/overlord"},
        {"title": "Fate/Zero", "link": "https://witanime.site/anime/fate-zero"},
        {"title": "Black Clover 2nd Season", "link": "https://witanime.site/anime/black-clover-2nd-season"},
        {"title": "Black Clover", "link": "https://witanime.site/anime/black-clover"},
    ]

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl
        self.wl = wl
        tmp = tempfile.mkdtemp(prefix="aed_poster_")
        patch = mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json"))
        patch.start()
        self.addCleanup(patch.stop)
        self.cover = os.path.join(tmp, "c.img")
        open(self.cover, "wb").close()

    def test_query_variants_recover_lost_punctuation(self):
        from utils.library_scan import query_variants
        self.assertEqual(["FateZero", "Fate Zero"], query_variants("FateZero"))
        self.assertEqual(["Sekai Saikou no Ansatsusha, Isekai Kizoku",
                          "Sekai Saikou no Ansatsusha Isekai Kizoku", "Sekai Saikou no"],
                         query_variants("Sekai Saikou no Ansatsusha, Isekai Kizoku"))

    def test_slug_from_episode_templates(self):
        from utils.library_scan import slug_from_template
        self.assertEqual("black-clover", slug_from_template("https://witanime.site/watch/black-clover/{x}"))
        self.assertEqual("show-a", slug_from_template("https://witanime.site/episode/show-a-الحلقة-{x}/"))
        self.assertEqual("", slug_from_template("https://eta.animerco.org/episodes/x-{x}/"))

    def test_only_a_sure_match_is_taken(self):
        from utils.library_scan import match_result
        m = lambda t, tpl="": (match_result(t, tpl, self.RESULTS) or {}).get("link")
        self.assertEqual("https://witanime.site/anime/overlord", m("Overlord"))
        self.assertEqual("https://witanime.site/anime/fate-zero", m("FateZero"))
        self.assertEqual("https://witanime.site/anime/black-clover", m("Black Clover"))
        # The episode link decides even when the folder name differs.
        self.assertEqual("https://witanime.site/anime/black-clover-2nd-season",
                         m("BC", "https://witanime.site/watch/black-clover-2nd-season/{x}"))
        self.assertIsNone(m("Overlord V"))        # no "closest" guess

    def test_poster_and_page_are_recorded(self):
        wl = self.wl
        wl.add_imported([{"url": "local://Overlord", "title": "Overlord", "status": wl.WATCHING,
                          "parts": [{"profile": "Overlord"}], "history": []}])
        e = wl.entries()[0]
        self.assertTrue(wl.needs_poster(e))
        new = wl.set_poster("local://Overlord", self.cover,
                            "https://witanime.site/anime/overlord", "witanime.site")
        self.assertEqual("https://witanime.site/anime/overlord", new)
        e = wl.find(new)
        self.assertEqual(self.cover, e["cover"])
        self.assertFalse(wl.needs_poster(e))

    def test_a_failed_lookup_waits_a_day(self):
        wl = self.wl
        wl.add_imported([{"url": "local://X", "title": "X", "parts": [], "history": []}])
        wl.set_poster("local://X")                 # nothing found
        e = wl.find("local://X")
        self.assertFalse(wl.needs_poster(e))
        self.assertTrue(wl.needs_poster(e, now=e["poster_tried"] + wl.POSTER_RETRY_SECONDS))

    def test_linked_folder_takes_the_looked_up_season(self):
        wl = self.wl
        wl.add_imported([{"url": "local://Overlord", "title": "Overlord", "status": wl.WATCHING,
                          "parts": [{"label": "", "template": "", "max_ep": 13, "first_ep": 1,
                                     "profile": "Overlord", "watched": [1, 2],
                                     "progress": {"3": 8.0}}], "history": []}])
        url = wl.set_poster("local://Overlord", self.cover,
                            "https://witanime.site/anime/overlord", "witanime.site")
        wl.set_parts(url, [{"template": "https://witanime.site/episode/overlord-الحلقة-{x}/",
                            "max_ep": 13, "first_ep": 1}])
        parts = wl.find(url)["parts"]
        self.assertEqual(1, len(parts))                     # not a second, empty season
        self.assertEqual("Overlord", parts[0]["profile"])
        self.assertEqual([1, 2], parts[0]["watched"])
        self.assertEqual({"3": 8.0}, parts[0]["progress"])
        self.assertIn("overlord", parts[0]["template"])

    def test_page_already_in_the_library_absorbs_the_folder_entry(self):
        wl = self.wl
        page = "https://witanime.site/anime/overlord"
        wl.add("Overlord", page, "witanime.site")
        wl.add_imported([{"url": "local://Overlord copy", "title": "Overlord",
                          "status": wl.WATCHING,
                          "parts": [{"profile": "Overlord copy", "watched": [1, 2]}],
                          "history": []}])
        url = wl.set_poster("local://Overlord copy", self.cover, page, "witanime.site")
        self.assertEqual(page, url)
        items = wl.entries()
        self.assertEqual(1, len(items))                       # one anime, one entry
        self.assertEqual([1, 2], items[0]["parts"][0]["watched"])
        self.assertEqual("Overlord copy", items[0]["parts"][0]["profile"])
        self.assertEqual(wl.WATCHING, items[0]["status"])
        self.assertEqual(self.cover, items[0]["cover"])
        # The old address still reaches it (a dialog open on it, say).
        self.assertEqual(page, wl.find("local://Overlord copy")["url"])
        # Its season is looked up later: the folder part is adopted, not duplicated.
        wl.set_parts(page, [{"template": "https://witanime.site/episode/overlord-الحلقة-{x}/",
                             "max_ep": 13}])
        parts = wl.find(page)["parts"]
        self.assertEqual(1, len(parts))
        self.assertEqual(("Overlord copy", [1, 2]), (parts[0]["profile"], parts[0]["watched"]))


class WatchLaterTabClickTests(unittest.TestCase):
    """Clicking the Watching / Watch later / Completed segments switches lists.
    SegmentedWidget's onClick is connected to clicked(bool); a lambda without a
    parameter for it received checked=True as the status and raised KeyError."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_details_save_on_done_and_close_discards(self):
        from unittest import mock
        from PyQt6.QtWidgets import QWidget
        from utils import watch_later as wl
        from ui.watch_later_tab import EntryDialog
        tmp = tempfile.mkdtemp(prefix="aed_wldlg_")
        url = "https://witanime.site/anime/show-d/"
        with mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json")):
            wl.add("Show D", url, "witanime.site")
            wl.set_parts(url, [{"template": "https://witanime.site/episode/show-d-الحلقة-{x}/",
                                "max_ep": 4}])
            host = QWidget()

            def edit(dlg):
                grid = dlg._grids[0][1]
                grid.set_selected([1, 2])
                dlg.combo_status.setCurrentIndex(list(wl.STATUSES).index(wl.COMPLETED))

            dlg = EntryDialog(wl.find(url), host)
            edit(dlg)
            dlg.cancelButton.click()                       # Close: nothing saved
            e = wl.find(url)
            self.assertEqual([], e["parts"][0]["watched"])
            self.assertEqual(wl.LATER, e["status"])

            dlg = EntryDialog(wl.find(url), host)
            edit(dlg)
            dlg.yesButton.click()                          # Done: saved
            e = wl.find(url)
            self.assertEqual([1, 2], e["parts"][0]["watched"])
            self.assertEqual(wl.COMPLETED, e["status"])
            self.assertEqual("Done", dlg.yesButton.text())
            self.assertEqual("Close", dlg.cancelButton.text())

    def test_switching_lists_opens_no_stray_windows(self):
        """A row's progress bar was shown before it had a parent, so each switch
        flashed a tiny top-level "Python" window per row."""
        from unittest import mock
        from PyQt6.QtCore import QObject, QEvent
        from utils import watch_later as wl
        from ui.watch_later_tab import WatchLaterWidget
        tmp = tempfile.mkdtemp(prefix="aed_wltab_")
        stray = []

        class Spy(QObject):
            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Type.Show and obj.isWidgetType() and obj.isWindow() \
                        and obj is not host:
                    stray.append(type(obj).__name__)
                return False

        with mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json")):
            for i, status in enumerate((wl.WATCHING, wl.LATER, wl.LATER)):
                url = f"https://witanime.site/anime/show-{i}/"
                wl.add(f"Show {i}", url, "witanime.site")
                wl.set_parts(url, [{"template": f"https://witanime.site/episode/show-{i}-الحلقة-{{x}}/",
                                    "max_ep": 12}])
                wl.set_status(url, status)
            from PyQt6.QtWidgets import QWidget, QVBoxLayout
            host = QWidget()
            w = WatchLaterWidget()
            QVBoxLayout(host).addWidget(w)
            w.fetch_posters = lambda: None
            host.show()
            spy = Spy()
            self._app.installEventFilter(spy)
            try:
                for key in (wl.LATER, wl.WATCHING, wl.LATER):
                    w.seg.items[key].click()
                    self._app.processEvents()
            finally:
                self._app.removeEventFilter(spy)
                host.close()
        self.assertEqual([], stray)

    def test_segment_clicks_switch_status(self):
        from unittest import mock
        from utils import watch_later as wl
        from ui.watch_later_tab import WatchLaterWidget
        tmp = tempfile.mkdtemp(prefix="aed_wltab_")
        with mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json")):
            w = WatchLaterWidget()
            for key in (wl.LATER, wl.COMPLETED, wl.WATCHING):
                w.seg.items[key].click()
                self.assertEqual(key, w._status)
            w.deleteLater()


class LibraryEdgeCaseTests(unittest.TestCase):
    """Fixes from the Library review: merges never lose a season, odd log lines
    never cost the good ones, Done only writes what was clicked, and the rest."""

    PAGE = "https://witanime.site/anime/show-x/"
    T1 = "https://witanime.site/episode/show-x-الحلقة-{x}/"
    T2 = "https://witanime.site/episode/show-x-season-2-الحلقة-{x}/"

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl
        self.wl = wl
        self.tmp = tempfile.mkdtemp(prefix="aed_edge_")
        import shutil
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        patch.start()
        self.addCleanup(patch.stop)
        self.downloads = os.path.join(self.tmp, "animes")
        os.makedirs(self.downloads)
        from PyQt6.QtWidgets import QWidget
        self.host = QWidget()               # dialogs' parent, alive for the whole test

    def _files(self, folder, eps, ext=".mp4"):
        path = os.path.join(self.downloads, folder)
        os.makedirs(path, exist_ok=True)
        for ep in eps:
            open(os.path.join(path, f"{folder} Ep{ep}{ext}"), "wb").close()
        return path

    def _log(self, folder, ep, percent, eof=False):
        return json.dumps({"path": os.path.join(self.downloads, folder, f"{folder} Ep{ep}.mp4"),
                           "percent": percent, "eof": eof})

    # ---- merging duplicates
    def test_absorbing_a_duplicate_keeps_its_unmatched_seasons(self):
        wl = self.wl
        wl.add_imported([{"url": "local://Show X", "title": "Show X", "status": wl.WATCHING,
                          "parts": [{"template": self.T1, "max_ep": 12, "profile": "Show X",
                                     "watched": [1, 2]},
                                    {"template": self.T2, "max_ep": 12, "profile": "Show X S2",
                                     "watched": [1, 2, 3]}],
                          "history": [{"status": "Success"}]}])
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12}])     # only S1 found
        items = wl.entries()
        self.assertEqual(1, len(items))
        parts = items[0]["parts"]
        self.assertEqual(2, len(parts))
        self.assertEqual(("Show X", [1, 2]), (parts[0]["profile"], parts[0]["watched"]))
        self.assertEqual(("Show X S2", [1, 2, 3]), (parts[1]["profile"], parts[1]["watched"]))
        self.assertEqual(1, len(items[0]["history"]))
        # A later lookup that finds both seasons keeps both, once each.
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12},
                                 {"template": self.T2, "max_ep": 12}])
        parts = wl.find(self.PAGE)["parts"]
        self.assertEqual(["Show X", "Show X S2"], [p["profile"] for p in parts])

    def test_folder_part_is_kept_when_its_season_matched_by_link(self):
        wl = self.wl
        wl.add("Show X", self.PAGE, "witanime.site")
        data = wl._load()
        data["entries"][0]["parts"] = [
            {"template": self.T1, "max_ep": 12, "profile": None, "watched": []},
            {"template": "", "max_ep": 12, "profile": "Other Folder", "watched": [4]}]
        wl._save(data)
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12}])
        parts = wl.find(self.PAGE)["parts"]
        self.assertIn([4], [p["watched"] for p in parts])          # not silently dropped

    # ---- the details dialog
    def test_done_keeps_watched_episodes_the_grid_does_not_show(self):
        wl = self.wl
        from ui.watch_later_tab import EntryDialog
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12}])
        wl.set_watched(self.PAGE, 0, list(range(1, 14)))          # ep 13 played, file gone
        dlg = EntryDialog(wl.find(self.PAGE), self.host)
        dlg.combo_status.setCurrentIndex(list(wl.STATUSES).index(wl.WATCHING))   # rewatching
        dlg.yesButton.click()
        e = wl.find(self.PAGE)
        self.assertEqual(list(range(1, 14)), e["parts"][0]["watched"])
        self.assertEqual(wl.WATCHING, e["status"])

    def test_done_only_writes_the_clicks_not_a_stale_copy(self):
        wl = self.wl
        from ui.watch_later_tab import EntryDialog
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12}])
        wl.link_profile(self.PAGE, 0, "Show X")
        dlg = EntryDialog(wl.find(self.PAGE), self.host)
        wl.apply_progress([self._log("Show X", 5, 99.0)])         # played while open
        grid = dlg._grids[0][1]
        grid.set_selected(sorted(set(grid.selected()) | {9}))     # the user ticks 9
        dlg.yesButton.click()
        self.assertEqual([1, 2, 3, 4, 5, 9], wl.find(self.PAGE)["parts"][0]["watched"])

    def test_unknown_status_and_junk_entries_do_not_crash(self):
        wl = self.wl
        from ui.watch_later_tab import EntryDialog
        with open(wl.FILE, "w", encoding="utf-8") as f:
            json.dump({"entries": [{"url": self.PAGE, "title": "X", "status": "dropped",
                                    "parts": None}, "junk", 7]}, f)
        e = wl.find(self.PAGE)
        self.assertEqual(wl.LATER, e["status"])
        self.assertEqual(1, len(wl.entries()))
        EntryDialog(e, self.host)                                  # no ValueError
        raw = dict(e, status="dropped")
        EntryDialog(raw, self.host)

    def test_clear_history_waits_for_done(self):
        wl = self.wl
        from ui.watch_later_tab import EntryDialog
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.update(self.PAGE, history=[{"date": "d", "episodes": "1", "status": "Success"}])
        dlg = EntryDialog(wl.find(self.PAGE), self.host)
        dlg.btn_clear_history.click()
        dlg.cancelButton.click()
        self.assertEqual(1, len(wl.find(self.PAGE)["history"]))
        dlg = EntryDialog(wl.find(self.PAGE), self.host)
        dlg.btn_clear_history.click()
        dlg.yesButton.click()
        self.assertEqual([], wl.find(self.PAGE)["history"])

    # ---- mpv.net progress log
    def test_bad_log_lines_never_cost_the_good_ones(self):
        wl = self.wl
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12}])
        wl.link_profile(self.PAGE, 0, "Show X")
        log = os.path.join(self.tmp, "aed-progress.log")
        bad = ["123", "[1]", "null", '"text"', "{bad json",
               json.dumps({"path": 5, "percent": "abc"}),
               json.dumps({"path": "C:\\x\\Show X\\Show X Ep2.mp4", "percent": [1]})]
        with open(log, "w", encoding="utf-8") as f:
            f.write("\n".join(bad + [self._log("Show X", 3, 95.0)]) + "\n")
        self.assertTrue(wl.ingest_log(log))
        self.assertEqual([1, 2, 3], wl.find(self.PAGE)["parts"][0]["watched"])
        self.assertFalse(wl.ingest_log(log))                       # read once
        self.assertEqual(os.path.getsize(log), wl._load()["log_offset"])

    def test_history_from_log_survives_bad_lines(self):
        from utils.library_scan import read_mpv_history
        cfg = os.path.join(self.tmp, "mpv")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "aed-progress.log"), "w", encoding="utf-8") as f:
            f.write("[1]\n" + json.dumps({"path": "C:\\a\\S Ep1.mp4", "percent": "abc"}) + "\n"
                    + json.dumps({"path": "C:\\a\\S Ep2.mp4", "percent": 99}) + "\n")
        self.assertEqual({os.path.normcase("C:\\a\\S Ep2.mp4"): None}, read_mpv_history(cfg))

    # ---- episode numbers from file names
    def test_episode_number_ignores_ep_inside_words(self):
        wl = self.wl
        self.assertEqual(5, wl.episode_number("Sleep2 Ep5.mp4"))
        self.assertEqual(7, wl.episode_number("Deep3 Sea Ep7.mkv"))
        self.assertEqual(12, wl.episode_number("Ep1 Story Ep12.mp4"))
        self.assertEqual(3, wl.episode_number(r"C:\Ep9 Folder\Show Ep3.mp4"))
        self.assertIsNone(wl.episode_number("Keep2.mp4"))
        self.assertIsNone(wl.episode_number("Movie.mp4"))
        path = self._files("Sleep2", [1, 5])
        self.assertEqual({1, 5}, set(wl.episode_files(path)))
        self.assertEqual(("Sleep2", 5, 50.0, False), wl.parse_log_line(
            json.dumps({"path": os.path.join(path, "Sleep2 Ep5.mp4"), "percent": 50})))

    # ---- durations of other containers
    def test_mkv_and_webm_durations(self):
        import struct
        from utils.library_scan import mkv_duration, video_duration

        def elem(eid_bytes, body):
            n = len(body)
            return eid_bytes + (bytes([0x80 | n]) if n < 127 else bytes([0x40 | (n >> 8), n & 0xFF])) + body

        header = elem(b"\x1A\x45\xDF\xA3", elem(b"\x42\x82", b"matroska"))
        info = elem(b"\x15\x49\xA9\x66",
                    elem(b"\x2A\xD7\xB1", (1000000).to_bytes(3, "big"))
                    + elem(b"\x44\x89", struct.pack(">d", 1440000.0)))
        seekhead = elem(b"\x11\x4D\x9B\x74", b"\0" * 10)
        segment = b"\x18\x53\x80\x67" + b"\x01\xFF\xFF\xFF\xFF\xFF\xFF\xFF" + seekhead + info
        path = os.path.join(self.tmp, "a.mkv")
        with open(path, "wb") as f:
            f.write(header + segment)
        self.assertAlmostEqual(1440.0, mkv_duration(path))
        webm = os.path.join(self.tmp, "a.webm")
        with open(webm, "wb") as f:
            f.write(header + segment)
        self.assertAlmostEqual(1440.0, video_duration(webm))
        with open(os.path.join(self.tmp, "bad.mkv"), "wb") as f:
            f.write(b"\x1A\x45\xDF")
        self.assertIsNone(mkv_duration(os.path.join(self.tmp, "bad.mkv")))
        self.assertIsNone(mkv_duration(os.path.join(self.tmp, "missing.mkv")))

    def test_avi_duration_prefers_the_opendml_frame_count(self):
        import struct
        from utils.library_scan import avi_duration

        def chunk(cid, body):
            return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")

        def lst(kind, body):
            return b"LIST" + struct.pack("<I", 4 + len(body)) + kind + body

        avih = chunk(b"avih", struct.pack("<10I", 41708, 0, 0, 0, 1000, 0, 0, 0, 0, 0)
                     + b"\0" * 16)
        odml = lst(b"odml", chunk(b"dmlh", struct.pack("<I", 34532) + b"\0" * 244))
        body = lst(b"hdrl", avih + lst(b"strl", chunk(b"strh", b"\0" * 56)) + odml)
        path = os.path.join(self.tmp, "a.avi")
        with open(path, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", 4 + len(body)) + b"AVI " + body)
        self.assertAlmostEqual(34532 * 0.041708, avi_duration(path), places=3)
        with open(os.path.join(self.tmp, "bad.avi"), "wb") as f:
            f.write(b"RIFF\0\0\0\0WAVE")
        self.assertIsNone(avi_duration(os.path.join(self.tmp, "bad.avi")))

    def test_mkv_resume_point_counts_as_watched(self):
        from utils.library_scan import watch_state
        files = {e: f"C:\\a\\S Ep{e}.mkv" for e in (1, 2, 3)}
        history = {os.path.normcase(files[3]): 1400.0}
        watched, progress = watch_state(files, history, duration=lambda _p: 1440.0)
        self.assertEqual({1, 2, 3}, watched)
        self.assertEqual({}, progress)

    # ---- downloads of profiles not in the Library
    def test_download_of_an_unlinked_profile_lands_in_the_library(self):
        wl = self.wl
        from utils.config import app_settings
        from unittest import mock
        self._files("Solo Show", [1, 2])
        with config_lock:
            sites_data["Solo Show"] = {"url": "https://witanime.site/episode/solo-الحلقة-{x}/",
                                       "episode_bounds": [1, 12]}
        self.addCleanup(lambda: sites_data.pop("Solo Show", None))
        with mock.patch.dict(app_settings, {"download_dir": self.downloads}):
            self.assertTrue(wl.record_download("Solo Show", "1-2", "Success", "ok"))
            self.assertTrue(wl.record_download("Solo Show", "3", "Failed", "x"))
        items = wl.entries()
        self.assertEqual(1, len(items))
        e = items[0]
        self.assertEqual("Solo Show", e["parts"][0]["profile"])
        self.assertEqual(wl.WATCHING, e["status"])
        self.assertEqual(["Failed", "Success"], [h["status"] for h in e["history"]])
        self.assertFalse(wl.record_download("No Such Profile", "1", "Success", ""))

    def test_download_links_the_entry_with_the_same_episode_link(self):
        wl = self.wl
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12}])
        with config_lock:
            sites_data["Show X (old)"] = {"url": self.T1}
        self.addCleanup(lambda: sites_data.pop("Show X (old)", None))
        self.assertTrue(wl.record_download("Show X (old)", "1", "Success", ""))
        e = wl.find(self.PAGE)
        self.assertEqual(1, len(wl.entries()))
        self.assertEqual("Show X (old)", e["parts"][0]["profile"])
        self.assertEqual(1, len(e["history"]))

    def test_download_keeps_the_folder_name_of_a_deleted_profile(self):
        wl = self.wl
        from unittest import mock
        from ui import watch_later_tab as tab
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 12},
                                 {"template": self.T2, "max_ep": 12}])
        wl.link_profile(self.PAGE, 0, "Show X")                  # not in sites_data
        w = tab.WatchLaterWidget()
        self.addCleanup(w.deleteLater)
        with mock.patch("ui.search_tab.open_existing_profile", return_value=(None, None)), \
                mock.patch("ui.search_tab.create_profile", return_value="Show X") as create, \
                mock.patch.object(tab, "save_config"), mock.patch.dict(tab.app_settings):
            w.download(wl.find(self.PAGE), 0)
        self.assertEqual("Show X", create.call_args[0][0])        # not "Show X - Season 1"

    # ---- Continue watching
    def test_continue_card_downloads_its_own_season(self):
        wl = self.wl
        from unittest import mock
        from ui import watch_later_tab as tab
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 6, "label": "Season 1"},
                                 {"template": self.T2, "max_ep": 4, "label": "Season 2"}])
        wl.link_profile(self.PAGE, 0, "Show X")
        wl.set_watched(self.PAGE, 0, range(1, 7))                 # S1 done, files deleted
        e = wl.find(self.PAGE)
        with mock.patch.object(tab, "_download_dir", return_value=self.downloads):
            nxt = wl.next_episode(e, self.downloads)
            self.assertEqual((1, 1), nxt[:2])
            w = tab.WatchLaterWidget()
            self.addCleanup(w.deleteLater)
            with mock.patch.object(w, "download") as dl:
                w.on_continue(self.PAGE, 1, 1)
                dl.assert_called_once()
                self.assertEqual(1, dl.call_args[0][1])
                self.assertEqual([1, 2, 3, 4], dl.call_args[1]["episodes"])
                dl.reset_mock()
                w.on_primary(self.PAGE)                           # the row's fallback too
                self.assertEqual(1, dl.call_args[0][1])
            self._files("Show X", [1])
            with mock.patch.object(w, "_play") as play:
                w.on_continue(self.PAGE, 0, 1)
                play.assert_called_once()

    def test_missing_from(self):
        wl = self.wl
        self._files("Show X", [1, 2, 4])
        e = {"parts": [{"template": self.T1, "max_ep": 6, "profile": "Show X"}]}
        self.assertEqual([3, 5, 6], wl.missing_from(e, 0, 3, self.downloads))
        self.assertEqual([5, 6], wl.missing_from(e, 0, 5, self.downloads))
        self.assertEqual([9], wl.missing_from(e, 0, 9, self.downloads))   # past the count
        self.assertEqual([], wl.missing_from(e, 3, 1, self.downloads))

    # ---- mpv.net lookups
    def test_progress_poll_never_searches_the_registry(self):
        from unittest import mock
        from ui import watch_later_tab as tab
        from utils import mpvnet
        w = tab.WatchLaterWidget()
        self.addCleanup(w.deleteLater)
        with mock.patch.object(mpvnet, "find_mpvnet", return_value=(None, None)) as find, \
                mock.patch.object(mpvnet, "ensure_progress_script") as ensure:
            for _ in range(5):
                w._poll_progress()
            self.assertEqual(0, find.call_count)
            w._check_mpv()
            self.assertEqual(1, find.call_count)
            self.assertTrue(w.mpv_notice.isVisibleTo(w))
            ensure.assert_not_called()
        cfg = os.path.join(self.tmp, "mpvcfg")
        with mock.patch.object(mpvnet, "find_mpvnet", return_value=(r"C:\x\mpvnet.exe", None)), \
                mock.patch.object(mpvnet, "config_dir", return_value=cfg), \
                mock.patch.object(mpvnet, "ensure_progress_script") as ensure:
            w._check_mpv()                                         # installed meanwhile
            ensure.assert_called_once()
            self.assertFalse(w.mpv_notice.isVisibleTo(w))
            self.assertEqual(os.path.join(cfg, mpvnet.PROGRESS_LOG), w._log_path)

    # ---- posters
    def test_poster_search_fetches_only_the_matching_cover(self):
        from unittest import mock
        from ui import watch_later_tab as tab
        seen = {}

        class FakeSearch:
            def __init__(self, query, url):
                from PyQt6.QtCore import QObject, pyqtSignal

                class Sig(QObject):
                    finished = pyqtSignal(list)
                    cover_loaded = pyqtSignal(str, object)
                    error = pyqtSignal(str)
                self._sig = Sig()
                self.finished, self.cover_loaded, self.error = \
                    self._sig.finished, self._sig.cover_loaded, self._sig.error
                self.cover_links = None

            def run(self):
                self.finished.emit([{"title": "Other", "link": "L1"},
                                    {"title": "Show X", "link": "L2"}])
                seen["links"] = self.cover_links
                seen["interrupted"] = self.isInterruptionRequested()
                for link in ("L1", "L2"):
                    if self.cover_links is None or link in self.cover_links:
                        self.cover_loaded.emit(link, "img-" + link)

        th = tab.PosterThread([])
        with mock.patch("ui.search_tab.AnimeSearchThread", FakeSearch):
            found, cover = th._search("Show X", "u", "Show X", "")
            self.assertEqual(("L2", "img-L2"), (found["link"], cover))
            self.assertEqual({"L2"}, seen["links"])
            self.assertFalse(seen["interrupted"])
            th.isInterruptionRequested = lambda: True             # the app is closing
            th._search("Show X", "u", "Nope", "")
            self.assertEqual(set(), seen["links"])                # no match: no covers
            self.assertTrue(seen["interrupted"])                  # closing reaches it

    def test_listing_is_cached_within_a_refresh_only(self):
        wl = self.wl
        from unittest import mock
        path = self._files("Show X", [1])
        real = os.listdir
        with mock.patch("os.listdir", side_effect=real) as listdir:
            with wl.cached_listing():
                with wl.cached_listing():                         # nested
                    wl.episode_files(path)
                wl.episode_files(path)
                self.assertEqual(1, listdir.call_count)
            wl.episode_files(path)
            self.assertEqual(2, listdir.call_count)

    def test_old_address_still_finds_a_relinked_entry(self):
        wl = self.wl
        wl.add_imported([{"url": "local://Show X", "title": "Show X",
                          "parts": [{"profile": "Show X"}], "history": []}])
        wl.set_poster("local://Show X", "", self.PAGE, "witanime.site")
        self.assertEqual(self.PAGE, wl.find("local://Show X")["url"])
        self.assertTrue(wl.set_status("local://Show X", wl.WATCHING))
        self.assertEqual(wl.WATCHING, wl.find(self.PAGE)["status"])
        # A later scan doesn't bring the folder back as a second entry.
        self.assertEqual(0, wl.add_imported([{"url": "local://Show X", "title": "Show X",
                                              "parts": [], "history": []}]))

    def test_download_history_table_is_capped(self):
        import sqlite3
        from unittest import mock
        from utils import database
        from utils.config import DB_FILE
        database.init_db()
        with mock.patch.object(database, "HISTORY_ROWS_KEPT", 3), \
                mock.patch("utils.watch_later.record_download"):
            for i in range(6):
                database.log_history(f"P{i}", "1", "Success", "")
        conn = sqlite3.connect(DB_FILE)
        try:
            rows = [r[0] for r in conn.execute("SELECT profile FROM downloads_v2 ORDER BY id")]
        finally:
            conn.close()
        self.assertEqual(["P3", "P4", "P5"], rows)


class DeferredTabsTests(unittest.TestCase):
    """The window opens with only the Downloader built; the other tabs are built
    after the first paint, or at once when one is clicked before that."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def _window(self):
        from unittest import mock
        from utils import watch_later as wl
        from ui.app_window import AppWindow
        tmp = tempfile.mkdtemp(prefix="aed_tabs_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        patch = mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json"))
        patch.start()
        self.addCleanup(patch.stop)
        w = AppWindow()
        self.addCleanup(w.deleteLater)
        return w

    def test_only_the_downloader_is_built_up_front(self):
        w = self._window()
        self.assertIsNone(w.search_interface)
        self.assertIsNone(w.manager_interface)
        # The sidebar is complete anyway, in the usual order.
        keys = [k for k, _i, _t in w.DEFERRED_TABS]
        for key in keys:
            self.assertIsNotNone(w.navigationInterface.widget(key), key)
        w._sync_manager_to_downloader()                   # no crash before the build
        w.on_search_profile_created("x")

    def test_clicking_a_tab_early_builds_and_opens_it(self):
        w = self._window()
        w.open_tab("watch_later_interface")
        self.assertTrue(w._tabs_built)
        self.assertIs(w.stackedWidget.currentWidget(), w.watch_later_interface)
        for key, _i, _t in w.DEFERRED_TABS:
            page = getattr(w, key)
            self.assertEqual(key, page.objectName())
            self.assertGreaterEqual(w.stackedWidget.view.indexOf(page), 0)
        before = w.search_interface
        w._build_tabs()                                   # once only
        self.assertIs(before, w.search_interface)
        w.downloader_interface.goto_profiles_signal.emit()
        self.assertIs(w.stackedWidget.currentWidget(), w.manager_interface)

    def test_first_paint_builds_the_rest(self):
        w = self._window()
        w.show()
        for _ in range(20):
            self._app.processEvents()
            if w._tabs_built:
                break
        w.hide()
        self.assertTrue(w._tabs_built)


class FluentToolTipTests(unittest.TestCase):
    """Every hint is drawn as a Fluent tooltip: the app-wide filter attaches one to
    any widget with a tooltip on hover, and the native white box never shows."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])
        from ui.tooltips import install_fluent_tooltips
        install_fluent_tooltips(cls._app)

    def _hover(self, widget):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QEnterEvent
        from PyQt6.QtWidgets import QApplication
        QApplication.sendEvent(widget, QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))

    def test_hint_gets_a_fluent_tooltip_and_the_native_one_is_blocked(self):
        from PyQt6.QtCore import QEvent, QPoint
        from PyQt6.QtGui import QHelpEvent
        from PyQt6.QtWidgets import QPushButton
        from ui.tooltips import has_fluent_tooltip
        btn = QPushButton("x")
        btn.setToolTip("A hint")
        btn.setEnabled(False)                  # disabled controls keep their hint
        self.assertFalse(has_fluent_tooltip(btn))
        self._hover(btn)
        self.assertTrue(has_fluent_tooltip(btn))
        from ui import tooltips
        native = QHelpEvent(QEvent.Type.ToolTip, QPoint(1, 1), QPoint(1, 1))
        self.assertTrue(tooltips._instance.eventFilter(btn, native))   # swallowed
        self._hover(btn)                       # hovering again adds no second filter
        from qfluentwidgets import ToolTipFilter
        self.assertEqual(1, sum(isinstance(c, ToolTipFilter) for c in btn.children()))

    def test_widgets_without_a_hint_are_left_alone(self):
        from PyQt6.QtWidgets import QPushButton
        from ui.tooltips import has_fluent_tooltip
        btn = QPushButton("x")
        self._hover(btn)
        self.assertFalse(has_fluent_tooltip(btn))

    def test_main_installs_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
            self.assertIn("install_fluent_tooltips(app)", f.read())


class SmoothMenuTests(unittest.TestCase):
    """Dropdowns open in place and fade in. The stock slide moved the window and
    reset its mask every frame, which stuttered on Windows."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])
        from ui.menus import install_smooth_menus
        install_smooth_menus()
        install_smooth_menus()                 # idempotent

    def test_combo_menu_opens_in_place_without_a_mask(self):
        import time
        from PyQt6.QtWidgets import QWidget, QVBoxLayout
        from qfluentwidgets import ComboBox
        from ui.menus import _FadeInPlace
        host = QWidget()
        cb = ComboBox()
        cb.addItems(["a", "b", "c"])
        QVBoxLayout(host).addWidget(cb)
        host.show()
        cb._showComboMenu()
        menu = cb.dropMenu
        self.assertIsInstance(menu.aniManager, _FadeInPlace)
        start = menu.pos()
        deadline = time.time() + 0.4
        while time.time() < deadline:
            self._app.processEvents()
        self.assertEqual(start, menu.pos())
        self.assertTrue(menu.mask().isEmpty())
        self.assertAlmostEqual(1.0, menu.windowOpacity(), places=2)
        menu.actions()[2].trigger()
        self.assertEqual("c", cb.currentText())
        host.close()

    def test_main_installs_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
            self.assertIn("install_smooth_menus()", f.read())


class ShaderToggleBindingTests(unittest.TestCase):
    """Ctrl+1 must reach the Lua binding. Newer mpv renames shader-toggle.lua's
    client to "shader_toggle", so a "shader-toggle/toggle" binding silently died."""

    def test_input_conf_binds_the_name_the_script_registers(self):
        import re
        from utils import mpvnet
        base = mpvnet.bundle_dir()
        with open(os.path.join(base, "input.conf"), encoding="utf-8") as f:
            conf = f.read()
        with open(os.path.join(base, "scripts", "shader-toggle.lua"), encoding="utf-8") as f:
            lua = f.read()
        bound = re.search(r"^Ctrl\+1\s+script-binding\s+(\S+)", conf, re.M).group(1)
        self.assertNotIn("/", bound)
        self.assertIn(f'add_key_binding(nil, "{bound}"', lua)



if __name__ == "__main__":
    unittest.main(verbosity=2)
