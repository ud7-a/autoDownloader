"""Regression tests for preference ownership, downloads, and cloud settings."""

import copy
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("AED_APP_DIR", os.path.join(tempfile.gettempdir(), "AutoEpisodesDownloader-tests"))

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
from ui import downloader_tab, settings_tab
from utils import config
from utils.sounds import SOUND_NONE


class SettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.saved_settings = copy.deepcopy(config.app_settings)
        self.saved_sites = copy.deepcopy(config.sites_data)
        self.tmp = tempfile.mkdtemp(prefix="aed_settings_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._restore_config)
        config.app_settings.update(
            unfinished_session=None, cloud_notify_enabled=False,
            cloud_subscriber_id="", discord_webhook="", download_dir=self.tmp,
            custom_sounds=[], selected_sound="", headless=True,
            concurrency=3, concurrency_auto=True, volume=75,
        )
        config.sites_data.clear()
        config.sites_data["Show"] = {
            "url": "https://witanime.site/episode/show-{x}/",
            "step_paths": {"mirror": [{"xpath": "//a"}]},
            "episode_min": 1, "episode_max": 3,
        }
        self.widgets = []
        self.addCleanup(self._dispose_widgets)
        for target in ("ui.settings_tab.save_config", "ui.downloader_tab.save_config",
                       "ui.settings_tab.start_background_watcher",
                       "ui.settings_tab.set_windows_autostart",
                       "ui.settings_tab.SettingsWidget._message"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_config(self):
        config.app_settings.clear()
        config.app_settings.update(self.saved_settings)
        config.sites_data.clear()
        config.sites_data.update(self.saved_sites)

    def _dispose_widgets(self):
        for widget in self.widgets:
            for name in ("_save_timer", "_cloud_poll_timer", "_watchlist_timer"):
                timer = getattr(widget, name, None)
                if timer:
                    timer.stop()
            if hasattr(widget, "_tabs_built"):
                widget._tabs_built = True
            widget.hide()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def settings(self):
        widget = settings_tab.SettingsWidget()
        self.widgets.append(widget)
        return widget

    def downloader(self):
        widget = downloader_tab.DownloaderWidget()
        self.widgets.append(widget)
        return widget

    def wait_for_cloud(self, widget):
        deadline = time.monotonic() + 3
        while widget._cloud_busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertFalse(widget._cloud_busy, "the queued cloud result must reach the UI")

    def test_defaults_are_written_without_an_anime_profile(self):
        config.sites_data.clear()
        settings = self.settings()
        settings.chk_headless.setChecked(False)
        settings.chk_auto_concurrency.setChecked(False)
        settings.spin_concurrency.setValue(5)
        settings.slider_vol.setValue(42)
        settings.chk_win_notify.setChecked(False)
        self.assertTrue(settings._save_timer.isActive())
        path = os.path.join(self.tmp, "settings.json")
        with mock.patch.object(config, "CONFIG_FILE", path):
            config.save_config()
        with open(path, encoding="utf-8") as saved_file:
            saved = json.load(saved_file)["settings"]
        self.assertEqual((saved["headless"], saved["concurrency_auto"],
                          saved["concurrency"], saved["volume"],
                          saved["windows_notifications"]), (False, False, 5, 42, False))

    def test_new_download_reads_the_preferences_saved_in_settings(self):
        settings = self.settings()
        downloader = self.downloader()
        settings.chk_headless.setChecked(False)
        settings.chk_auto_concurrency.setChecked(False)
        settings.spin_concurrency.setValue(4)
        settings.slider_vol.setValue(35)
        settings.combo_sound.setCurrentIndex(0)
        settings.txt_webhook.setText("https://discord.com/api/webhooks/123/test")
        settings.save_discord()
        with mock.patch.object(downloader, "_begin_download") as begin:
            downloader.start_watch_download(
                "Show", "https://witanime.site/episode/show-{x}/", "witanime.site", "1-2")
        params = begin.call_args.args[0]
        self.assertEqual((params["headless"], params["concurrency"],
                          params["concurrency_auto"], params["volume"],
                          params["selected_sound"]), (False, 4, False, 35, SOUND_NONE))
        self.assertEqual(params["webhook"], "https://discord.com/api/webhooks/123/test")

    def test_episode_edits_do_not_overwrite_app_preferences(self):
        downloader = self.downloader()
        config.app_settings.update(headless=False, concurrency=6, concurrency_auto=False,
                                   volume=22, discord_webhook="saved-webhook")
        downloader.ep_picker.set_spec("2-3")
        downloader.save_settings()
        self.assertEqual(config.sites_data["Show"]["last_episodes"], "2-3")
        self.assertEqual((config.app_settings["headless"], config.app_settings["concurrency"],
                          config.app_settings["volume"], config.app_settings["discord_webhook"]),
                         (False, 6, 22, "saved-webhook"))
        self.assertFalse(hasattr(downloader, "txt_webhook"))
        self.assertFalse(hasattr(downloader, "spin_concurrency"))

    def test_resume_keeps_session_preferences_without_changing_new_defaults(self):
        downloader = self.downloader()
        downloader._session_at_launch = {
            "site": "Show", "episodes": [2], "target_dir": self.tmp,
            "headless": False, "webhook": "session-webhook", "volume": 18,
            "selected_sound": SOUND_NONE, "concurrency": 2, "concurrency_auto": False,
        }
        with mock.patch("qfluentwidgets.MessageBox") as dialog, \
                mock.patch.object(downloader, "_begin_download") as begin:
            dialog.return_value.exec.return_value = True
            downloader.check_and_prompt_resume()
        params = begin.call_args.args[0]
        self.assertEqual((params["headless"], params["volume"], params["concurrency_auto"]),
                         (False, 18, False))
        self.assertEqual(params["webhook"], "session-webhook")
        self.assertTrue(config.app_settings["headless"])
        self.assertTrue(config.app_settings["concurrency_auto"])
        self.assertEqual(config.app_settings["volume"], 75)

    def test_download_thread_and_recovery_use_the_same_concurrency_snapshot(self):
        downloader = self.downloader()
        params = dict(site="Show", episodes_list=[1], target_dir=self.tmp,
                      headless=True, webhook="", selected_sound=SOUND_NONE,
                      volume=0, concurrency=2, concurrency_auto=False)
        config.app_settings["concurrency_auto"] = True
        with mock.patch.object(downloader_tab, "signals"), \
                mock.patch.object(downloader_tab.threading, "Thread") as thread:
            downloader._begin_download(params)
        self.assertFalse(thread.call_args.kwargs["kwargs"]["concurrency_auto"])
        self.assertFalse(config.app_settings["unfinished_session"]["concurrency_auto"])

    def test_none_sound_and_mute_preserve_choices(self):
        settings = self.settings()
        settings.toggle_mute()
        self.assertEqual(config.app_settings["volume"], 0)
        settings.toggle_mute()
        self.assertEqual(config.app_settings["volume"], 75)
        settings.combo_sound.setCurrentIndex(0)
        self.assertEqual(config.app_settings["selected_sound"], SOUND_NONE)
        self.assertTrue(settings.volume_container.isHidden())
        settings.load_defaults()
        self.assertEqual(settings.combo_sound.currentData(), SOUND_NONE)

    def test_invalid_cloud_inputs_do_not_start_a_registration(self):
        settings = self.settings()
        with mock.patch.object(settings_tab, "cloud_register_and_sync") as register:
            settings.chk_cloud.setChecked(True)
            self.assertFalse(settings.chk_cloud.isChecked())
            self.assertFalse(config.app_settings["cloud_notify_enabled"])
            settings.txt_cloud_url.setText("https://[")
            settings.save_discord()
            register.assert_not_called()
        self.assertEqual(config.app_settings["discord_webhook"], "")

    def test_cloud_registration_result_returns_to_the_ui(self):
        settings = self.settings()
        settings.txt_webhook.setText("https://discord.com/api/webhooks/123/test")

        def register(service_url, webhook):
            config.app_settings.update(cloud_notify_enabled=True, cloud_subscriber_id="test")
            return True, "Connected"

        with mock.patch.object(settings_tab, "cloud_register_and_sync", side_effect=register):
            settings.chk_cloud.setChecked(True)
            self.wait_for_cloud(settings)
        settings_tab.start_background_watcher.assert_called_once()
        self.assertTrue(settings.chk_cloud.isChecked())
        self.assertTrue(settings.btn_save_discord.isEnabled())
        self.assertIn("Active", settings.lbl_cloud_status.text())

    def test_failed_cloud_registration_restores_controls(self):
        settings = self.settings()
        settings.txt_webhook.setText("https://discord.com/api/webhooks/123/test")
        with mock.patch.object(settings_tab, "cloud_register_and_sync",
                               return_value=(False, "Unavailable")):
            settings.chk_cloud.setChecked(True)
            self.wait_for_cloud(settings)
        self.assertFalse(settings.chk_cloud.isChecked())
        self.assertTrue(settings.txt_webhook.isEnabled())
        settings_tab.start_background_watcher.assert_not_called()

    def test_disabling_cloud_updates_registration_and_autostart(self):
        config.app_settings.update(cloud_notify_enabled=True, cloud_subscriber_id="test")
        settings = self.settings()

        def unsubscribe():
            config.app_settings.update(cloud_notify_enabled=False, cloud_subscriber_id="")

        with mock.patch.object(settings_tab, "cloud_unsubscribe", side_effect=unsubscribe) as remove:
            settings.chk_cloud.setChecked(False)
            self.wait_for_cloud(settings)
        remove.assert_called_once()
        settings_tab.set_windows_autostart.assert_called_once_with(False)
        self.assertFalse(settings.chk_cloud.isChecked())

    def test_settings_navigation_and_cloud_polling_apply_without_a_restart(self):
        from ui.app_window import AppWindow
        with mock.patch("ui.app_window.get_watchlist", return_value=[]), \
                mock.patch.object(AppWindow, "_poll_cloud_commands"):
            window = AppWindow()
            self.widgets.append(window)
            self.assertIsNone(window.settings_interface)
            window.downloader_interface.settings_requested.emit()
            self.assertIs(window.stackedWidget.currentWidget(), window.settings_interface)
            self.assertFalse(window._cloud_poll_timer.isActive())
            config.app_settings["cloud_notify_enabled"] = True
            window.settings_interface.cloud_changed.emit()
            self.assertTrue(window._cloud_poll_timer.isActive())
            config.app_settings["cloud_notify_enabled"] = False
            window.settings_interface.cloud_changed.emit()
            self.assertFalse(window._cloud_poll_timer.isActive())
