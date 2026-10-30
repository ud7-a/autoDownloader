"""Watchlist: release days, the daily check, new-episode notices and covers.

Run with: py -m unittest discover -s tests
"""

import os
import sys
import unittest

# The app's data, shortcut and mpv.net state folders point at a temp folder
# (tests/__init__.py) BEFORE any app module is imported. `discover -s tests`
# imports this file on its own, so the package is imported here explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from ui.watchlist_tab import entries_airing_today  # noqa: E402


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
        def airing(label):
            return self.s.find_day({"title": "Hell Mode", "url": ""}, items,
                                   season=self.s.season_number(label)) is not None
        self.assertTrue(airing("Season 2"))
        for label in ("Season 1", "Season 3"):
            self.assertFalse(airing(label), label)

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
        self.assertEqual(cfg.get_watchlist()[1]["seen_max"], 7)          # state kept
        self.assertFalse(cfg.restore_watch(b, 1))                         # no duplicate


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
