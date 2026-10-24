"""Library: watched tracking from mpv.net, Continue and the Continue shortcut, folder import, posters, the episode dialog, shortcuts and Free up space.

Run with: py -m unittest discover -s tests
"""

import json
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

from utils.config import sites_data, config_lock  # noqa: E402


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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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

    def test_unticking_moves_continue_off_a_later_started_episode(self):
        wl = self.wl
        profile = self._linked(max_ep=28)
        self._files(profile, range(1, 29))
        wl.apply_progress([self._log(profile, 28, 0.5)])         # jumped to 28 by mistake
        self.assertEqual(28, wl.next_episode(wl.find(self.URL), self.downloads)[1])
        wl.change_watched(self.URL, 0, removed=range(19, 28))
        part = wl.find(self.URL)["parts"][0]
        self.assertEqual({}, part["progress"])
        self.assertEqual(19, wl.next_episode(wl.find(self.URL), self.downloads)[1])

    def test_unticking_later_episodes_keeps_earlier_progress(self):
        wl = self.wl
        profile = self._linked(max_ep=20)
        wl.apply_progress([self._log(profile, 10, 20.0)])        # 1-9 watched, 10 half-way
        wl.change_watched(self.URL, 0, added=[15])
        wl.change_watched(self.URL, 0, removed=[15])
        part = wl.find(self.URL)["parts"][0]
        self.assertEqual({"10": 20.0}, part["progress"])
        self.assertEqual(10, wl.next_episode(wl.find(self.URL), self.downloads)[1])

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

    def test_app_scripts_are_bundled_and_installed_alone(self):
        """Progress tracking and "Skip opening" work without applying the config."""
        from unittest import mock
        from utils import mpvnet
        src = os.path.join(mpvnet.bundle_dir(), "scripts", mpvnet.PROGRESS_SCRIPT)
        self.assertTrue(os.path.isfile(src))
        cfg = os.path.join(self.tmp, "mpvcfg")
        with mock.patch.object(mpvnet, "find_mpvnet", return_value=(r"C:\x\mpvnet.exe", None)), \
                mock.patch.object(mpvnet, "config_dir", return_value=cfg):
            self.assertTrue(mpvnet.ensure_progress_script())
            self.assertEqual(os.path.join(cfg, mpvnet.PROGRESS_LOG), mpvnet.progress_log_path())
        self.assertEqual(["aed-progress.lua", "aed-skip.lua", "key-layouts.lua"],
                         sorted(os.listdir(os.path.join(cfg, "scripts"))))
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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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

    def _resume_file(self, folder, name, path, start, mtime):
        os.makedirs(folder, exist_ok=True)
        file = os.path.join(folder, name)
        with open(file, "w", encoding="utf-8") as f:
            f.write(f"# {path}\nstart={start}\n")
        os.utime(file, (mtime, mtime))

    def test_mpv_history_reads_libmpv_041s_state_folder_too(self):
        """mpv.net 7.1 (libmpv 0.41) keeps resume files in %LOCALAPPDATA%\\mpv\\
        watch_later; the config folder's watch_later stopped changing (measured:
        newest file 2026-10-02 there, today's in the new one). Both are read; the
        newest file for a path wins."""
        from unittest import mock
        from utils.library_scan import read_mpv_history, resume_dirs
        cfg = os.path.join(self.tmp, "mpvcfg")
        state = os.path.join(self.tmp, "state")
        os.makedirs(cfg)
        self._resume_file(os.path.join(cfg, "watch_later"), "A", "C:\\anime\\Show Ep1.mp4", 100, 1000)
        self._resume_file(os.path.join(cfg, "watch_later"), "B", "C:\\anime\\Show Ep2.mp4", 50, 1000)
        self._resume_file(os.path.join(state, "watch_later"), "B", "C:\\anime\\Show Ep2.mp4", 900, 2000)
        self._resume_file(os.path.join(state, "watch_later"), "C", "C:\\anime\\Show Ep3.mp4", 30, 2000)
        with mock.patch.dict(os.environ, {"AED_MPV_STATE_DIR": state}):
            self.assertEqual([os.path.join(state, "watch_later"), os.path.join(cfg, "watch_later")],
                             resume_dirs(cfg))
            h = read_mpv_history(cfg)
        norm = os.path.normcase
        self.assertEqual({norm("C:\\anime\\Show Ep1.mp4"): 100.0,      # only in the old folder
                          norm("C:\\anime\\Show Ep2.mp4"): 900.0,      # the newer file wins
                          norm("C:\\anime\\Show Ep3.mp4"): 30.0}, h)   # only in the new one

    def test_mpv_history_follows_watch_later_dir_in_mpv_conf(self):
        from unittest import mock
        from utils.library_scan import read_mpv_history, resume_dirs
        cfg = os.path.join(self.tmp, "mpvcfg")
        state = os.path.join(self.tmp, "state")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "mpv.conf"), "w", encoding="utf-8") as f:
            f.write("save-position-on-quit=yes\n# watch-later-dir=C:/nope\n"
                    "watch-later-directory=~~/old-name  # the option's old name\n"
                    "watch-later-dir=~~state/mine\n"
                    "[some-profile]\nwatch-later-dir=C:/only-in-a-profile\n")
        self._resume_file(os.path.join(state, "mine"), "A", "C:\\anime\\Show Ep7.mp4", 70, 1000)
        with mock.patch.dict(os.environ, {"AED_MPV_STATE_DIR": state}):
            dirs = resume_dirs(cfg)
            h = read_mpv_history(cfg)
        self.assertEqual(os.path.join(state, "mine"), dirs[0])        # the last top-level one
        self.assertNotIn("C:/only-in-a-profile", dirs)
        self.assertEqual(70.0, h[os.path.normcase("C:\\anime\\Show Ep7.mp4")])
        self.assertEqual(os.path.join(os.environ["LOCALAPPDATA"], "mpv")
                         if os.environ.get("LOCALAPPDATA") else "",
                         self._state_dir_without_override())

    def _state_dir_without_override(self):
        from unittest import mock
        from utils.library_scan import mpv_state_dir
        env = {k: v for k, v in os.environ.items() if k != "AED_MPV_STATE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            return mpv_state_dir()

    def test_tests_never_read_the_users_mpv_resume_files(self):
        from utils.library_scan import resume_dirs
        real = os.path.join(os.environ.get("LOCALAPPDATA", ""), "mpv")
        for folder in resume_dirs(os.path.join(self.tmp, "mpvcfg")):
            self.assertFalse(os.path.normcase(folder).startswith(os.path.normcase(real) + os.sep),
                             folder)

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
        self.addCleanup(shutil.rmtree, self.tmp, True)
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
        self.addCleanup(shutil.rmtree, tmp, True)
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
        self.addCleanup(shutil.rmtree, tmp, True)
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
        self.addCleanup(shutil.rmtree, tmp, True)
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
        self.addCleanup(shutil.rmtree, tmp, True)
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

    def test_dialog_names_what_next_plays_and_follows_unticks(self):
        """Ep 28 opened by mistake was saved as started, and Next kept playing it
        with no way to change that from this dialog (Frieren, 2026-10-23)."""
        wl = self.wl
        from ui.watch_later_tab import EntryDialog
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 28}])
        wl.link_profile(self.PAGE, 0, "Show X")
        wl.apply_progress([self._log("Show X", 28, 0.5)])         # the accidental jump
        dlg = EntryDialog(wl.find(self.PAGE), self.host)
        self.assertTrue(dlg.lbl_next.text().startswith("Up next: Ep 28, started"))
        grid = dlg._grids[0][1]
        grid.set_selected(range(1, 19))                            # untick 19-27
        self.assertTrue(dlg.next_text().startswith("Up next: Ep 19"))
        dlg.yesButton.click()
        nxt = wl.next_episode(wl.find(self.PAGE), self.downloads)
        self.assertEqual((0, 19), nxt[:2])
        self.assertEqual({}, wl.find(self.PAGE)["parts"][0]["progress"])

    def test_done_reads_unread_playback_before_saving(self):
        """Ep 28 played again, its log line not yet read. Read after Done, it re-ticked
        1-27 and Continue went back to 28; read before, the unticks win."""
        wl = self.wl
        from ui.watch_later_tab import EntryDialog
        wl.add("Show X", self.PAGE, "witanime.site")
        wl.set_parts(self.PAGE, [{"template": self.T1, "max_ep": 28}])
        wl.link_profile(self.PAGE, 0, "Show X")
        log = os.path.join(self.tmp, "aed-progress.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write(self._log("Show X", 28, 0.5) + "\n")
        wl.ingest_log(log)
        dlg = EntryDialog(wl.find(self.PAGE), self.host, catch_up=lambda: wl.ingest_log(log))
        with open(log, "a", encoding="utf-8") as f:              # played again meanwhile
            f.write(self._log("Show X", 28, 0.6) + "\n")
        dlg._grids[0][1].set_selected(range(1, 19))
        dlg.yesButton.click()
        self.assertFalse(wl.ingest_log(log))                     # nothing left to undo it
        part = wl.find(self.PAGE)["parts"][0]
        self.assertEqual(list(range(1, 19)), part["watched"])
        self.assertEqual(19, wl.next_episode(wl.find(self.PAGE), self.downloads)[1])

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


class LibraryShortcutTests(unittest.TestCase):
    """The key combination that opens the app on the Library (utils/shortcut.py,
    utils/instance.py, AppWindow open_library / show_library)."""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def test_only_combinations_a_windows_shortcut_can_hold(self):
        from utils.shortcut import normalize, to_win32, to_lnk_hotkey
        self.assertEqual("Ctrl+Alt+L", normalize("alt+ctrl+l"))
        self.assertEqual("Ctrl+Shift+F5", normalize("Shift+Ctrl+F5"))
        self.assertEqual("Ctrl+Alt+Space", normalize("Ctrl+Alt+space"))
        self.assertEqual("Ctrl+Alt+Shift+F20", normalize("Ctrl+Alt+Shift+F20"))
        self.assertEqual("Ctrl+Alt+Num+", normalize("Ctrl+Alt+Num+"))
        self.assertEqual("Alt+Shift+;", normalize("Alt+Shift+;"))
        self.assertEqual((0x2 | 0x1, ord("L")), to_win32("Ctrl+Alt+L"))
        self.assertEqual((0x2 | 0x4, 0x74), to_win32("Ctrl+Shift+F5"))
        self.assertEqual((0x2 | 0x1, 0x6B), to_win32("Ctrl+Alt+Num+"))
        # .lnk header: HOTKEYF_CONTROL|ALT = 0x06 in the high byte.
        self.assertEqual(0x0600 | ord("L"), to_lnk_hotkey("Ctrl+Alt+L"))
        self.assertEqual(0x0300 | 0x20, to_lnk_hotkey("Ctrl+Shift+Space"))
        # One modifier is allowed (it only comes with a warning, see below).
        self.assertEqual("Ctrl+L", normalize("ctrl+l"))
        self.assertEqual("Alt+7", normalize("Alt+7"))
        self.assertEqual("Shift+F5", normalize("Shift+F5"))
        self.assertEqual((0x4, 0x74), to_win32("Shift+F5"))
        self.assertEqual(0x0100 | 0x74, to_lnk_hotkey("Shift+F5"))
        for bad in ("", "L", "F5", "Meta+L", "Ctrl+Alt", "Ctrl+A+B",
                    "Alt+Tab", "Alt+Shift+Tab", "Ctrl+Shift+Escape", "Ctrl+Alt+م"):
            with self.assertRaises(ValueError, msg=bad):
                normalize(bad)

    def test_one_modifier_combinations_warn_what_they_take_away(self):
        from utils.shortcut import app_warnings, conflicts
        none = set()
        self.assertIn("couldn't type it anywhere", app_warnings("Shift+L", running=none)[0])
        self.assertIn("Copy", app_warnings("Ctrl+C", running=none)[0])
        self.assertIn("menu", app_warnings("Alt+F", running=none)[0])
        self.assertIn("Some apps use Ctrl+F7", app_warnings("Ctrl+F7", running=none)[0])
        self.assertEqual([], app_warnings("Ctrl+F22", running=none))     # no keyboard has it
        self.assertEqual([], app_warnings("Ctrl+Alt+K", running=none))
        self.assertIn("closing windows", conflicts("Alt+F4")[0])          # Windows owns it

    def test_keys_are_read_by_position_not_by_the_character_typed(self):
        from utils.shortcut import from_vk
        # Ctrl+Alt+L on an Arabic layout types "م"; its virtual key is still L.
        self.assertEqual("Ctrl+Alt+L", from_vk(True, True, False, ord("L")))
        self.assertEqual("Ctrl+Alt+Space", from_vk(True, True, False, 0x20))
        self.assertEqual("Ctrl+Shift+Left", from_vk(True, False, True, 0x25))
        self.assertEqual("Ctrl+Alt+Num5", from_vk(True, True, False, 0x65))
        self.assertEqual("Ctrl+Alt+F13", from_vk(True, True, False, 0x7C))
        self.assertEqual("Ctrl+Alt+/", from_vk(True, True, False, 0xBF))
        with self.assertRaises(ValueError):
            from_vk(True, True, False, 0xAD)                # volume mute: not a shortcut key

    def test_conflicts_name_what_already_uses_the_keys(self):
        from unittest import mock
        from utils import shortcut
        tmp = tempfile.mkdtemp(prefix="aed_lnk_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        other = os.path.join(tmp, "Notepad.lnk")
        header = bytearray(0x4C)
        header[:4] = b"\x4c\x00\x00\x00"
        value = shortcut.to_lnk_hotkey("Ctrl+Alt+N")
        header[0x40], header[0x41] = value & 0xFF, value >> 8
        with open(other, "wb") as f:
            f.write(bytes(header))
        self.assertEqual(value, shortcut.read_lnk_hotkey(other))
        ours = os.path.join(tmp, "ours")
        os.makedirs(ours)
        with mock.patch.object(shortcut, "_shortcut_folders", return_value=[tmp]), \
                mock.patch.object(shortcut, "shortcut_path",
                                  side_effect=lambda action="library": os.path.join(ours, f"{action}.lnk")), \
                mock.patch.object(shortcut, "is_taken", side_effect=lambda hk: hk == "Ctrl+Alt+T"):
            self.assertEqual(["The shortcut “Notepad” already uses Ctrl+Alt+N."],
                             shortcut.conflicts("Ctrl+Alt+N"))
            self.assertIn("Windows already uses", shortcut.conflicts("Ctrl+Alt+Delete")[0])
            self.assertEqual(["Another program has already claimed Ctrl+Alt+T."],
                             shortcut.conflicts("Ctrl+Alt+T"))
            self.assertEqual([], shortcut.conflicts("Ctrl+Alt+T", current="Ctrl+Alt+T"))  # ours
            self.assertEqual([], shortcut.conflicts("Ctrl+Alt+L"))
            # Our other action's shortcut is named as such, not as "a program".
            value = shortcut.to_lnk_hotkey("Ctrl+Alt+P")
            header[0x40], header[0x41] = value & 0xFF, value >> 8
            with open(os.path.join(ours, "play_next.lnk"), "wb") as f:
                f.write(bytes(header))
            self.assertEqual(["Your “Continue watching” shortcut already uses Ctrl+Alt+P."],
                             shortcut.conflicts("Ctrl+Alt+P", action="library"))
            self.assertEqual([], shortcut.conflicts("Ctrl+Alt+P", action="play_next"))
        self.assertEqual(["AMD Radeon Software uses Ctrl+Shift+E for start/stop recording by default."],
                         shortcut.app_warnings("Ctrl+Shift+E", running={"radeonsoftware.exe"}))
        self.assertEqual([], shortcut.app_warnings("Ctrl+Shift+E", running=set()))

    def test_shell_link_saves_any_key_and_the_target(self):
        """Written by Windows' ShellLink object (what Explorer registers hotkeys from)."""
        if sys.platform != "win32":
            self.skipTest("Windows only")
        from utils import shortcut
        tmp = tempfile.mkdtemp(prefix="aed_lnk_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "x.lnk")
        cmd = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
        for hk in ("Ctrl+Alt+Space", "Ctrl+Shift+Num5", "Ctrl+Alt+Shift+F22", "Alt+Shift+/"):
            shortcut._save_shell_link(path, cmd, "--library", tmp, f"{cmd},0", hk)
            self.assertEqual(shortcut.to_lnk_hotkey(hk), shortcut.read_lnk_hotkey(path), hk)
        with open(path, "rb") as f:
            data = f.read()
        self.assertIn("--library".encode("utf-16-le"), data)
        bad = os.path.join(tmp, "bad.lnk")
        with open(bad, "wb") as f:
            f.write(b"not a link" * 10)
        self.assertEqual(0, shortcut.read_lnk_hotkey(bad))

    def test_shortcut_launches_this_copy_with_library(self):
        from utils import shortcut
        program, args, cwd, _icon = shortcut.launch_target()
        self.assertTrue(args.endswith("--library"))
        self.assertTrue(os.path.isdir(cwd))
        self.assertEqual(shortcut.SHORTCUT_NAME, os.path.basename(shortcut.shortcut_path()))
        self.assertEqual(f"{program}|{args}", shortcut.target_signature())
        from unittest import mock
        # Outside the tests (patch.dict puts the variable back afterwards):
        with mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\x\AppData\Roaming"}):
            os.environ.pop("AED_SHORTCUT_DIR")
            self.assertEqual(os.path.join(r"C:\Users\x\AppData\Roaming", "Microsoft", "Windows",
                                          "Start Menu", "Programs", shortcut.SHORTCUT_NAME),
                             shortcut.shortcut_path())

    def test_refresh_rewrites_only_a_stale_shortcut(self):
        from unittest import mock
        from utils import shortcut
        self.assertFalse(shortcut.refresh({}))                       # no hotkey set
        with mock.patch.object(shortcut, "install", return_value=(True, "")) as install, \
                mock.patch("os.path.exists", return_value=True):
            fresh = {shortcut.SETTING: "Ctrl+Alt+L",
                     shortcut.TARGET_SETTING: shortcut.target_signature()}
            self.assertFalse(shortcut.refresh(fresh))
            install.assert_not_called()
            moved = {shortcut.SETTING: "Ctrl+Alt+L", shortcut.TARGET_SETTING: "C:\\old.exe|--library"}
            self.assertTrue(shortcut.refresh(moved))
            self.assertEqual(shortcut.target_signature(), moved[shortcut.TARGET_SETTING])

    def test_play_next_shortcut_launches_with_its_own_argument(self):
        from utils import shortcut
        program, args, _cwd, _icon = shortcut.launch_target("play_next")
        self.assertTrue(args.endswith("--play-next"))
        self.assertTrue(shortcut.shortcut_path("play_next").endswith(
            "Auto Episodes Downloader - Continue watching.lnk"))
        self.assertNotEqual(shortcut.shortcut_path("library"), shortcut.shortcut_path("play_next"))

    def _health_env(self, files, taken):
        """shortcut.health against fake files {action: hotkey} and a fake 'taken' set."""
        from unittest import mock
        from utils import shortcut
        tmp = tempfile.mkdtemp(prefix="aed_health_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        installs = []

        def write(action, hotkey):
            header = bytearray(0x4C)
            header[:4] = b"\x4c\x00\x00\x00"
            value = shortcut.to_lnk_hotkey(hotkey)
            header[0x40], header[0x41] = value & 0xFF, value >> 8
            with open(os.path.join(tmp, f"{action}.lnk"), "wb") as f:
                f.write(bytes(header))

        def install(hotkey, wait_for_explorer=3.0, action="library"):
            installs.append((action, hotkey))
            write(action, hotkey)
            return True, "ok"
        for action, hotkey in files.items():
            write(action, hotkey)
        patches = [mock.patch.object(shortcut, "shortcut_path",
                                     side_effect=lambda action="library": os.path.join(tmp, f"{action}.lnk")),
                   mock.patch.object(shortcut, "install", side_effect=install),
                   mock.patch.object(shortcut, "is_taken", side_effect=lambda hk: hk in taken),
                   mock.patch.object(shortcut.sys, "platform", "win32")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return installs

    def test_health_check_repairs_and_reports(self):
        from utils import shortcut
        sig = shortcut.target_signature
        # Healthy: in place, launches this copy, Windows holds the key -> nothing done.
        installs = self._health_env({"library": "Ctrl+Alt+L"}, taken={"Ctrl+Alt+L"})
        settings = {"library_hotkey": "Ctrl+Alt+L", "library_hotkey_target": sig("library")}
        self.assertEqual((False, []), shortcut.health(settings, wait=0))
        self.assertEqual([], installs)

    def test_health_check_follows_the_file_and_rewrites_what_slipped(self):
        from utils import shortcut
        sig = shortcut.target_signature
        installs = self._health_env({"library": "Ctrl+Alt+K"}, taken=set())
        settings = {"library_hotkey": "Ctrl+Alt+L", "library_hotkey_target": sig("library"),
                    "play_next_hotkey": "Ctrl+Alt+P"}                  # its file is gone
        changed, warnings = shortcut.health(settings, wait=0)
        self.assertTrue(changed)
        self.assertEqual("Ctrl+Alt+K", settings["library_hotkey"])     # the file wins
        # The Library key isn't held by Windows -> rewritten; Play next re-created.
        self.assertEqual([("library", "Ctrl+Alt+K"), ("play_next", "Ctrl+Alt+P")], installs)
        self.assertEqual(sig("play_next"), settings["play_next_hotkey_target"])
        # Still not held after rewriting -> the user is told what to do.
        self.assertEqual(2, len(warnings))
        self.assertIn("Sign out of Windows and back in", warnings[0])

    def _install_env(self, drops):
        """install() against a fake Explorer: the n-th save of the link is
        registered, then dropped after `drops[n]` checks (None = kept)."""
        from types import SimpleNamespace
        from unittest import mock
        from utils import shortcut
        tmp = tempfile.mkdtemp(prefix="aed_inst_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        clock = SimpleNamespace(now=0.0, slept=0.0)
        state = SimpleNamespace(saves=0, held=None, checks=0)

        def save(path, *args):
            hotkey = args[4]
            header = bytearray(0x4C)
            header[:4] = b"\x4c\x00\x00\x00"
            value = shortcut.to_lnk_hotkey(hotkey)
            header[0x40], header[0x41] = value & 0xFF, value >> 8
            with open(path, "wb") as f:
                f.write(bytes(header))
            state.held, state.checks = hotkey, 0
            state.saves += 1

        def is_taken(hotkey):
            if hotkey != state.held:
                return False
            state.checks += 1
            limit = drops[min(state.saves, len(drops)) - 1]
            return limit is None or state.checks <= limit

        def sleep(seconds):
            clock.now += seconds
            clock.slept += seconds
        patches = [mock.patch.dict(os.environ, {"AED_SHORTCUT_DIR": tmp}),
                   mock.patch.object(shortcut, "_save_shell_link", side_effect=save),
                   mock.patch.object(shortcut, "_notify_shell"),
                   mock.patch.object(shortcut, "is_taken", side_effect=is_taken),
                   mock.patch.object(shortcut, "time", SimpleNamespace(
                       time=lambda: clock.now, sleep=sleep)),
                   mock.patch.object(shortcut.sys, "platform", "win32")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return shortcut, state, clock

    def test_a_key_explorer_drops_right_away_is_saved_again(self):
        """Measured after the rename from "Play next": Explorer registered the new
        key, then dropped it ~0.1 s later (a delete it handled late). install()
        now checks the key stays held, and saves again after a pause if not."""
        shortcut, state, clock = self._install_env(drops=[2, None])
        ok, message = shortcut.install("Ctrl+Alt+M", action="play_next")
        self.assertTrue(ok)
        self.assertIn("continues what you're watching", message)
        self.assertEqual(2, state.saves)                       # the retry
        self.assertGreaterEqual(clock.slept, shortcut.SETTLE_SECONDS)

    def test_a_key_explorer_never_keeps_ends_in_the_sign_out_advice(self):
        shortcut, state, _clock = self._install_env(drops=[1, 1, 1])
        ok, message = shortcut.install("Ctrl+Alt+M", action="play_next")
        self.assertTrue(ok)                                    # the file is right
        self.assertIn("sign out of Windows", message)
        self.assertEqual(shortcut.SAVE_ATTEMPTS, state.saves)

    def test_a_kept_key_is_saved_once(self):
        shortcut, state, _clock = self._install_env(drops=[None])
        self.assertTrue(shortcut.install("Ctrl+Alt+M", action="play_next")[0])
        self.assertEqual(1, state.saves)

    def test_an_old_play_next_shortcut_moves_to_continue_watching(self):
        """The action was renamed; a shortcut saved as "... - Play next.lnk" is
        deleted and written again under the new name with the same key (a renamed
        .lnk would never have its key registered)."""
        from utils import shortcut
        installs = self._health_env({}, taken=set())
        folder = os.path.dirname(shortcut.shortcut_path("play_next"))
        old = os.path.join(folder, "Auto Episodes Downloader - Play next.lnk")
        header = bytearray(0x4C)
        header[:4] = b"\x4c\x00\x00\x00"
        value = shortcut.to_lnk_hotkey("Ctrl+Alt+N")
        header[0x40], header[0x41] = value & 0xFF, value >> 8
        with open(old, "wb") as f:
            f.write(bytes(header))
        settings = {"play_next_hotkey": "Ctrl+Alt+N",
                    "play_next_hotkey_target": shortcut.target_signature("play_next")}
        changed, _warnings = shortcut.health(settings, wait=0)
        self.assertTrue(changed)
        self.assertFalse(os.path.exists(old))
        self.assertEqual([("play_next", "Ctrl+Alt+N")], installs)    # under the new name
        self.assertTrue(os.path.exists(shortcut.shortcut_path("play_next")))
        self.assertEqual("Ctrl+Alt+N", settings["play_next_hotkey"])
        self.assertEqual(shortcut.target_signature("play_next"), settings["play_next_hotkey_target"])

    def _window(self, **kw):
        from unittest import mock
        from utils import watch_later as wl
        from ui.app_window import AppWindow
        tmp = tempfile.mkdtemp(prefix="aed_short_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        for p in (mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json")),
                  mock.patch("ui.app_window.get_watchlist", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        w = AppWindow(**kw)

        def dispose():
            w._tabs_built = True
            w.hide()
            w.deleteLater()
        self.addCleanup(dispose)
        return w

    def test_started_by_the_shortcut_the_library_comes_first(self):
        w = self._window(open_library=True)
        lib = w.watch_later_interface
        self.assertIsNotNone(lib)
        self.assertIs(lib, w.stackedWidget.currentWidget())
        self.assertIsNone(w.search_interface)                       # the rest still deferred
        w._build_tabs()
        self.assertIs(lib, w.watch_later_interface)                 # not built twice
        self.assertEqual(1, sum(1 for i in range(w.stackedWidget.view.count())
                                if w.stackedWidget.view.widget(i) is lib))
        lib.search_requested.emit()
        self.assertIs(w.search_interface, w.stackedWidget.currentWidget())

    def test_the_hand_over_message_shows_the_library(self):
        import ctypes.wintypes
        from PyQt6.QtCore import QByteArray
        w = self._window()
        w.raise_to_front = lambda: None
        self.assertIs(w.downloader_interface, w.stackedWidget.currentWidget())
        msg = ctypes.wintypes.MSG()
        msg.message = w._open_library_msg
        handled = w.nativeEvent(QByteArray(b"windows_generic_MSG"), ctypes.addressof(msg))
        self.assertEqual((True, 0), tuple(handled))
        self._app.processEvents()
        self.assertIs(w.watch_later_interface, w.stackedWidget.currentWidget())
        # During a download the window only comes forward.
        w.switchTo(w.downloader_interface)
        w._download_running = True
        w.show_library()
        self.assertIs(w.downloader_interface, w.stackedWidget.currentWidget())

    def test_library_tab_has_the_shortcut_button(self):
        from unittest import mock
        from utils import watch_later as wl
        from ui import watch_later_tab as tab
        tmp = tempfile.mkdtemp(prefix="aed_short_")
        import shutil
        self.addCleanup(shutil.rmtree, tmp, True)
        from utils import shortcut
        lnk = os.path.join(tmp, "library.lnk")              # never this PC's real Start menu
        with mock.patch.object(wl, "FILE", os.path.join(tmp, "watch_later.json")), \
                mock.patch.object(shortcut, "shortcut_path",
                                  side_effect=lambda action="library": os.path.join(tmp, f"{action}.lnk")), \
                mock.patch.object(tab, "save_config"), \
                mock.patch.dict(tab.app_settings, {"library_hotkey": "Ctrl+Alt+L"}):
            w = tab.WatchLaterWidget()
            self.addCleanup(w.deleteLater)
            self.assertEqual("Shortcut: Ctrl+Alt+L", w.btn_shortcut.text())   # from the setting
            tab.app_settings["play_next_hotkey"] = "Ctrl+Alt+P"
            w._update_shortcut_button()
            self.assertEqual("Shortcuts: Ctrl+Alt+L, Ctrl+Alt+P", w.btn_shortcut.text())
            tab.app_settings.pop("library_hotkey")
            tab.app_settings.pop("play_next_hotkey")
            w._update_shortcut_button()
            self.assertEqual("Add shortcuts", w.btn_shortcut.text())
            # The shortcut file is what Windows uses: it wins over a lost setting.
            header = bytearray(0x4C)
            header[:4] = b"\x4c\x00\x00\x00"
            value = shortcut.to_lnk_hotkey("Ctrl+Alt+K")
            header[0x40], header[0x41] = value & 0xFF, value >> 8
            with open(lnk, "wb") as f:
                f.write(bytes(header))
            w._update_shortcut_button()
            self.assertEqual("Shortcut: Ctrl+Alt+K", w.btn_shortcut.text())
            self.assertEqual("Ctrl+Alt+K", tab.app_settings.get("library_hotkey"))   # synced
            self.assertEqual([], shortcut.conflicts("Ctrl+Alt+K"))   # our own: no conflict

    @staticmethod
    def _press(widget, vk, ctrl=True, alt=True, shift=False, key=0, text=""):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QApplication
        mods = Qt.KeyboardModifier.NoModifier
        for on, m in ((ctrl, Qt.KeyboardModifier.ControlModifier),
                      (alt, Qt.KeyboardModifier.AltModifier),
                      (shift, Qt.KeyboardModifier.ShiftModifier)):
            if on:
                mods |= m
        ev = QKeyEvent(QEvent.Type.KeyPress, key, mods, 0, vk, 0, text)
        QApplication.sendEvent(widget, ev)

    def test_dialog_reads_any_layout_and_flags_conflicts(self):
        from unittest import mock
        from PyQt6.QtWidgets import QWidget
        from ui import watch_later_tab as tab
        host = QWidget()
        host.show()
        self.addCleanup(host.close)
        with mock.patch("utils.shortcut.conflicts",
                        side_effect=lambda hk, cur="", action="library": [
                            "Another program has already claimed Ctrl+Alt+T."]
                        if hk == "Ctrl+Alt+T" else []), \
                mock.patch("utils.shortcut.app_warnings", return_value=[]), \
                mock.patch.object(tab._HotkeyProbe, "hold", return_value=True):
            dlg = tab.ShortcutDialog({}, host)
            lib, nxt = dlg.row("library"), dlg.row("play_next")
            dlg.show()
            lib.keys.setFocus()
            self.assertFalse(dlg.yesButton.isEnabled())
            self._press(lib.keys, ord("L"), ctrl=False, alt=False)
            self.assertFalse(dlg.yesButton.isEnabled())             # needs a modifier
            self._press(lib.keys, ord("T"))
            self.assertFalse(dlg.yesButton.isEnabled())             # taken
            self.assertIn("already claimed", lib.lbl_check.text())
            # Arabic layout: Qt's key is the typed letter, the virtual key is L.
            self._press(lib.keys, ord("L"), key=0x0645, text="م")
            self.assertEqual("Ctrl+Alt+L", lib.keys.text())
            self.assertTrue(dlg.yesButton.isEnabled())
            self.assertIn("Press Ctrl+Alt+L now to test it", lib.lbl_test.text())
            dlg._on_test_press()                                   # the WM_HOTKEY arrived
            self.assertIn("Tested", lib.lbl_test.text())
            self._press(lib.keys, 0x09, ctrl=True, alt=True)        # Tab: captured, not focus
            self.assertIn("belongs to Windows", lib.lbl_check.text())
            self._press(lib.keys, ord("L"), ctrl=True, alt=False)   # one modifier is allowed
            self.assertEqual("Ctrl+L", lib.keys.text())
            self.assertTrue(dlg.yesButton.isEnabled())
            # The same keys for both actions is refused.
            self._press(nxt.keys, ord("L"), ctrl=True, alt=False)
            self.assertIn("for “Open the Library” too", nxt.lbl_check.text())
            self.assertFalse(dlg.yesButton.isEnabled())
            self._press(nxt.keys, ord("P"))                          # Ctrl+Alt+P
            self.assertTrue(dlg.yesButton.isEnabled())
            dlg.yesButton.click()
            self.assertEqual({"library": "Ctrl+L", "play_next": "Ctrl+Alt+P"}, dlg.results)
            # Unchanged: nothing to save; Remove applies on Save.
            dlg = tab.ShortcutDialog({"library": "Ctrl+Alt+L"}, host)
            self.assertFalse(dlg.yesButton.isEnabled())
            self.assertFalse(dlg.row("play_next").btn_remove.isVisibleTo(dlg.row("play_next")))
            dlg.row("library").btn_remove.click()
            self.assertTrue(dlg.yesButton.isEnabled())
            dlg.yesButton.click()
            self.assertEqual({"library": ""}, dlg.results)

    @staticmethod
    def _key(widget, kind, vk, mods=(), key=0, repeat=False):
        from PyQt6.QtCore import QEvent, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QApplication
        flags = Qt.KeyboardModifier.NoModifier
        for m in mods:
            flags |= {"Ctrl": Qt.KeyboardModifier.ControlModifier,
                      "Alt": Qt.KeyboardModifier.AltModifier,
                      "Shift": Qt.KeyboardModifier.ShiftModifier}[m]
        t = QEvent.Type.KeyPress if kind == "down" else QEvent.Type.KeyRelease
        QApplication.sendEvent(widget, QKeyEvent(t, key, flags, 0, vk, 0, "", repeat))

    def test_the_keyboard_hook_takes_only_combinations(self):
        """While the picker has focus: Ctrl / Alt / Shift combinations are swallowed
        and handed to it (so another program's hotkey can't fire); plain keys pass."""
        if sys.platform != "win32":
            self.skipTest("Windows only")
        from unittest import mock
        from utils import keyhook
        got = []
        hook = keyhook.ComboHook(lambda *a: got.append(a),
                                 pass_through=lambda c, a, s, vk: vk == ord("T"))
        hook._u = mock.MagicMock()
        hook._u.CallNextHookEx.return_value = 0
        held = set()
        hook._u.GetAsyncKeyState.side_effect = lambda vk: -32768 if vk in held else 0

        def send(msg, vk):
            kb = keyhook.KBDLLHOOKSTRUCT(vkCode=vk)
            return hook._handle(0, msg, ctypes.addressof(kb))
        import ctypes
        D, U = keyhook.WM_KEYDOWN, keyhook.WM_KEYUP
        self.assertEqual(0, send(D, 0x11))                  # modifiers pass
        held |= {0x11, 0x12}
        self.assertEqual(1, send(D, ord("L")))              # Ctrl+Alt+L: taken
        self.assertEqual(1, send(D, ord("L")))              # its auto-repeat too
        self.assertEqual(1, send(U, ord("L")))              # and its key-up
        self.assertEqual([(True, True, False, ord("L"))], got)
        self.assertEqual(0, send(D, ord("T")))              # the live-test combo goes on
        held.clear()
        self.assertEqual(0, send(D, ord("L")))              # plain typing untouched
        held.add(0x10)
        self.assertEqual(1, send(D, ord("K")))              # Shift+K: a combination too
        self.assertEqual([(False, False, True, ord("K"))], got[1:])

    def test_key_box_follows_the_keys_as_they_are_held(self):
        from ui.watch_later_tab import KeyCaptureEdit
        box = KeyCaptureEdit("")
        self.addCleanup(box.deleteLater)
        got = []
        box.captured.connect(lambda combo, err: got.append((combo, err)))
        self.assertEqual(KeyCaptureEdit.PLACEHOLDER, box.text())
        self.assertIn("color: #7a7a7a", box.styleSheet())                # dimmed placeholder
        CTRL, ALT, SHIFT = 0x11, 0x12, 0x10
        self._key(box, "down", CTRL, ["Ctrl"])
        self.assertEqual("Ctrl+…", box.text())
        self._key(box, "down", ALT, ["Ctrl", "Alt"])
        self.assertEqual("Ctrl+Alt+…", box.text())
        self._key(box, "up", ALT, ["Ctrl"])                               # let go of Alt
        self.assertEqual("Ctrl+…", box.text())
        self._key(box, "down", ALT, ["Ctrl", "Alt"])
        self._key(box, "down", ALT, ["Ctrl", "Alt"], repeat=True)         # held: auto-repeat
        self.assertEqual("Ctrl+Alt+…", box.text())
        self._key(box, "down", ord("L"), ["Ctrl", "Alt"])
        self.assertEqual("Ctrl+Alt+L", box.text())
        self.assertEqual([("Ctrl+Alt+L", "")], got)
        self.assertIn("color: #ffffff", box.styleSheet())
        for vk, left in ((ord("L"), ["Ctrl", "Alt"]), (ALT, ["Ctrl"]), (CTRL, [])):
            self._key(box, "up", vk, left)
        self.assertEqual("Ctrl+Alt+L", box.text())                       # kept after letting go
        self._key(box, "down", SHIFT, ["Shift"])                          # a new try, abandoned
        self.assertEqual("Shift+…", box.text())
        self._key(box, "up", SHIFT, [])
        self.assertEqual("Ctrl+Alt+L", box.text())                       # back to the kept one
        self._key(box, "down", SHIFT, ["Shift"])
        self._key(box, "down", 0x09, ["Shift"])                           # Shift+Tab: Windows'
        self.assertEqual("Shift+Tab", box.text())
        self.assertIn("color: #ff8a80", box.styleSheet())
        self.assertEqual("", got[-1][0])
        self.assertTrue(got[-1][1])
        self._key(box, "down", ord("K"), ["Shift"])                       # Shift+K: allowed
        self.assertEqual(("Shift+K", ""), got[-1])

    def test_live_test_catches_the_real_key_press(self):
        """The probe really registers the keys and sees the press (Windows only)."""
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import ctypes
        import time
        from PyQt6.QtWidgets import QApplication
        from utils.shortcut import is_taken, to_win32
        from ui.watch_later_tab import _HotkeyProbe
        hotkey = "Ctrl+Alt+Shift+F22"                  # no keyboard has F22: nothing else uses it
        if is_taken(hotkey):
            self.skipTest(f"{hotkey} is taken on this PC")
        hits = []
        probe = _HotkeyProbe(lambda: hits.append(1))
        self.assertTrue(probe.hold(hotkey))
        self.addCleanup(probe.release)
        self.assertTrue(is_taken(hotkey))                         # held while testing
        mods, vk = to_win32(hotkey)
        u = ctypes.windll.user32
        for k in (0x11, 0x12, 0x10):
            u.keybd_event(k, 0, 0, 0)
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, 2, 0)
        for k in (0x10, 0x12, 0x11):
            u.keybd_event(k, 0, 2, 0)
        end = time.time() + 3
        while not hits and time.time() < end:
            QApplication.processEvents()
            time.sleep(0.02)
        self.assertEqual([1], hits[:1])
        probe.release()
        self.assertFalse(is_taken(hotkey))                        # let go afterwards


class QuickPlayTests(unittest.TestCase):
    """The Play next episode shortcut (utils/quick_play.py): no window, straight
    to mpv.net with the episode the Continue watching strip would show first."""

    T = "https://witanime.site/episode/{slug}-الحلقة-{{x}}/"

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl
        self.wl = wl
        self.tmp = tempfile.mkdtemp(prefix="aed_qp_")
        import shutil
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        p.start()
        self.addCleanup(p.stop)
        self.downloads = os.path.join(self.tmp, "animes")

    def _anime(self, title, eps_on_disk, watched, status, played_ts=None, max_ep=12):
        wl = self.wl
        url = f"https://witanime.site/anime/{title.lower().replace(' ', '-')}/"
        wl.add(title, url, "witanime.site")
        wl.set_parts(url, [{"template": self.T.format(slug=title.lower()), "max_ep": max_ep}])
        wl.link_profile(url, 0, title)
        wl.set_watched(url, 0, watched)
        wl.update(url, status=status,
                  last_played={"profile": title, "ep": 1, "ts": played_ts} if played_ts else None)
        folder = os.path.join(self.downloads, title)
        os.makedirs(folder, exist_ok=True)
        for ep in eps_on_disk:
            open(os.path.join(folder, f"{title} Ep{ep}.mp4"), "wb").close()
        return url

    def test_picks_the_most_recently_played_with_its_next_episode_on_disk(self):
        from utils.quick_play import pick_next, decide
        wl = self.wl
        self._anime("Old Show", [3, 4], [1, 2], wl.WATCHING, played_ts=100)
        self._anime("Recent Show", [5, 6, 7], [1, 2, 3, 4], wl.WATCHING, played_ts=200)
        self._anime("All Watched", [1, 2], [1, 2], wl.WATCHING, played_ts=300, max_ep=2)
        self._anime("Saved For Later", [1], [], wl.LATER, played_ts=400)    # not being watched
        entry, part, ep, files = pick_next(wl.entries(), self.downloads)
        # Nothing left of "All Watched": the next most recent one plays.
        self.assertEqual(("Recent Show", 0, 5), (entry["title"], part, ep))
        self.assertEqual(["Recent Show Ep5.mp4", "Recent Show Ep6.mp4", "Recent Show Ep7.mp4"],
                         [os.path.basename(f) for f in files])               # the rest queued
        self.assertIsNone(pick_next([], self.downloads))
        self.assertIsNone(decide([], self.downloads))

    def test_a_missing_next_episode_is_downloaded_not_swapped_for_another_anime(self):
        """You watched A Ep3, then B Ep5. B Ep6 isn't on disk: Play next doesn't
        quietly play A Ep4 -- B's episodes from 6 are to be downloaded."""
        from utils.quick_play import decide, pick_next
        wl = self.wl
        self._anime("Show A", [4, 5], [1, 2, 3], wl.WATCHING, played_ts=100)
        self._anime("Show B", [5, 9], [1, 2, 3, 4, 5], wl.WATCHING, played_ts=200)
        kind, entry, part, ep = decide(wl.entries(), self.downloads)
        self.assertEqual(("download", "Show B", 0, 6), (kind, entry["title"], part, ep))
        self.assertIsNone(pick_next(wl.entries(), self.downloads))
        # The Downloader's range: from the missing one to the last, skipping
        # what's already on disk.
        self.assertEqual([6, 7, 8, 10, 11, 12],
                         wl.missing_from(entry, part, ep, self.downloads))

    def test_an_anime_that_cant_be_downloaded_is_passed_over(self):
        from utils.quick_play import decide
        wl = self.wl
        self._anime("Show A", [4], [1, 2, 3], wl.WATCHING, played_ts=100)
        url = self._anime("Folder Only", [], [1], wl.WATCHING, played_ts=200)
        wl.set_parts(url, [{"template": "", "max_ep": 12}])                 # no site page
        wl.link_profile(url, 0, "Folder Only")
        kind, entry, _part, ep = decide(wl.entries(), self.downloads)[:4]
        self.assertEqual(("play", "Show A", 4), (kind, entry["title"], ep))

    def test_play_next_plays_asks_for_a_download_or_says_there_is_nothing(self):
        from unittest import mock
        from utils import quick_play
        wl = self.wl
        settings = {"download_dir": self.downloads, "play_in_mpvnet": True}
        with mock.patch.object(quick_play, "_settings", return_value=settings), \
                mock.patch("utils.mpvnet.find_mpvnet", return_value=(r"C:\mpv\mpvnet.exe", "7")), \
                mock.patch.object(quick_play, "catch_up") as catch_up, \
                mock.patch.object(quick_play.subprocess, "Popen") as popen:
            status, message, request = quick_play.play_next()
            self.assertEqual(("nothing", None), (status, request))
            popen.assert_not_called()
            catch_up.assert_called_with(r"C:\mpv\mpvnet.exe")      # the log first, every time

            url = self._anime("Recent Show", [5, 6], [1, 2, 3, 4], wl.WATCHING, played_ts=200)
            status, message, request = quick_play.play_next()
            self.assertEqual("played", status)
            args = popen.call_args[0][0]
            self.assertEqual(r"C:\mpv\mpvnet.exe", args[0])
            self.assertEqual(["Recent Show Ep5.mp4", "Recent Show Ep6.mp4"],
                             [os.path.basename(a) for a in args[1:]])
            self.assertIn("episode 5", message)

            popen.reset_mock()
            wl.set_watched(url, 0, [1, 2, 3, 4, 5, 6])
            status, message, request = quick_play.play_next()
            self.assertEqual("download", status)
            popen.assert_not_called()
            self.assertEqual({"url": url, "part": 0, "ep": 7, "title": "Recent Show"}, request)
            self.assertIn("episode 7 isn't downloaded", message)

    def test_play_next_catches_up_on_what_mpvnet_logged_first(self):
        """The gap: the progress log was only read while the Library tab was open,
        so after watching with the app closed Play next picked from stale data."""
        from unittest import mock
        from utils import quick_play
        wl = self.wl
        self._anime("Show A", [4, 5], [1, 2, 3], wl.WATCHING, played_ts=100)
        self._anime("Show B", [5, 6], [1, 2, 3, 4], wl.WATCHING, played_ts=50)
        mpv_dir = os.path.join(self.tmp, "mpvnet")
        os.makedirs(mpv_dir)
        # Watched B Ep5 to the end since the Library last looked.
        b5 = os.path.join(self.downloads, "Show B", "Show B Ep5.mp4")
        with open(os.path.join(mpv_dir, "aed-progress.log"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"path": b5, "percent": 100, "eof": True}) + "\n")
        settings = {"download_dir": self.downloads, "play_in_mpvnet": True}
        with mock.patch.object(quick_play, "_settings", return_value=settings), \
                mock.patch("utils.mpvnet.find_mpvnet", return_value=(r"C:\mpv\mpvnet.exe", "7")), \
                mock.patch("utils.mpvnet.config_dir", return_value=mpv_dir), \
                mock.patch.object(quick_play.subprocess, "Popen") as popen:
            status, message, _request = quick_play.play_next()
        self.assertEqual("played", status)
        self.assertEqual("Show B Ep6.mp4", os.path.basename(popen.call_args[0][0][1]))

    def test_play_next_frees_up_space_after_reading_the_log(self):
        from unittest import mock
        from utils import quick_play, storage
        settings = {"download_dir": self.downloads, "cleanup_keep": 2}
        order = []
        with mock.patch.object(quick_play, "_settings", return_value=settings), \
                mock.patch("utils.mpvnet.find_mpvnet", return_value=(None, None)), \
                mock.patch.object(quick_play, "catch_up", side_effect=lambda exe: order.append("log")), \
                mock.patch.object(storage, "auto_clean",
                                  side_effect=lambda s, d: order.append(("clean", s, d))):
            quick_play.play_next()
        self.assertEqual(["log", ("clean", settings, self.downloads)], order)


class FreeUpSpaceTests(unittest.TestCase):
    """Library > Free up space (utils/storage.py, wl.cleanup_candidates): delete
    watched episodes N behind. Files go through a fake Recycle Bin here -- the
    suite never calls the real one."""

    URL = "https://witanime.site/anime/space-show/"
    TEMPLATE = "https://witanime.site/episode/space-show-الحلقة-{x}/"
    PROFILE = "Space Show"

    def setUp(self):
        from unittest import mock
        from utils import watch_later as wl, storage
        self.wl, self.storage = wl, storage
        self.tmp = tempfile.mkdtemp(prefix="aed_space_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        p.start()
        self.addCleanup(p.stop)
        self.downloads = os.path.join(self.tmp, "animes")
        self.folder = os.path.join(self.downloads, self.PROFILE)
        self.binned = []
        wl.add(self.PROFILE, self.URL, "witanime.site")
        wl.set_parts(self.URL, [{"template": self.TEMPLATE, "max_ep": 24, "first_ep": 1}])
        wl.link_profile(self.URL, 0, self.PROFILE)

    def recycle(self, path):
        os.remove(path)
        self.binned.append(wl_ep(path))
        return True

    def files(self, eps):
        os.makedirs(self.folder, exist_ok=True)
        for ep in eps:
            with open(os.path.join(self.folder, f"{self.PROFILE} Ep{ep}.mp4"), "wb") as f:
                f.write(b"x" * 1000)

    def finish(self, *eps, percent=100.0):
        self.wl.apply_progress([json.dumps({
            "path": os.path.join(self.folder, f"{self.PROFILE} Ep{ep}.mp4"),
            "percent": percent, "eof": percent >= 100}) for ep in eps])

    def on_disk(self):
        return sorted(self.wl.episode_files(self.folder))

    def auto(self, keep=2, **extra):
        return self.storage.auto_clean(dict({"cleanup_keep": keep}, **extra), self.downloads,
                                       recycle=self.recycle)

    def test_finishing_an_episode_deletes_the_ones_n_behind(self):
        self.files(range(1, 25))
        self.finish(*range(1, 20))                     # watched up to Ep 19
        report = self.auto(keep=2)
        self.assertEqual(list(range(1, 18)), self.binned)
        self.assertEqual(list(range(18, 25)), self.on_disk())
        part = self.wl.find(self.URL)["parts"][0]
        self.assertEqual(list(range(1, 18)), part["cleaned"])
        self.assertEqual("Freed " + self.storage.size_text(17000),
                         self.storage.describe(report)[0])
        self.assertIn("Space Show Ep 1-17", self.storage.describe(report)[1])

    def test_episodes_only_implied_by_a_jump_are_never_deleted_on_their_own(self):
        """Ep 28 opened by mistake ticks 1-27; only what was really seen goes."""
        self.files(range(1, 25))
        self.finish(1, 2, 3)
        self.finish(24, percent=0.5)                   # the accidental jump
        self.assertEqual(list(range(1, 24)), self.wl.find(self.URL)["parts"][0]["watched"])
        self.auto(keep=2)
        self.assertEqual([1], self.binned)             # 3 - 2; nothing of 4-23

    def test_off_by_default_and_never_for_a_kept_anime(self):
        self.files(range(1, 25))
        self.finish(*range(1, 20))
        self.assertEqual([], self.storage.auto_clean({}, self.downloads, recycle=self.recycle))
        self.assertEqual([], self.auto(keep=0))
        self.wl.update(self.URL, keep_all=True)
        self.assertEqual([], self.auto(keep=2))
        self.assertEqual([], self.binned)

    def test_hand_ticked_episodes_go_only_when_asked(self):
        self.files(range(1, 25))
        self.wl.set_watched(self.URL, 0, range(1, 11))
        self.assertEqual([], self.auto(keep=2))
        self.storage.clean(self.downloads, 2, url=self.URL, finished_only=False,
                           recycle=self.recycle)
        self.assertEqual(list(range(1, 9)), self.binned)

    def test_deleted_after_watching_is_not_missing_but_deleted_by_hand_is(self):
        self.files(range(1, 25))
        self.finish(*range(1, 11))
        self.auto(keep=2)                              # 1-8 cleaned
        os.remove(os.path.join(self.folder, f"{self.PROFILE} Ep20.mp4"))
        entry = self.wl.find(self.URL)
        self.assertEqual((0, [20]), self.wl.missing_episodes(entry, self.downloads))
        self.assertEqual([20], self.wl.missing_from(entry, 0, 9, self.downloads))

    def test_keeps_three_files_the_one_playing_and_skip_neighbours(self):
        from utils import skip_times
        wl = self.wl
        self.files(range(1, 5))
        self.finish(1, 2, 3, 4)
        self.storage.clean(self.downloads, 0, recycle=self.recycle)   # 4 files: one may go
        self.assertEqual([1], self.binned)
        # The last played episode may be open in mpv.net.
        self.binned.clear()
        self.files(range(5, 11))
        self.finish(5, 6, 7, 8)
        entry = wl.find(self.URL)
        self.assertEqual(8, entry["last_played"]["ep"])
        doomed = [ep for ep, _p in wl.cleanup_candidates(entry, self.downloads, 0)[0]]
        self.assertNotIn(8, doomed)
        # Ep 9 still waits for skip times: its two neighbours below stay.
        stamped = {os.path.basename(p): skip_times.stamp(p)
                   for ep, p in wl.episode_files(self.folder).items() if ep != 9}
        with open(os.path.join(self.folder, skip_times.SKIP_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "episodes": stamped}, f)
        doomed = [ep for ep, _p in wl.cleanup_candidates(entry, self.downloads, 0)[0]]
        self.assertEqual([2, 3, 4, 5, 6], doomed)

    def test_a_file_in_use_is_tried_again_next_time(self):
        self.files(range(1, 25))
        self.finish(*range(1, 6))
        busy = os.path.join(self.folder, f"{self.PROFILE} Ep1.mp4")
        report = self.storage.clean(self.downloads, 2, recycle=lambda p: p != busy and self.recycle(p))
        self.assertEqual([2, 3], self.binned)
        self.assertEqual(1, report[0]["failed"])
        self.assertEqual([2, 3], self.wl.find(self.URL)["parts"][0]["cleaned"])
        self.auto(keep=2)
        self.assertEqual([2, 3, 1], self.binned)

    def test_permanent_deletion_skips_the_recycle_bin(self):
        from unittest import mock
        self.files(range(1, 25))
        self.finish(*range(1, 6))
        bin_ = mock.Mock(return_value=True)
        self.storage.auto_clean({"cleanup_keep": 2, "cleanup_permanent": True}, self.downloads,
                                recycle=bin_)
        bin_.assert_not_called()
        self.assertEqual(list(range(4, 25)), self.on_disk())

    def test_finished_and_cleaned_survive_a_season_refresh(self):
        self.files(range(1, 25))
        self.finish(*range(1, 6))
        self.auto(keep=2)
        self.wl.set_parts(self.URL, [{"template": self.TEMPLATE, "max_ep": 26}])
        part = self.wl.find(self.URL)["parts"][0]
        self.assertEqual([1, 2, 3, 4, 5], part["finished"])
        self.assertEqual([1, 2, 3], part["cleaned"])

    def test_the_windows_delete_call_works(self):
        """The real SHFileOperationW call, minus FOF_ALLOWUNDO so the temp file is
        deleted outright: nothing lands in the user's Recycle Bin."""
        if sys.platform != "win32":
            self.skipTest("Windows only")
        from unittest import mock
        path = os.path.join(self.tmp, "Space Show Ep1.mp4")
        with open(path, "wb") as f:
            f.write(b"x")
        with mock.patch.object(self.storage, "_FOF_ALLOWUNDO", 0):
            self.assertTrue(self.storage._recycle(path))
        self.assertFalse(os.path.exists(path))

    def test_storage_and_details_dialogs(self):
        from unittest import mock
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication, QWidget
        type(self)._app = QApplication.instance() or QApplication([])
        from ui import watch_later_tab as tab
        host = QWidget()
        dlg = tab.StorageDialog(2, True, host)
        self.assertEqual((2, True), dlg.values())
        dlg.combo_keep.setCurrentIndex(0)
        dlg.combo_where.setCurrentIndex(0)
        self.assertEqual((0, False), dlg.values())

        self.files(range(1, 25))
        self.finish(*range(1, 11))
        with mock.patch.object(tab, "_settings", return_value={"cleanup_keep": 2}), \
                mock.patch.object(tab, "_download_dir", return_value=self.downloads):
            dlg = tab.EntryDialog(self.wl.find(self.URL), host)
            self.assertTrue(dlg.btn_free.isVisibleTo(dlg))         # 1-8 can go
            self.assertIn("8 watched", dlg.btn_free.toolTip())
            dlg.check_keep_all.setChecked(True)
            dlg.yesButton.click()
            self.assertTrue(self.wl.find(self.URL)["keep_all"])
            dlg = tab.EntryDialog(self.wl.find(self.URL), host)
            self.assertFalse(dlg.btn_free.isVisibleTo(dlg))        # kept: nothing offered
        self.assertEqual(list(range(1, 25)), self.on_disk())

    def test_settings_and_text(self):
        keep = self.storage.keep_setting
        self.assertEqual(0, keep({}))
        self.assertEqual(0, keep({"cleanup_keep": "x"}))
        self.assertEqual(0, keep({"cleanup_keep": -1}))
        self.assertEqual(3, keep({"cleanup_keep": 3}))
        self.assertEqual("1-3, 7, 9-10", self.storage.episode_spec([9, 1, 2, 3, 7, 10]))
        self.assertEqual("1.5 GB", self.storage.size_text(int(1.5 * 1024 ** 3)))
        self.assertIsNone(self.storage.describe([{"episodes": [], "freed": 0, "failed": 1,
                                                  "title": "x", "label": ""}]))


def wl_ep(path):
    from utils.watch_later import episode_number
    return episode_number(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
