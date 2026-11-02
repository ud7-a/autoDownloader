""""What's new" after an update: reading the bundled CHANGELOG.md
(utils/changelog.py), choosing what to show (whats_new), the window
(ui/whats_new.py), when the app shows it (AppWindow._maybe_show_whats_new) and the
settings it keeps.

Run with: py -m unittest discover -s tests
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

LOG = """# Changelog

Intro text.

## Unreleased

### 📰 New: Anime news tab
- A new **Anime news** tab, see `news_tab.py`.
- Two lines that
  wrap onto a second.

## 5.2.0 — 2026-11-03

### 🔁 witanime: Google Drive first
- Google **first**.

## 5.1.0 — 2026-10-30

- A loose point before any feature.

### ⏭️ New: skip openings and endings
- Find openings.
  - A sub-point.

### Fixes without an emoji
- One <b>fix</b> & [a link](https://example.com/x).

## 5.0.0 — 2026-10-01

### 🧹 Old feature
- Old.

## 4.9.0

## 4.8.2 — 2026-09-01
### ✨ Ancient
- Ancient.
"""


class ChangelogTests(unittest.TestCase):
    def setUp(self):
        from utils import changelog
        self.c = changelog

    def test_versions_and_dates(self):
        vk, sd = self.c.version_key, self.c.section_date
        self.assertEqual((5, 2, 0), vk("5.2.0 — 2026-11-03"))
        self.assertEqual((5, 10), vk("v5.10"))
        self.assertLess(vk("5.9.0"), vk("5.10.0"))                  # numbers, not text
        self.assertIsNone(vk("Unreleased"))
        self.assertIsNone(vk(None))
        self.assertEqual("2026-11-03", sd("5.2.0 — 2026-11-03"))
        self.assertEqual("", sd("4.9.0"))

    def test_features(self):
        body = dict(self.c.sections(LOG))["5.1.0 — 2026-10-30"]
        feats = self.c.features(body)
        self.assertEqual([("", ""), ("⏭️", "New: skip openings and endings"), ("", "Fixes without an emoji")],
                         [(f["emoji"], f["title"]) for f in feats])
        self.assertEqual(["A loose point before any feature."], feats[0]["points"])
        self.assertEqual(["Find openings.", "A sub-point."], feats[1]["points"])
        self.assertEqual(['One &lt;b&gt;fix&lt;/b&gt; &amp; <a href="https://example.com/x">a link</a>.'],
                         feats[2]["points"])                         # escaped, link kept

    def test_inline_markup_and_wrapping(self):
        feats = self.c.features(dict(self.c.sections(LOG))["Unreleased"])
        self.assertEqual(["A new <b>Anime news</b> tab, see <code>news_tab.py</code>.",
                          "Two lines that wrap onto a second."], feats[0]["points"])
        self.assertEqual("📰", feats[0]["emoji"])
        self.assertEqual([], self.c.features(""))
        self.assertEqual([], self.c.features(None))
        more = self.c.features("### X\n- The **⋯** menu.")[0]["points"][0]
        self.assertEqual("The <b>···</b> menu.", more)            # a glyph Segoe UI has

    def test_changes_since(self):
        since = self.c.changes_since
        self.assertEqual(["5.2.0", "5.1.0"], [v for v, _d, _f in since(LOG, "5.0.0", "5.2.0")])
        self.assertEqual("2026-11-03", since(LOG, "5.1.0", "5.2.0")[0][1])
        # A build from source: the version has no section yet, Unreleased is it.
        got = since(LOG, "5.2.0", "5.3.0")
        self.assertEqual([("5.3.0", "")], [(v, d) for v, d, _f in got])
        self.assertEqual("New: Anime news tab", got[0][2][0]["title"])
        # Sections without points (4.9.0) and later than the app are left out.
        self.assertEqual(["5.0.0", "4.8.2"], [v for v, _d, _f in since(LOG, "4.0", "5.0.0")])
        self.assertEqual([], since(LOG, "5.0.0", "not a version"))

    def test_what_to_show(self):
        wn = self.c.whats_new
        self.assertEqual([], wn(LOG, "5.2.0", "5.2.0"))              # seen
        self.assertEqual([], wn(LOG, "5.2.0 ", "5.2.0"))
        self.assertEqual([], wn(LOG, "5.3.0", "5.2.0"))              # downgraded
        self.assertEqual(["5.2.0"], [v for v, _d, _f in wn(LOG, "", "5.2.0")])   # never shown: this one only
        self.assertEqual(["5.2.0"], [v for v, _d, _f in wn(LOG, "garbage", "5.2.0")])
        self.assertEqual(["5.2.0", "5.1.0", "5.0.0"], [v for v, _d, _f in wn(LOG, "4.0.0", "5.2.0")])  # capped
        self.assertEqual([], wn("", "5.0.0", "5.2.0"))               # no changelog bundled

    def test_the_bundled_file(self):
        with mock.patch.object(self.c.sys, "_MEIPASS", r"C:\App\_internal", create=True):
            self.assertEqual(os.path.join(r"C:\App\_internal", "CHANGELOG.md"), self.c.bundled_path())
        with mock.patch.object(self.c, "bundled_path", return_value=os.path.join(tempfile.gettempdir(), "no-such.md")):
            self.assertEqual("", self.c.read_bundled())

    def test_the_shipped_changelog(self):
        """Every version's notes read as features, and this version has some: a
        release without notes would show nothing after the update."""
        from utils.config import APP_VERSION
        text = self.c.read_bundled()
        self.assertTrue(text)
        for title, body in self.c.sections(text):
            for f in self.c.features(body):
                self.assertTrue(f["points"], f"{title}: {f['title']} has no points")
        self.assertTrue(self.c.changes_since(text, "0", APP_VERSION))

    def test_the_release_tool_reads_sections_the_same_way(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import release_notes
        self.assertIs(self.c.sections, release_notes._sections)
        self.assertIn("Google **first**", release_notes.notes_for(LOG, "5.2.0"))


class Qt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls._app = QApplication.instance() or QApplication([])

    def versions(self, seen="5.0.0", current="5.2.0"):
        from utils import changelog
        return changelog.whats_new(LOG, seen, current)

    def dialog(self, versions):
        """Never closed for real: done() starts a fade-out whose callback outlives
        the deleted dialog and crashes a later test."""
        from PyQt6.QtWidgets import QWidget
        from ui.whats_new import WhatsNewDialog
        host = QWidget()
        self.addCleanup(host.deleteLater)
        dlg = WhatsNewDialog(versions, host)
        p = mock.patch.object(dlg, "accept")
        self.accept = p.start()
        self.addCleanup(p.stop)
        return dlg


class WindowTests(Qt):
    def test_targets(self):
        from ui.whats_new import target_for
        self.assertEqual("news_interface", target_for("New: Anime news tab")[0])
        self.assertEqual("player_interface", target_for("New: skip openings and endings")[0])
        self.assertEqual("watch_later_interface", target_for("New: free up space")[0])
        self.assertEqual("manager_interface", target_for("witanime: Google Drive first")[0])
        self.assertEqual("player_interface", target_for("Video Player")[0])
        self.assertIsNone(target_for("Fixes without an emoji"))
        self.assertIsNone(target_for(None))

    def test_pages_titles_and_buttons(self):
        dlg = self.dialog(self.versions())
        self.assertEqual(4, len(dlg.pages))                          # 1 + 3 features, newest first
        titles = [p.title.text() for p in dlg.pages]
        self.assertEqual(["Witanime: Google Drive first", "Other changes", "Skip openings and endings",
                          "Fixes without an emoji"], titles)          # "New:" became a badge
        self.assertEqual("Open Profile Manager", dlg.pages[0].button.text())
        self.assertIsNone(dlg.pages[1].button)
        self.assertTrue(dlg.pages[3].points[0].openExternalLinks())
        self.assertEqual("1 of 4", dlg.counter.text())
        self.assertEqual("Next", dlg.yesButton.text())
        self.assertTrue(dlg.pager.isVisibleTo(dlg))

    def test_next_turns_pages_until_the_last(self):
        dlg = self.dialog(self.versions())
        for i in range(3):
            self.assertFalse(dlg.validate())                         # Next: stays open
            self.assertEqual(i + 1, dlg.stack.currentIndex())
        self.assertEqual(("Got it", "4 of 4"), (dlg.yesButton.text(), dlg.counter.text()))
        self.assertTrue(dlg.validate())                              # Got it: closes

    def test_arrow_keys_pips_and_bounds(self):
        from PyQt6.QtCore import Qt as Q
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtCore import QEvent
        dlg = self.dialog(self.versions())
        press = lambda k: dlg.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, Q.KeyboardModifier.NoModifier))  # noqa: E731
        press(Q.Key.Key_Left)
        self.assertEqual(0, dlg.stack.currentIndex())                # not before the first
        press(Q.Key.Key_Right)
        press(Q.Key.Key_PageDown)
        self.assertEqual(2, dlg.stack.currentIndex())
        dlg.pager.currentIndexChanged.emit(3)                        # a pip clicked
        self.assertEqual(3, dlg.stack.currentIndex())
        press(Q.Key.Key_Right)
        self.assertEqual(3, dlg.stack.currentIndex())                # not past the last
        press(Q.Key.Key_PageUp)
        self.assertEqual(2, dlg.stack.currentIndex())

    def test_show_me_closes_on_its_tab(self):
        dlg = self.dialog(self.versions())
        dlg.pages[0].button.click()
        self.assertEqual("manager_interface", dlg.opened)
        self.accept.assert_called_once()
        self.assertTrue(dlg.validate())                              # closes from any page

    def test_one_version_one_feature(self):
        dlg = self.dialog(self.versions("5.1.0", "5.2.0"))
        self.assertEqual(1, len(dlg.pages))
        self.assertEqual(("Got it", ""), (dlg.yesButton.text(), dlg.counter.text()))
        self.assertFalse(dlg.pager.isVisibleTo(dlg))
        from PyQt6.QtWidgets import QLabel
        heads = [w.text() for w in dlg.findChildren(QLabel)]
        self.assertTrue(any("What's new in version 5.2.0" == t for t in heads))
        self.assertTrue(any("Released 3 November 2026" == t for t in heads))

    def test_several_versions_say_since_when(self):
        from utils.config import app_settings
        with mock.patch.dict(app_settings, {"whats_new_seen": "5.0.0"}):
            dlg = self.dialog(self.versions())
        from PyQt6.QtWidgets import QLabel
        texts = [w.text() for w in dlg.findChildren(QLabel)]
        self.assertIn("What's new since version 5.0.0", texts)
        self.assertIn("Version 5.1.0 · 30 October 2026", texts)


class ShowAndSeenTests(Qt):
    def setUp(self):
        from utils.config import app_settings
        p = mock.patch.dict(app_settings, {"whats_new_seen": "5.0.0"})
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch("ui.whats_new.save_config")
        self.save = s.start()
        self.addCleanup(s.stop)

    def run_show(self, opened=None, versions=None):
        from PyQt6.QtWidgets import QWidget
        from ui import whats_new
        host = QWidget()
        self.addCleanup(host.deleteLater)
        opened_tabs = []

        def fake_exec(dlg):
            dlg.opened = opened
            return 1
        with mock.patch.object(whats_new.WhatsNewDialog, "exec", fake_exec):
            whats_new.show(host, self.versions() if versions is None else versions, opened_tabs.append)
        return opened_tabs

    def test_shown_means_seen_and_show_me_opens_the_tab(self):
        from utils.config import APP_VERSION, app_settings
        self.assertEqual(["news_interface"], self.run_show(opened="news_interface"))
        self.assertEqual(APP_VERSION, app_settings["whats_new_seen"])
        self.save.assert_called()

    def test_closed_without_show_me(self):
        self.assertEqual([], self.run_show(opened=None))

    def test_nothing_to_show_shows_nothing(self):
        from utils.config import app_settings
        self.assertEqual([], self.run_show(versions=[]))
        self.assertEqual("5.0.0", app_settings["whats_new_seen"])


class AppWindowTests(unittest.TestCase):
    def window(self, **state):
        base = dict(_download_running=False, _update_busy=False, _remote_start=False,
                    open_tab=mock.Mock(), isVisible=lambda: True)
        base.update(state)
        return types.SimpleNamespace(**base)

    def call(self, window, pending, seen="5.0.0"):
        from ui import whats_new
        from ui.app_window import AppWindow
        from utils.config import app_settings
        with mock.patch.object(whats_new, "pending", return_value=pending), \
                mock.patch.object(whats_new, "show") as show, \
                mock.patch.object(whats_new, "mark_seen") as mark, \
                mock.patch.dict(app_settings, {"whats_new_seen": seen}):
            AppWindow._maybe_show_whats_new(window)
        return show, mark

    def test_shown_after_an_update(self):
        w = self.window()
        show, mark = self.call(w, ["v"])
        show.assert_called_once_with(w, ["v"], w.open_tab)
        mark.assert_not_called()

    def test_not_over_a_download_an_update_prompt_a_remote_start_or_off_screen(self):
        for state in ({"_download_running": True}, {"_update_busy": True}, {"_remote_start": True},
                      {"isVisible": lambda: False}):
            show, mark = self.call(self.window(**state), ["v"])
            show.assert_not_called()
            mark.assert_not_called()                                 # still unseen: next launch

    def test_nothing_to_tell_is_marked_seen_quietly(self):
        from utils.config import APP_VERSION
        show, mark = self.call(self.window(), [])
        show.assert_not_called()
        mark.assert_called_once()
        _show, mark = self.call(self.window(), [], seen=APP_VERSION)
        mark.assert_not_called()

    def test_the_up_to_date_notice_offers_it_again(self):
        from ui.app_window import AppWindow
        import qfluentwidgets
        bar = mock.Mock()
        w = types.SimpleNamespace(show_whats_new=mock.Mock())
        with mock.patch.object(qfluentwidgets.InfoBar, "success", return_value=bar), \
                mock.patch.object(qfluentwidgets, "PushButton") as button:
            AppWindow._show_update_check_result(w, "success", "You're up to date.")
            bar.addWidget.assert_called_once_with(button.return_value)
            clicked = button.return_value.clicked.connect.call_args.args[0]
            clicked()
        w.show_whats_new.assert_called_once()
        bar.close.assert_called_once()
        with mock.patch.object(qfluentwidgets.InfoBar, "error", return_value=bar):
            bar.reset_mock()
            AppWindow._show_update_check_result(w, "error", "Couldn't check")
            bar.addWidget.assert_not_called()

    def test_it_waits_for_the_tabs(self):
        from ui.app_window import AppWindow
        self.assertGreater(AppWindow.WHATS_NEW_DELAY_MS, 0)

    def test_start_up_timers_die_with_the_window(self):
        """A bare QTimer.singleShot fired into a window the tests had already
        deleted and crashed a later test (0xC0000409): the timers are the window's."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QWidget
        from ui.app_window import AppWindow
        app = QApplication.instance() or QApplication([])
        owner = QWidget()
        fired = []
        timer = AppWindow._later(owner, 50, lambda: fired.append(1))
        self.assertIs(owner, timer.parent())
        self.assertTrue(timer.isSingleShot())
        from PyQt6 import sip
        sip.delete(owner)                                            # the window goes now
        QTimer.singleShot(150, app.quit)                             # well past the 50 ms
        app.exec()
        self.assertEqual([], fired)
        kept = QWidget()
        self.addCleanup(kept.deleteLater)
        AppWindow._later(kept, 10, lambda: fired.append(2))
        QTimer.singleShot(150, app.quit)
        app.exec()
        self.assertEqual([2], fired)                                 # and fires while it stays


class SettingsTests(unittest.TestCase):
    """Settings are read back on start only if app_settings lists them: news_notify
    wasn't, so turning Anime news' notifications off lasted until a restart."""

    def setUp(self):
        import utils.config as cfg
        self.cfg = cfg
        self.tmp = tempfile.mkdtemp(prefix="aed_settings_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, "sites_config.json")
        for name, value in (("APP_DIR", self.tmp), ("CONFIG_FILE", self.file)):
            p = mock.patch.object(cfg, name, value)
            p.start()
            self.addCleanup(p.stop)
        saved = copy.deepcopy(cfg.app_settings)
        self.addCleanup(lambda: (cfg.app_settings.clear(), cfg.app_settings.update(saved)))

    def test_news_notify_and_whats_new_seen_survive_a_restart(self):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"sites": {}, "settings": {"news_notify": False, "whats_new_seen": "5.1.0"}}, f)
        self.cfg.load_config()
        self.assertFalse(self.cfg.app_settings["news_notify"])
        self.assertEqual("5.1.0", self.cfg.app_settings["whats_new_seen"])

    def test_a_fresh_install_has_nothing_new(self):
        self.cfg.load_config()                                       # no file yet
        with open(self.file, encoding="utf-8") as f:
            self.assertEqual(self.cfg.APP_VERSION, json.load(f)["settings"]["whats_new_seen"])

    def test_an_install_from_before_the_window_sees_it_once(self):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump({"sites": {}, "settings": {"download_dir": "D:\\Anime"}}, f)
        self.cfg.app_settings["whats_new_seen"] = ""                 # the default, as on a real start
        self.cfg.load_config()
        self.assertEqual("", self.cfg.app_settings["whats_new_seen"])


if __name__ == "__main__":
    unittest.main()
