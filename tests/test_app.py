"""App shell: config loading and migration, start-up speed, layout, tooltips, menus, touch input, error reports and fixes from the app reviews.

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
            self.assertEqual(["witanime_mp4upload_v1", "witanime_wahmi_v1", "witanime_template_v2"], ran)
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
        self.addCleanup(shutil.rmtree, fresh, True)
        old =(cfg.APP_DIR, cfg.CONFIG_FILE, list(cfg.app_settings.get("migrations_done", [])))
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


class WindowGuardTests(unittest.TestCase):
    def test_window_guard_headless_flag(self):
        from core.window_guard import WindowGuard
        guard_visible = WindowGuard(headless=False)
        self.assertFalse(guard_visible.headless)

        guard_hidden = WindowGuard(headless=True)
        # On Windows, headless=True enables guard
        import sys
        if sys.platform == "win32":
            self.assertTrue(guard_hidden.headless)

    def test_window_guard_suppresses_only_target_windows(self):
        from unittest import mock
        from core.window_guard import (
            WindowGuard, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW,
            SWP_NOACTIVATE, SWP_NOZORDER, SW_HIDE
        )

        guard = WindowGuard(headless=True)
        guard.target_pids = {9999}
        guard.prelaunch = False
        guard.last_user_fg = 1234  # User was typing in window 1234

        # Window 1: Our Chrome window (PID 9999, class Chrome_WidgetWin_1, on screen at 100, 100)
        # Window 2: User's personal Chrome (PID 1111, class Chrome_WidgetWin_1, on screen at 200, 200)
        # Window 3: Notepad (PID 2222, class Notepad, on screen)
        # Window 4: Our Chrome popup window that stole foreground (PID 9999, class Chrome_WidgetWin_1)

        set_styles = {}
        moved_windows = {}
        hidden_windows = []
        restored_fgs = []

        class MockUser32:
            def GetForegroundWindow(self):
                return 4  # Window 4 stole foreground

            def GetWindowThreadProcessId(self, hwnd, pid_ref):
                pid_map = {1: 9999, 2: 1111, 3: 2222, 4: 9999}
                pid_ref._obj.value = pid_map.get(hwnd, 0)
                return 1

            def GetClassNameW(self, hwnd, buf, maxlen):
                cls_map = {1: "Chrome_WidgetWin_1", 2: "Chrome_WidgetWin_1", 3: "Notepad", 4: "Chrome_WidgetWin_1"}
                buf.value = cls_map.get(hwnd, "")
                return len(buf.value)

            def GetWindowRect(self, hwnd, rect_ref):
                if hwnd == 1:
                    rect_ref._obj.left, rect_ref._obj.top = 100, 100
                elif hwnd == 2:
                    rect_ref._obj.left, rect_ref._obj.top = 200, 200
                elif hwnd == 4:
                    rect_ref._obj.left, rect_ref._obj.top = 50, 50

            def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
                moved_windows[hwnd] = (x, y, flags)

            def IsWindowVisible(self, hwnd):
                return True

            def ShowWindow(self, hwnd, cmd):
                hidden_windows.append((hwnd, cmd))

            def EnumWindows(self, cb, lparam):
                for h in [1, 2, 3, 4]:
                    cb(h, lparam)
                return True

        mock_user = MockUser32()

        def mock_get_long(hwnd, idx):
            return WS_EX_APPWINDOW

        def mock_set_long(hwnd, idx, val):
            set_styles[hwnd] = val
            return val

        with mock.patch("core.window_guard.user32", mock_user), \
             mock.patch("core.window_guard.GetWindowLongPtrW", mock_get_long), \
             mock.patch("core.window_guard.SetWindowLongPtrW", mock_set_long), \
             mock.patch("core.window_guard.restore_foreground", lambda h: restored_fgs.append(h)):
            guard.suppress_windows()

        # Window 1 (ours): style updated to NOACTIVATE | TOOLWINDOW, moved to (-32000, -32000), hidden
        self.assertIn(1, set_styles)
        self.assertTrue(set_styles[1] & WS_EX_NOACTIVATE)
        self.assertTrue(set_styles[1] & WS_EX_TOOLWINDOW)
        self.assertFalse(set_styles[1] & WS_EX_APPWINDOW)
        self.assertEqual(moved_windows[1], (-32000, -32000, SWP_NOACTIVATE | SWP_NOZORDER))
        self.assertIn((1, SW_HIDE), hidden_windows)

        # Window 2 (user's personal Chrome): NEVER TOUCHED!
        self.assertNotIn(2, set_styles)
        self.assertNotIn(2, moved_windows)
        self.assertNotIn((2, SW_HIDE), hidden_windows)

        # Window 3 (Notepad): NEVER TOUCHED!
        self.assertNotIn(3, set_styles)
        self.assertNotIn(3, moved_windows)

        # Window 4 (our popup that stole FG): suppressed AND restored user's previous foreground!
        self.assertIn(4, set_styles)
        self.assertIn(1234, restored_fgs)

    def test_other_apps_windows_are_never_touched(self):
        """Electron apps (Claude, Discord, VS Code...) use Chrome's window class, and
        every minimized window sits at (-32000, -32000). Before the session's own
        processes are known -- and when looking them up comes back empty -- the
        guard hid such windows and took them off the taskbar; they could not be
        brought back without restarting the app."""
        from unittest import mock
        import core.window_guard as wg

        procs = {  # pid -> (exe, command line)
            100: ("Claude.exe", "Claude.exe --type=browser"),
            200: ("Discord.exe", "Discord.exe"),
            300: ("chrome.exe", "chrome.exe --profile-directory=Default"),          # the user's
            400: ("chrome.exe", "chrome.exe --test-type=webdriver --remote-debugging-port=0"),
        }
        windows = {1: 100, 2: 200, 3: 300, 4: 400, 5: 100}
        rects = {5: (-32000, -32000)}                                              # minimized

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

            def name(self):
                return procs[self.pid][0]

            def cmdline(self):
                return procs[self.pid][1].split()

        touched = set()

        class FakeUser32:
            def GetForegroundWindow(self):
                return 0

            def GetWindowThreadProcessId(self, hwnd, pid_ref):
                pid_ref._obj.value = windows[hwnd]
                return 1

            def GetClassNameW(self, hwnd, buf, n):
                buf.value = "Chrome_WidgetWin_1"
                return len(buf.value)

            def GetWindowRect(self, hwnd, rect_ref):
                rect_ref._obj.left, rect_ref._obj.top = rects.get(hwnd, (100, 100))

            def SetWindowPos(self, hwnd, *a):
                touched.add(hwnd)

            def IsWindowVisible(self, hwnd):
                return True

            def ShowWindow(self, hwnd, cmd):
                touched.add(hwnd)

            def EnumWindows(self, cb, lparam):
                for h in windows:
                    cb(h, lparam)
                return True

        wg._automation_cache.clear()
        with mock.patch.object(wg, "user32", FakeUser32()), \
                mock.patch.object(wg, "GetWindowLongPtrW", lambda h, i: 0), \
                mock.patch.object(wg, "SetWindowLongPtrW", lambda h, i, v: touched.add(h)), \
                mock.patch.object(wg, "restore_foreground", lambda h: None), \
                mock.patch("psutil.Process", FakeProcess):
            for prelaunch, target in ((True, set()), (False, set())):     # unknown / lookup empty
                touched.clear()
                guard = wg.WindowGuard(headless=True)
                guard.headless = True
                guard.prelaunch, guard.target_pids, guard.existing_pids = prelaunch, target, {300}
                guard.suppress_windows()
                self.assertEqual({4}, touched, (prelaunch, target))     # only Selenium's Chrome
        wg._automation_cache.clear()

    def test_window_guard_driver_wrapping(self):
        from unittest import mock
        from core.window_guard import WindowGuard

        guard = WindowGuard(headless=True)
        mock_driver = mock.MagicMock()
        mock_driver.service.process.pid = 5555

        orig_switch = mock_driver.switch_to.window
        orig_quit = mock_driver.quit

        with mock.patch.object(guard, "suppress_windows") as mock_suppress, \
             mock.patch("core.window_guard.get_session_chrome_pids", return_value={5555}):
            guard.attach_driver(mock_driver)

            # Target PIDs mapped
            self.assertIn(5555, guard.target_pids)
            self.assertFalse(guard.prelaunch)
            self.assertTrue(mock_suppress.called)

            # Test switch_to.window wrapper
            mock_suppress.reset_mock()
            mock_driver.switch_to.window("tab-2")
            orig_switch.assert_called_with("tab-2")
            self.assertTrue(mock_suppress.called)

            # Test quit wrapper stops guard
            with mock.patch.object(guard, "stop") as mock_stop:
                mock_driver.quit()
                self.assertTrue(mock_stop.called)
                self.assertTrue(orig_quit.called)


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
        import faulthandler
        was_enabled = faulthandler.is_enabled()

        def release_native_file():
            # install() points faulthandler at last_native_crash.txt and keeps it
            # open; until it lets go, Windows won't delete the folder (runs before
            # the rmtree above: cleanups go last-in, first-out).
            faulthandler.disable()
            if error_report._native is not None:
                error_report._native.close()
                error_report._native = None
            if was_enabled:
                faulthandler.enable()
        self.addCleanup(release_native_file)
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
        # No Watchlist: with entries, building the tabs schedules the 8-second
        # Watchlist check, which fired during later tests and started a real
        # headless Chrome that outlived the run (and held its output pipe open).
        no_watchlist = mock.patch("ui.app_window.get_watchlist", return_value=[])
        no_watchlist.start()
        self.addCleanup(no_watchlist.stop)
        w = AppWindow()

        def dispose():
            w._tabs_built = True          # the 1.5 s fallback must not build them later
            w.hide()
            w.deleteLater()
        self.addCleanup(dispose)
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

    def test_no_tooltip_for_a_widget_hidden_during_the_delay(self):
        # A dialog closed with the mouse over a hint: the tooltip, its own window,
        # would show after the delay and never be hidden again.
        from PyQt6.QtWidgets import QLabel
        from qfluentwidgets import ToolTipFilter
        label = QLabel("まかでみWAっしょい!")
        label.setToolTip("まかでみWAっしょい!")
        self.addCleanup(label.deleteLater)
        label.show()
        self._hover(label)
        hint = next(c for c in label.children() if isinstance(c, ToolTipFilter))
        label.hide()
        hint.showToolTip()                     # the delay ends
        self.assertFalse(hint._tooltip is not None and hint._tooltip.isVisible())
        self.assertFalse(hint.isEnter)
        label.show()                           # shown again: hints work as before
        self._hover(label)
        hint.showToolTip()
        self.assertTrue(hint._tooltip.isVisible())
        hint.hideToolTip()
        # Hidden before it is deleted: a window deleted while shown counts as the
        # last one closing, and the quit that posts ends a later test's event loop.
        label.hide()

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


class WindowPlacementTests(unittest.TestCase):
    """The window opens wholly on a display (ui/app_window.place_window)."""

    def setUp(self):
        from PyQt6.QtCore import QRect
        from ui.app_window import place_window
        self.QRect, self.place = QRect, place_window
        # A main display (taskbar left out) and one to its left, in Windows' pixels.
        self.main = QRect(0, 0, 2560, 1380)
        self.left = QRect(-2400, 151, 2400, 1290)

    def at(self, x, y, w, h, screens=None):
        settings = {"window_x": x, "window_y": y, "window_width": w, "window_height": h}
        r = self.place(settings, screens or [self.main, self.left], self.main)
        return (r.x(), r.y(), r.width(), r.height())

    def test_a_window_wholly_on_a_display_stays_where_it_was(self):
        self.assertEqual((300, 200, 1100, 800), self.at(300, 200, 1100, 800))
        self.assertEqual((-2000, 300, 1100, 800), self.at(-2000, 300, 1100, 800))  # the left display

    def test_its_bottom_off_the_display_moves_it_to_the_centre(self):
        # 1170 tall at y 366 ends at 1536, below the 1380 px area.
        self.assertEqual((730, 105, 1100, 1170), self.at(450, 366, 1100, 1170))
        # The sandbox's window as Qt measures it at 125%: taller than the display.
        self.assertEqual((474, 0, 1100, 1104),
                         self.at(450, 366, 1100, 1170, [self.QRect(0, 0, 2048, 1104)]))

    def test_off_any_edge_centres_it_on_the_display_it_was_mostly_on(self):
        self.assertEqual((730, 290, 1100, 800), self.at(2000, 300, 1100, 800))     # right edge
        self.assertEqual((730, 290, 1100, 800), self.at(400, -50, 1100, 800))      # above the top
        self.assertEqual((-1750, 396, 1100, 800), self.at(-2600, 300, 1100, 800))  # the left one's edge

    def test_straddling_two_displays_goes_to_the_one_with_more_of_it(self):
        self.assertEqual((730, 290, 1100, 800), self.at(-300, 300, 1100, 800))
        self.assertEqual((-1750, 396, 1100, 800), self.at(-800, 300, 1100, 800))

    def test_on_no_display_any_more_it_opens_on_the_main_one(self):
        self.assertEqual((730, 290, 1100, 800), self.at(-2000, 300, 1100, 800, [self.main]))
        self.assertEqual((730, 290, 1100, 800), self.at(9000, 9000, 1100, 800))

    def test_bigger_than_the_display_is_shrunk_to_fit(self):
        self.assertEqual((0, 0, 2560, 1380), self.at(0, 0, 3000, 1600))
        self.assertEqual((-2400, 151, 2400, 1290), self.at(-2400, 151, 2600, 1400))

    def test_never_saved_or_broken_settings_open_centred(self):
        r = self.place({}, [self.main, self.left], self.main)
        self.assertEqual((730, 290, 1100, 800), (r.x(), r.y(), r.width(), r.height()))
        r = self.place({"window_x": "junk", "window_width": None}, [self.main], self.main)
        self.assertEqual((730, 290, 1100, 800), (r.x(), r.y(), r.width(), r.height()))
        r = self.place({}, [], self.main)                                 # no display listed
        self.assertEqual((730, 290, 1100, 800), (r.x(), r.y(), r.width(), r.height()))

    def test_the_window_uses_it(self):
        import inspect
        from ui import app_window
        self.assertIn("place_window(", inspect.getsource(app_window.AppWindow.__init__))


if __name__ == "__main__":
    unittest.main(verbosity=2)
