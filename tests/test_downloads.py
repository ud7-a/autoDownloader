"""Downloads: episode ranges, the engine's mirror and tab handling, retries, concurrency, pausing, resuming and the Downloader tab.

Run with: py -m unittest discover -s tests
"""

import os
import shutil
import sys
import tempfile
import unittest

# The app's data, shortcut and mpv.net state folders point at a temp folder
# (tests/__init__.py) BEFORE any app module is imported. `discover -s tests`
# imports this file on its own, so the package is imported here explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from ui.downloader_tab import (compact_episode_spec, spec_to_ranges,  # noqa: E402
                               encode_check_url)
from ui.search_tab import DEFAULT_SITE_FLOWS  # noqa: E402
from core.selenium_engine import (_format_eta, _aria_convert_unit,  # noqa: E402
                                  parse_smart_xpath, episode_url_variants,
                                  is_block_page, tab_matches_path, PATH_HOSTS,
                                  rotate_error_log, return_to_episode)


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
        self.addCleanup(shutil.rmtree, tmp, True)
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


class ReturnToEpisodeTests(unittest.TestCase):
    """Each download path must start on the episode page. A path that failed on its
    host's tab used to leave the browser there, so the next path searched the wrong
    page -- One Piece 612: MediaFire file removed, Google Drive tried inside MediaFire."""

    EPISODE = "https://witanime.site/episode/one-piece-الحلقة-612/"

    class _Browser:
        """Tabs as {handle: url}; closing via CDP or close() removes the tab."""
        def __init__(self, tabs, current):
            from types import SimpleNamespace
            self.tabs = dict(tabs)
            self.current = current
            self.loaded = []
            self.switch_to = SimpleNamespace(window=self._switch)

        @property
        def window_handles(self):
            return list(self.tabs)

        @property
        def current_url(self):
            return self.tabs[self.current]

        def _switch(self, handle):
            if handle not in self.tabs:
                raise RuntimeError("no such window")
            self.current = handle

        def execute_cdp_cmd(self, cmd, args):
            del self.tabs[args["targetId"]]

        def close(self):
            del self.tabs[self.current]

        def get(self, url):
            self.tabs[self.current] = url
            self.loaded.append(url)

    def back(self, browser):
        from unittest import mock
        with mock.patch("core.selenium_engine.time.sleep"):
            return return_to_episode(browser, "ep", self.EPISODE, {"base"})

    def test_a_failed_host_tab_is_left_for_the_episode_page(self):
        b = self._Browser({"base": "about:blank", "ep": self.EPISODE,
                           "mf": "https://www.mediafire.com/file/x/ep612.mp4/file"}, "mf")
        self.assertTrue(self.back(b))
        self.assertEqual(b.current, "ep")
        self.assertEqual(set(b.tabs), {"base", "ep"})
        self.assertEqual(b.loaded, [])          # same page, no reload needed

    def test_the_downloads_tab_and_ads_are_closed_too(self):
        b = self._Browser({"base": "about:blank", "ep": self.EPISODE,
                           "ad": "https://ads.example/pop", "dl": "chrome://downloads/"}, "dl")
        self.assertTrue(self.back(b))
        self.assertEqual(set(b.tabs), {"base", "ep"})

    def test_an_episode_tab_navigated_away_is_reloaded(self):
        # gofile opens in the episode's own tab, with no popup.
        b = self._Browser({"base": "about:blank", "ep": "https://gofile.io/d/abc"}, "ep")
        self.assertTrue(self.back(b))
        self.assertEqual(b.loaded, [self.EPISODE])

    def test_a_lost_episode_tab_reports_failure(self):
        b = self._Browser({"base": "about:blank",
                           "mf": "https://www.mediafire.com/file/x/ep612.mp4/file"}, "mf")
        self.assertFalse(self.back(b))
        self.assertEqual(set(b.tabs), {"base"})

    def test_mapped_hosts_are_bare_domains(self):
        """Hosts are compared with == and endswith('.'+host), so a scheme, path or
        leading dot in this table would silently never match anything."""
        for name, hosts in PATH_HOSTS.items():
            for h in hosts:
                self.assertNotIn("/", h, f"{name}: '{h}' should be a bare domain")
                self.assertFalse(h.startswith("."), f"{name}: '{h}' has a leading dot")
                self.assertEqual(h, h.lower(), f"{name}: '{h}' should be lowercase")


class DownloadNextTests(unittest.TestCase):
    """Play next's episode isn't downloaded: the app opens on the Downloader with
    that anime's profile and its missing episodes selected, and says so."""

    def _window(self, found, running=False, profile="Show B"):
        from types import SimpleNamespace
        from unittest import mock
        library = mock.Mock()
        library.download.return_value = profile
        win = SimpleNamespace(raise_to_front=mock.Mock(), show_library=mock.Mock(),
                              _download_running=running,
                              _ensure_library=mock.Mock(return_value=library))
        return win, library

    def _run(self, win, found, missing=(6, 7, 8)):
        from unittest import mock
        from ui.app_window import AppWindow
        with mock.patch("utils.quick_play.decide", return_value=found), \
                mock.patch("utils.watch_later.entries", return_value=[]), \
                mock.patch("utils.watch_later.missing_from", return_value=list(missing)), \
                mock.patch("qfluentwidgets.InfoBar") as bar:
            AppWindow.download_next(win)
        return bar

    def test_sets_up_the_downloader_and_tells_the_user(self):
        entry = {"title": "Show B", "url": "https://x/anime/b/"}
        win, library = self._window(None)
        bar = self._run(win, ("download", entry, 0, 6))
        win.raise_to_front.assert_called_once()
        library.download.assert_called_once_with(entry, 0, episodes=[6, 7, 8])
        text = bar.warning.call_args[0][1]
        self.assertIn("Show B episode 6 isn't downloaded yet", text)
        self.assertIn("Episodes 6-8 are selected", text)
        win.show_library.assert_not_called()

    def test_while_downloading_it_only_says_so(self):
        entry = {"title": "Show B", "url": "https://x/anime/b/"}
        win, library = self._window(None, running=True)
        bar = self._run(win, ("download", entry, 0, 6))
        library.download.assert_not_called()
        self.assertIn("once the current download has finished", bar.warning.call_args[0][1])

    def test_no_message_when_the_profile_couldnt_be_set_up(self):
        entry = {"title": "Show B", "url": "https://x/anime/b/"}
        win, library = self._window(None, profile=None)
        bar = self._run(win, ("download", entry, 0, 6))
        bar.warning.assert_not_called()

    def test_downloaded_since_opens_the_library(self):
        win, library = self._window(None)
        self._run(win, ("play", {}, 0, 6, ["x.mp4"]))
        win.show_library.assert_called_once()
        library.download.assert_not_called()

    def test_hand_over_posts_the_registered_message(self):
        from unittest import mock
        from utils import instance
        user32 = mock.Mock()
        user32.IsIconic.return_value = False
        user32.RegisterWindowMessageW.side_effect = lambda name: {
            instance.OPEN_LIBRARY_MESSAGE: 0xC001, instance.DOWNLOAD_NEXT_MESSAGE: 0xC002}[name]
        with mock.patch.object(instance, "app_is_running", return_value=True), \
                mock.patch.object(instance, "find_main_window", return_value=1234), \
                mock.patch.object(instance, "_user32", return_value=user32), \
                mock.patch.object(instance.sys, "platform", "win32"):
            self.assertTrue(instance.forward_download_next())
            user32.PostMessageW.assert_called_with(1234, 0xC002, 0, 0)
            self.assertTrue(instance.forward_open_library())
            user32.PostMessageW.assert_called_with(1234, 0xC001, 0, 0)
        with mock.patch.object(instance, "app_is_running", return_value=False):
            self.assertFalse(instance.forward_download_next())


if __name__ == "__main__":
    unittest.main(verbosity=2)
