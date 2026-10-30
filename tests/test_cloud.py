"""Notifications and remote control: Windows and Discord notices, the cloud checker, remote download commands and the watcher.

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


class CloudPollingTests(unittest.TestCase):
    """Polling every 5 s plus a heartbeat every 15 s was about 23,000 requests a day
    per PC, and it used up the service host's free monthly bandwidth. One request
    every 30 s does both jobs: the service records a commands poll as a heartbeat."""

    def test_poll_interval_keeps_the_pc_online(self):
        import core.watcher as w
        self.assertEqual(30, w.POLL_SECONDS)
        # Two polls fit in the service's 180 s online window, so one lost request
        # never shows the PC as offline.
        self.assertLess(w.POLL_SECONDS * 2, 180)

    def test_watcher_makes_one_request_per_poll(self):
        from unittest import mock
        import core.watcher as w
        self.assertFalse(hasattr(w, "send_heartbeat"))
        with mock.patch.object(w, "load_cloud_credentials", return_value=("https://svc", "sid", "tok", True)), \
             mock.patch.object(w, "is_main_app_running", return_value=False), \
             mock.patch.object(w, "fetch_commands", return_value=[]) as fetch, \
             mock.patch.object(w.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(w, "log"):
            w.run_watcher(single_pass=True)
        fetch.assert_called_once_with("https://svc", "sid", "tok")
        urlopen.assert_not_called()

    def test_main_app_poll_is_one_request(self):
        import threading
        import types
        from unittest import mock
        from ui.app_window import AppWindow
        import utils.config as config
        self.assertFalse(hasattr(config, "cloud_send_heartbeat"))
        done = threading.Event()
        calls = []

        def fetch():
            calls.append("commands")
            done.set()
            return []

        window = types.SimpleNamespace()
        with mock.patch.object(config, "cloud_fetch_commands", fetch):
            AppWindow._poll_cloud_commands(window)
            self.assertTrue(done.wait(5))
        self.assertEqual(["commands"], calls)

    def test_main_app_uses_the_watcher_interval(self):
        import inspect
        from ui.app_window import AppWindow
        src = inspect.getsource(AppWindow)
        self.assertIn("from core.watcher import POLL_SECONDS", src)
        self.assertIn("self._cloud_poll_timer.start(POLL_SECONDS * 1000)", src)
        self.assertNotIn("start(5 * 1000)", src)


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


class DiscordRepeatTests(unittest.TestCase):
    """One new episode, one Discord message. The 30-minute check re-posted the same
    episode every half hour until it was downloaded from the Watchlist, and posted
    a second copy beside the cloud service's own."""

    W = {"url": "https://witanime.site/anime/black-clover-2nd-season/", "domain": "witanime.site",
         "seen_max": 1, "latest_max": 2}
    A = {"url": "https://eta.animerco.org/animes/x/", "domain": "eta.animerco.org", "seen_max": 1}

    def test_each_episode_is_announced_once(self):
        from ui.watchlist_tab import episodes_to_announce
        self.assertEqual([2], episodes_to_announce(self.A, 2, cloud_on=False))
        self.assertEqual([], episodes_to_announce(dict(self.A, notified_max=2), 2, cloud_on=False))
        self.assertEqual([3, 4], episodes_to_announce(dict(self.A, notified_max=2), 4, cloud_on=False))
        self.assertEqual([], episodes_to_announce(dict(self.A, seen_max=None), 2, cloud_on=False))

    def test_cloud_announces_the_sites_it_checks(self):
        from ui.watchlist_tab import episodes_to_announce
        self.assertEqual([], episodes_to_announce(self.W, 2, cloud_on=True))
        self.assertEqual([2], episodes_to_announce(self.W, 2, cloud_on=False))
        self.assertEqual([2], episodes_to_announce(self.A, 2, cloud_on=True))   # cloud can't reach it

    def test_a_check_records_what_was_announced(self):
        from unittest import mock
        from ui import watchlist_tab as wt
        store = [dict(self.A)]
        updates = []

        def update(url, **fields):
            updates.append(fields)
            store[0].update(fields)

        fake = mock.MagicMock()
        fake._cards = {}
        with mock.patch.object(wt, "update_watch", side_effect=update), \
                mock.patch.object(wt, "get_watchlist", return_value=store), \
                mock.patch.dict(wt.app_settings, {"discord_webhook": "", "cloud_notify_enabled": False}), \
                mock.patch("ui.search_tab.raise_episode_bound", return_value=None):
            for _ in range(4):                                 # four half-hourly checks
                wt.WatchlistWidget._on_entry_done(fake, self.A["url"], 2, 1, "", False)
        self.assertEqual(2, store[0]["notified_max"])
        self.assertEqual(1, sum(1 for u in updates if "notified_max" in u))

    def test_a_discord_download_counts_the_episodes_as_taken(self):
        from unittest import mock
        from ui.app_window import AppWindow
        fake = mock.MagicMock()
        fake.watchlist_interface = None
        with mock.patch("utils.config.update_watch") as update:
            AppWindow._acknowledge_remote(fake, dict(self.W, seen_max=1, latest_max=3), "2")
            update.assert_called_once_with(self.W["url"], seen_max=2, new_count=1, notified_max=2)
            update.reset_mock()
            AppWindow._acknowledge_remote(fake, dict(self.W, seen_max=1, latest_max=3), "3")
            update.assert_not_called()                     # ep 2 skipped: stays new


if __name__ == "__main__":
    unittest.main(verbosity=2)
