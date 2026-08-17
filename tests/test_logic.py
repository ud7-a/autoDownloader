"""Unit tests for the app's pure logic (no Qt widgets, no network, no browser).

Run from the repo root with:

    py -m unittest discover -s tests -v

These cover the parsing/formatting rules that the download and search features are
built on -- the places where a silent regression would quietly break real downloads
(wrong episode ranges, unreachable episode URLs, mis-detected seasons).
"""

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

from ui.downloader_tab import compact_episode_spec, spec_to_ranges          # noqa: E402
from ui.search_tab import (extract_domain, _full_res, AnimeDetailsThread,   # noqa: E402
                           SUPPORTED_SITES, DEFAULT_SITE_FLOWS)
from core.selenium_engine import (_format_eta, _aria_convert_unit,          # noqa: E402
                                  parse_smart_xpath, episode_url_variants,
                                  is_block_page, _host_of)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
