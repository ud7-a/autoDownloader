"""Anime news and Library polish: a followed anime's way to its episodes ("Find to
download", the notice that says so, a click on it opening the news), the New
filter and airing order, and each season's "Skip opening" status in the Library's
details window. No test reaches AniList or decodes audio.

Run with: py -m unittest discover -s tests
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

from tests.test_news import FakeAniList, m  # noqa: E402
from tests.test_news_edges import TabBase  # noqa: E402


def airing(id=10, title="Clover", at=None, english=None):
    return m(id, title, status="RELEASING", start=(2026, 10, 3), english=english,
             next_ep={"episode": 6, "airingAt": at or time.time() + 86400})


class FollowedToDownloadTests(TabBase):
    def tab(self, followed, records, library=()):
        self.write_file({"shows": records, "checked_at": time.time(), "followed": followed})
        p = mock.patch("ui.news_tab.library_shows", return_value=list(library))
        p.start()
        self.addCleanup(p.stop)
        from ui.news_tab import NewsWidget
        w = NewsWidget()
        self.addCleanup(w.deleteLater)
        w.refresh()
        return w

    def airing_state(self, anilist_id, next_at=None):
        return dict(self.state("airing"), anilist_id=anilist_id, next_at=next_at)

    def test_the_sites_name_is_the_romaji_one(self):
        frieren = m(1, "Sousou no Frieren", english="Frieren: Beyond Journey's End")
        self.assertEqual("Sousou no Frieren", self.news.site_name(frieren))
        native = {"id": 2, "title": {"romaji": None, "english": None, "native": "まかでみ"}}
        self.assertEqual("まかでみ", self.news.site_name(native))
        self.assertEqual("?", self.news.site_name(None))
        entry = self.news.follow(frieren)
        self.assertEqual(("Frieren: Beyond Journey's End", "Sousou no Frieren"),
                         (entry["title"], entry["site_name"]))
        self.assertEqual("Sousou no Frieren", self.news.followed()[0]["site_name"])

    def test_find_asks_the_sites_for_the_romaji_name(self):
        w = self.tab([{"url": "anilist:1", "title": "Frieren: Beyond Journey's End",
                       "site_name": "Sousou no Frieren 2nd Season"},
                      {"url": "anilist:2", "title": "Old Follow"}],      # from before site_name
                     {"anilist:1": {"anilist_id": 1, "state": self.airing_state(1)},
                      "anilist:2": {"anilist_id": 2, "state": self.airing_state(2)}})
        got = []
        w.search_requested.connect(lambda q, d: got.append((q, d)))
        w.find_in_search("anilist:1")
        w.find_in_search("anilist:2")
        self.assertEqual([("sousou no frieren", ""), ("old follow", "")], got)

    def test_a_followed_airing_anime_offers_to_download_it(self):
        from qfluentwidgets import PrimaryPushButton
        lib = [{"url": "https://site/a", "title": "Library Show"}]
        w = self.tab([{"url": "anilist:9", "title": "Followed", "site_name": "Followed"}],
                     {"https://site/a": {"state": self.airing_state(1)},
                      "anilist:9": {"anilist_id": 9, "state": self.airing_state(9)}}, lib)
        rows = {r.url: r for r in w._rows}
        followed, library = rows["anilist:9"].btn_find, rows["https://site/a"].btn_find
        self.assertEqual("Find to download", followed.text())
        self.assertIsInstance(followed, PrimaryPushButton)
        self.assertIn("heart button", followed.toolTip())
        self.assertEqual("Find in Search", library.text())
        got = []
        w.search_requested.connect(lambda q, d: got.append(q))
        followed.click()
        self.assertEqual(["followed"], got)

    def test_a_followed_anime_that_is_only_coming_has_nothing_to_find(self):
        w = self.tab([{"url": "anilist:9", "title": "F"}],
                     {"anilist:9": {"anilist_id": 9, "state": dict(self.state("coming"), anilist_id=9)}})
        self.assertIsNone(w._rows[0].btn_find)

    def test_the_notice_says_how_to_get_its_episodes(self):
        coming = {"kind": "coming", "key": "coming|10|", "when": "No date yet", "headline": "h",
                  "detail": "", "season_title": "Clover", "site_url": "", "anilist_id": 10}
        self.write_file({"shows": {
            "anilist:10": {"anilist_id": 10, "manual": True, "state": coming, "checked_at": 0},
            "https://site/c": {"anilist_id": 11, "manual": True, "state": dict(coming, anilist_id=11),
                               "checked_at": 0}},
            "checked_at": 0, "followed": [{"url": "anilist:10", "title": "Clover"}]})
        fake = FakeAniList({10: airing(10, "Clover"), 11: airing(11, "Spade")})
        _shows, changes, _errors = self.news.check(
            [{"url": "https://site/c", "title": "Spade"}] + self.news.followed(), fetch=fake, gap=0)
        self.assertEqual([("Spade", "Spade: Spade has started airing."),
                          ("Clover", "Clover: Clover has started airing. "
                                     "Find it in Search to download it.")], changes)

    def test_an_announcement_for_a_followed_anime_says_nothing_more(self):
        waiting = {"kind": "waiting", "key": "waiting|10", "when": "", "headline": "h",
                   "detail": "", "season_title": "Clover", "site_url": "", "anilist_id": 10}
        self.write_file({"shows": {"anilist:10": {"anilist_id": 10, "manual": True,
                                                  "state": waiting, "checked_at": 0}},
                         "checked_at": 0, "followed": [{"url": "anilist:10", "title": "Clover"}]})
        coming = m(10, "Clover", status="NOT_YET_RELEASED", start=(2027, 4, None))
        _s, changes, _e = self.news.check(self.news.followed(), fetch=FakeAniList({10: coming}), gap=0)
        self.assertEqual([("Clover", "Clover: Clover has been announced for April 2027.")], changes)


class NewFilterAndOrderTests(TabBase):
    def record(self, kind, changed=None, next_at=None):
        rec = {"state": dict(self.state(kind), next_at=next_at), "checked_at": time.time()}
        if changed is not None:
            rec["changed_at"] = changed
        return rec

    def test_describe_keeps_when_the_next_episode_airs(self):
        s = self.news.describe(airing(at=1_900_000_000), "RELEASING")
        self.assertEqual(1_900_000_000, s["next_at"])
        no_date = m(4, "X", status="RELEASING", next_ep=None)
        self.assertIsNone(self.news.describe(no_date, "RELEASING")["next_at"])

    def test_new_shows_the_news_of_the_last_7_days(self):
        now = time.time()
        w = self.shows([{"url": "a", "title": "Fresh"}, {"url": "b", "title": "Old News"},
                        {"url": "c", "title": "Never Changed"}],
                       {"a": self.record("coming", now - 3600),
                        "b": self.record("airing", now - 8 * 86400),
                        "c": self.record("finished")})
        self.assertEqual("New  1", w.seg.widget("new").text())
        self.assertEqual("All  3", w.seg.widget("all").text())
        w.show_filter("new")
        self.assertEqual("new", w.seg.currentRouteKey())
        self.assertEqual(["a"], [r.url for r in w._rows])
        self.assertTrue(w.empty.isHidden())

    def test_new_with_nothing_new(self):
        w = self.shows([{"url": "a", "title": "A"}], {"a": self.record("finished")})
        w.show_filter("new")
        self.assertEqual([], w._rows)
        self.assertEqual("No news in the last 7 days.", w.empty.text())
        w.show_filter("finished")
        self.assertEqual(["a"], [r.url for r in w._rows])

    def test_airing_shows_come_soonest_first(self):
        now = time.time()
        w = self.shows([{"url": u, "title": t} for u, t in
                        (("a", "Aaa"), ("b", "Bbb"), ("c", "Ccc"), ("d", "Ddd"), ("e", "Eee"))],
                       {"a": self.record("airing", next_at=now + 5 * 86400),
                        "b": self.record("airing", next_at=now + 3600),
                        "c": self.record("airing"),                       # no date: last of them
                        "d": self.record("coming"),
                        "e": self.record("airing", next_at="junk")})
        self.assertEqual(["b", "a", "c", "e", "d"], [r.url for r in w._rows])


class NotificationClickTests(unittest.TestCase):
    def notifier(self):
        from ui.notifier import Notifier
        window = mock.Mock()
        window.isActiveWindow.return_value = False
        window.isMinimized.return_value = False
        n = Notifier(window)
        n._ensure_tray = mock.Mock(return_value=mock.Mock())
        return n, window

    def test_a_click_brings_the_window_back_then_runs_its_action(self):
        from utils.config import app_settings
        n, window = self.notifier()
        action = mock.Mock()
        with mock.patch.dict(app_settings, {"windows_notifications": True}):
            self.assertTrue(n.notify("Anime news", "x", action))
            n._clicked()
            window.activateWindow.assert_called_once()
            action.assert_called_once_with()
            n.notify("Download complete", "y")           # the next one has no action
            n._clicked()
        action.assert_called_once_with()

    def test_news_notices_open_the_new_filter(self):
        from ui.app_window import AppWindow
        from utils.config import app_settings
        fake = types.SimpleNamespace(notifier=mock.Mock(), _show_news=object())
        with mock.patch.dict(app_settings, {"news_notify": True}):
            AppWindow._on_news_changes(fake, [("A", "A: news.")])
            AppWindow._on_news_changes(fake, [(str(i), "n") for i in range(5)])
        calls = fake.notifier.notify.call_args_list
        self.assertEqual([fake._show_news, fake._show_news], [c.args[2] for c in calls])
        tabs = types.SimpleNamespace(open_tab=mock.Mock(), news_interface=mock.Mock())
        AppWindow._show_news(tabs)
        tabs.open_tab.assert_called_once_with("news_interface")
        tabs.news_interface.show_filter.assert_called_once_with("new")


class SkipStatusTests(unittest.TestCase):
    def setUp(self):
        from utils import skip_times
        self.st = skip_times
        self.tmp = tempfile.mkdtemp(prefix="aed_skips_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def files(self, eps):
        for ep in eps:
            with open(os.path.join(self.tmp, f"Show Ep{ep}.mp4"), "wb") as f:
                f.write(b"\0" * (1000 + ep))

    def saved(self, ops):
        """The skip file: {episode: opening or None}, stamped for the files as they are."""
        eps = {}
        for ep, op in ops.items():
            path = os.path.join(self.tmp, f"Show Ep{ep}.mp4")
            eps[os.path.basename(path)] = dict(self.st.stamp(path), op=op, ed=None)
        with open(os.path.join(self.tmp, self.st.SKIP_FILE), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "episodes": eps}, f)

    def test_no_folder_or_no_episodes_says_nothing(self):
        self.assertEqual("", self.st.skip_status(""))
        self.assertEqual("", self.st.skip_status(None))
        self.assertEqual("", self.st.skip_status(os.path.join(self.tmp, "gone")))
        self.assertEqual("", self.st.skip_status(self.tmp))

    def test_fewer_than_three_episodes(self):
        self.files([1, 2])
        self.assertEqual("Skip opening: needs 3 episodes in the folder to find the opening "
                         "(2 so far).", self.st.skip_status(self.tmp))

    def test_ready_for_every_episode(self):
        self.files(range(1, 5))
        self.saved({ep: [10, 100] for ep in range(1, 5)})
        self.assertEqual("Skip opening: ready for every episode.", self.st.skip_status(self.tmp))

    def test_found_none_found_and_not_looked_at(self):
        self.files(range(1, 7))
        self.saved({1: None, 2: [5, 95], 3: [5, 95], 4: [5, 95]})
        self.assertEqual("Skip opening: ready for Ep 2-4; no opening found in Ep 1; Ep 5-6 not "
                         "looked at yet (Find openings to skip, in the card's ··· menu, looks now).",
                         self.st.skip_status(self.tmp))

    def test_a_replaced_episode_is_looked_at_again(self):
        self.files(range(1, 4))
        self.saved({ep: [5, 95] for ep in range(1, 4)})
        with open(os.path.join(self.tmp, "Show Ep2.mp4"), "wb") as f:
            f.write(b"\0" * 9000)
        self.assertIn("ready for Ep 1, 3; Ep 2 not looked at yet", self.st.skip_status(self.tmp))

    def test_only_none_found(self):
        self.files(range(1, 4))
        self.saved({ep: None for ep in range(1, 4)})
        self.assertEqual("Skip opening: no opening found in Ep 1-3.", self.st.skip_status(self.tmp))

    def test_a_folder_in_the_queue_or_being_looked_at(self):
        self.files(range(1, 4))
        with self.st._queue_lock:
            self.st._queue.append((self.tmp + os.sep, False))      # the same folder, spelled apart
        self.addCleanup(self.st._queue.clear)
        self.assertTrue(self.st.is_queued(self.tmp))
        self.assertIn("(being looked at now)", self.st.skip_status(self.tmp))
        self.st._queue.clear()
        self.assertFalse(self.st.is_queued(self.tmp))
        with mock.patch.object(self.st, "_current", self.tmp):
            self.assertTrue(self.st.is_queued(self.tmp))
        self.assertFalse(self.st.is_queued(""))

    def test_the_worker_says_which_folder_it_is_on(self):
        seen = []
        fake = types.SimpleNamespace(detect_folder=lambda folder, **_k: seen.append(
            (folder, self.st._current, self.st.is_queued(folder))))
        import threading
        import utils
        # A fresh stop flag: an app window closed by an earlier test sets the real one.
        with mock.patch.dict(sys.modules, {"utils.intro_detect": fake}), \
                mock.patch.object(utils, "intro_detect", fake, create=True), \
                mock.patch.object(self.st, "_stop", threading.Event()):
            self.st._queue.append((self.tmp, False))
            self.st._run()
        self.assertEqual([(self.tmp, self.tmp, True)], seen)
        self.assertIsNone(self.st._current)
        self.assertFalse(self.st.is_queued(self.tmp))


class DetailsSkipLineTests(unittest.TestCase):
    URL = "https://witanime.site/anime/space-show/"
    PROFILE = "Space Show"

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        from utils import watch_later as wl
        self.wl = wl
        self.tmp = tempfile.mkdtemp(prefix="aed_details_skips_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(wl, "FILE", os.path.join(self.tmp, "watch_later.json"))
        p.start()
        self.addCleanup(p.stop)
        self.downloads = os.path.join(self.tmp, "animes")
        self.folder = os.path.join(self.downloads, self.PROFILE)
        wl.add(self.PROFILE, self.URL, "witanime.site")
        wl.set_parts(self.URL, [{"template": "https://witanime.site/episode/space-show-{x}/",
                                 "max_ep": 12, "first_ep": 1}])
        wl.link_profile(self.URL, 0, self.PROFILE)

    def texts(self):
        from PyQt6.QtWidgets import QLabel, QWidget
        from ui import watch_later_tab as tab
        host = QWidget()
        self.addCleanup(host.deleteLater)
        with mock.patch.object(tab, "_settings", return_value={}), \
                mock.patch.object(tab, "_download_dir", return_value=self.downloads):
            dlg = tab.EntryDialog(self.wl.find(self.URL), host)
        self.addCleanup(dlg.deleteLater)
        return [lbl.text() for lbl in dlg.findChildren(QLabel)]

    def test_each_season_says_its_skip_status(self):
        os.makedirs(self.folder)
        for ep in range(1, 5):
            with open(os.path.join(self.folder, f"{self.PROFILE} Ep{ep}.mp4"), "wb") as f:
                f.write(b"x")
        self.assertTrue(any(t.startswith("Skip opening: Ep 1-4 not looked at yet") for t in self.texts()))

    def test_nothing_downloaded_says_nothing_about_skips(self):
        self.assertFalse(any(t.startswith("Skip opening") for t in self.texts()))


if __name__ == "__main__":
    unittest.main()
