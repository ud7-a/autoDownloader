"""Video Player: the bundled mpv.net config and scripts, skip openings and endings, and shortcuts on non-Latin keyboard layouts.

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


class MpvnetTests(unittest.TestCase):
    """The Video Player tab's mpv.net helpers (utils/mpvnet.py)."""

    def setUp(self):
        from utils import mpvnet
        self.mpvnet = mpvnet
        self.cfg = tempfile.mkdtemp(prefix="aed_mpvcfg_")
        # mpv.net config copies land here; left behind, 2,449 of these folders
        # had piled up to 139 MB in %TEMP%.
        self.addCleanup(shutil.rmtree, self.cfg, True)

    def test_bundle_is_complete(self):
        base = self.mpvnet.bundle_dir()
        for name in self.mpvnet.BUNDLE_FILES:
            self.assertTrue(os.path.isfile(os.path.join(base, name)), name)
        shaders = self.mpvnet.bundled_shaders()
        self.assertIn("Anime4K_Upscale_CNN_x2_UL.glsl", shaders)
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
        # Smooth timing is global, so the guard that drops back to audio sync when
        # a profile is too heavy for the GPU (F3 stuttered on an RTX 3050) ships too.
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, "scripts", "sync-guard.lua")))
        self.assertTrue(self.mpvnet.shaders_applied(self.cfg))

        # Re-applying the same thing has nothing to back up.
        self.assertIsNone(self.mpvnet.apply_config(self.cfg, r"C:\x\animes"))

    def test_config_current_notices_an_update(self):
        """Apply is manual, so a config change in an update (swapchain-depth for
        smooth timing on mid-range GPUs) reaches nobody unless the tab says so."""
        from unittest import mock
        current = self.mpvnet.config_current
        self.assertFalse(current(self.cfg, r"C:\x\animes"))             # never applied
        self.mpvnet.apply_config(self.cfg, r"C:\x\animes")
        self.assertTrue(current(self.cfg, r"C:\x\animes"))
        self.assertFalse(current(self.cfg, r"C:\x\Anime Downloads"))    # folder changed since
        real = self.mpvnet._bundle_plan
        newer = lambda d: [(r, b + b"\n# newer\n" if r == "mpv.conf" else b) for r, b in real(d)]  # noqa: E731
        with mock.patch.object(self.mpvnet, "_bundle_plan", newer):
            self.assertFalse(current(self.cfg, r"C:\x\animes"))         # the app was updated
            self.mpvnet.apply_config(self.cfg, r"C:\x\animes")
            self.assertTrue(current(self.cfg, r"C:\x\animes"))
        with open(os.path.join(self.cfg, "scripts", "sync-guard.lua"), "a") as f:
            f.write("-- edited\n")
        self.assertFalse(current(self.cfg, r"C:\x\animes"))             # any shipped file counts
        with mock.patch.object(self.mpvnet, "_bundle_plan", side_effect=FileNotFoundError):
            self.assertFalse(current(self.cfg, r"C:\x\animes"))

    def test_smooth_timing_on_mid_range_gpus(self):
        """Display sync with the UL anime chain needs the GPU to work ahead: with 3
        frames queued an RTX 3050 delayed 476 frames per 20 s at 300 Hz, with 8
        none -- and only on D3D11 (Vulkan still delayed 600+)."""
        with open(os.path.join(self.mpvnet.bundle_dir(), "mpv.conf"), encoding="utf-8") as f:
            conf = f.read()
        self.assertRegex(conf, r"(?m)^gpu-api=d3d11\s*$")
        self.assertRegex(conf, r"(?m)^swapchain-depth=8\s*$")
        self.assertRegex(conf, r"(?m)^video-sync=display-resample\s*$")
        self.assertNotIn("4090", conf)

    def test_shaders_applied_needs_conf_to_load_them(self):
        self.mpvnet.apply_config(self.cfg)
        with open(os.path.join(self.cfg, "mpv.conf"), "w") as f:
            f.write("vo=gpu-next\n")
        self.assertFalse(self.mpvnet.shaders_applied(self.cfg))

    def test_profiles_parsed_and_described(self):
        from ui.player_tab import SHADER_INFO, PROFILE_TITLES
        profiles = dict(self.mpvnet.bundled_profiles())
        # Two profiles: the former F3 "best" chain became the anime profile.
        self.assertEqual(["anime", "series"], list(profiles))
        # The largest Anime4K models, Soft Restore (no over-sharpened outlines), no
        # Thin, no SSimDownscaler -- and no KrigBilateral: it painted colour
        # fringes beside black line art.
        self.assertEqual(["Anime4K_Clamp_Highlights", "Anime4K_Restore_CNN_Soft_UL",
                          "Anime4K_Upscale_CNN_x2_UL"], profiles["anime"])
        # Nothing shipped that no profile loads (shaders_applied checks them all).
        used = {s for stems in profiles.values() for s in stems}
        self.assertEqual(used, {os.path.splitext(s)[0] for s in self.mpvnet.bundled_shaders()})
        # Series: FSRCNNX 8 then a light sharpen. KrigBilateral and SSimDownscaler
        # scored worse on live action, and full-strength sharpening painted skin
        # like oil paint, so the bundled copy is turned down to 0.2.
        self.assertEqual(["FSRCNNX_x2_8-0-4-1", "adaptive-sharpen"], profiles["series"])
        with open(os.path.join(self.mpvnet.bundle_dir(), "Shaders", "adaptive-sharpen.glsl"),
                  encoding="utf-8") as f:
            self.assertRegex(f.read(), r"(?m)^#define curve_height\s+0\.2\b")
        for name, stems in profiles.items():
            self.assertIn(name, PROFILE_TITLES)
            for stem in stems:
                self.assertIn(stem, SHADER_INFO, f"{stem} has no friendly name in the tab")

    def test_two_profiles_one_per_key(self):
        """The former F3 "best" chain is the anime profile (F1); F3 is gone. Both
        profiles set the same options, so switching never leaks one into the other."""
        import re
        base = self.mpvnet.bundle_dir()
        with open(os.path.join(base, "mpv.conf"), encoding="utf-8") as f:
            conf = f.read()
        blocks = dict(re.findall(r"^\[([^\]]+)\]\n(.*?)(?=^\[|\Z)", conf, re.M | re.S))
        self.assertEqual(["anime", "series"], sorted(blocks))

        def keys(block):
            return {line.split("=", 1)[0] for line in block.splitlines()
                    if "=" in line and not line.startswith("#")}
        anime = blocks["anime"]
        self.assertEqual(keys(anime), keys(blocks["series"]))
        # ewa_lanczos: high quality without the sharpening of its -sharp variant,
        # for chroma too (KrigBilateral fringed lines); downscaled in gamma light.
        for line in ("dscale=ewa_lanczos", "cscale=ewa_lanczos", "linear-downscaling=no"):
            self.assertIn(line + "\n", anime)
        # Even pacing (display sync + oversample, no invented frames) for every
        # profile: set once globally, never overridden, so F12's manual switch to
        # audio sync survives F1/F2.
        for key in ("video-sync=display-resample", "interpolation=yes", "tscale=oversample"):
            self.assertRegex(conf, rf"(?m)^{key}$")
        for name, block in blocks.items():
            self.assertNotRegex(block, r"(?m)^(video-sync|interpolation|tscale)=", name)
        with open(os.path.join(base, "input.conf"), encoding="utf-8") as f:
            keys_conf = f.read()
        self.assertRegex(keys_conf, r"(?m)^F1\s+apply-profile anime\s")
        self.assertRegex(keys_conf, r"(?m)^F2\s+apply-profile series\s")
        # F3 bound to nothing, rather than left to mpv.net's own default.
        self.assertRegex(keys_conf, r"(?m)^F3\s+ignore\s*$")
        self.assertNotIn("anime-best", keys_conf + conf)

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
        cmd = self.mpvnet.winget_upgrade_command("winget")
        self.assertEqual(["winget", "upgrade", "--id", "mpv.net", "--exact"], cmd[:5])
        self.assertIn("--silent", cmd)

    def _release(self, body):
        class Response:
            def __init__(self):
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        return lambda req, timeout=None: Response()

    def test_mpvnet_update_check(self):
        m = self.mpvnet
        self.assertEqual((7, 1, 2), m.version_tuple("v7.1.2.0"))
        self.assertEqual(m.version_tuple("7.1.2"), m.version_tuple("7.1.2.0"))
        stable = {"tag_name": "v7.2.0.0", "html_url": "https://x/v7.2.0.0", "prerelease": False}
        self.assertEqual(("7.2.0.0", "https://x/v7.2.0.0"),
                         m.latest_release(opener=self._release(stable)))
        with self.assertRaises(ValueError):          # a beta is never offered
            m.latest_release(opener=self._release({"tag_name": "v7.3-beta", "prerelease": True}))
        fetch = lambda: ("7.2.0.0", "https://x")    # noqa: E731
        self.assertEqual("newer", m.check_update("7.1.2.0", fetch)["status"])
        self.assertEqual("current", m.check_update("7.2.0.0", fetch)["status"])
        self.assertEqual("current", m.check_update("7.2", fetch)["status"])
        self.assertEqual("current", m.check_update("8.0.0.0", fetch)["status"])  # ahead (a beta)
        self.assertEqual("unknown", m.check_update(None, fetch)["status"])

    def test_file_version_reads_the_exe_resource(self):
        if sys.platform != "win32":
            self.skipTest("Windows only")
        self.assertRegex(self.mpvnet.file_version(sys.executable) or "", r"^\d+\.\d+\.\d+\.\d+$")
        self.assertIsNone(self.mpvnet.file_version(os.path.join(self.cfg, "missing.exe")))
        self.assertIsNone(self.mpvnet.file_version(None))

    def _player_tab(self, version="7.1.2.0"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        type(self)._app = QApplication.instance() or QApplication([])   # alive for the run
        m = self.mpvnet
        state = {"version": version}
        exe = os.path.join(self.cfg, "mpvnet.exe")
        for p in (mock.patch.object(m, "find_mpvnet", side_effect=lambda: (exe, state["version"])),
                  mock.patch.object(m, "winget_path", return_value="winget"),
                  mock.patch.object(m, "default_player_status", return_value={".mp4": True,
                                                                              ".mkv": True}),
                  mock.patch.object(m, "policy_active", return_value=False),
                  mock.patch.object(m, "config_dir", return_value=self.cfg),
                  mock.patch("ui.player_tab.InfoBar")):
            p.start()
            self.addCleanup(p.stop)
        from ui.player_tab import PlayerWidget
        w = PlayerWidget()
        self.addCleanup(w.deleteLater)
        w.refresh_status()
        return w, state

    def test_skip_card_saves_what_the_player_does(self):
        from utils import skip_times
        w, _state = self._player_tab()
        boxes = w.skip_boxes
        self.assertEqual(["Ask (press K)", "Off"],
                         [boxes["opening"].currentText(), boxes["ending"].currentText()])
        # The skip script is put in place without applying (overwriting) the config.
        self.assertTrue(os.path.isfile(os.path.join(self.cfg, "scripts", "aed-skip.lua")))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "mpv.conf")))
        self.assertEqual("On", w.skip_pill.text())
        boxes["opening"].setCurrentIndex(2)            # Skip automatically
        boxes["ending"].setCurrentIndex(1)             # Ask
        self.assertEqual({"opening": "auto", "ending": "ask"}, skip_times.read_settings(self.cfg))
        boxes["opening"].setCurrentIndex(0)
        boxes["ending"].setCurrentIndex(0)
        self.assertEqual("Off", w.skip_pill.text())
        # Re-reading the tab shows what's saved.
        w.refresh_status()
        self.assertEqual("Off", boxes["opening"].currentText())

    def test_the_tab_offers_a_config_update(self):
        from unittest import mock
        w, _state = self._player_tab()
        self.assertEqual(("Not applied", "Apply shaders"), (w.shader_pill.text(), w.btn_apply.text()))
        self.mpvnet.apply_config(self.cfg, None)
        with mock.patch.object(self.mpvnet, "config_current", return_value=True):
            w.refresh_status()
        self.assertEqual(("Applied", "Re-apply shaders"), (w.shader_pill.text(), w.btn_apply.text()))
        with mock.patch.object(self.mpvnet, "config_current", return_value=False):
            w.refresh_status()
        self.assertEqual(("Update ready", "Apply the update"), (w.shader_pill.text(), w.btn_apply.text()))

    def test_page_fits_the_tab_side_by_side(self):
        """The page never scrolls sideways, so a page wider than the tab is cut
        off: the three profile panels, each one line per shader with the purpose
        unwrapped, needed more than a 1580 px tab and clipped every right-hand
        control (status pills, Open mpv.net, the switch, Re-apply, the skip boxes)."""
        w, _state = self._player_tab()
        w.resize(w.STACK_PANELS_BELOW + 20, 900)          # widest layout: side by side
        page = w.scroll.widget()
        self.assertEqual(w._panels.direction(), w._panels.Direction.LeftToRight)
        self.assertLessEqual(page.minimumSizeHint().width(), w.width() - 20)

    def test_check_button_offers_a_newer_mpvnet(self):
        from ui import player_tab
        w, _state = self._player_tab()
        self.assertEqual("Check for updates", w.btn_recheck.text())
        self.assertFalse(w.btn_update.isVisibleTo(w))
        w._on_update_checked({"status": "newer", "latest": "7.2.0.0", "url": "https://x",
                              "installed": "7.1.2.0"})
        self.assertTrue(w.btn_update.isVisibleTo(w))
        self.assertEqual("Update to 7.2.0.0", w.btn_update.text())
        self.assertEqual("7.2.0.0 available", w.player_pill.text())
        self.assertIn("you have 7.1.2.0", w.lbl_install_line.text())
        w._on_update_checked({"status": "current", "latest": "7.2.0.0", "url": "https://x",
                              "installed": "7.2.0.0"})
        self.assertFalse(w.btn_update.isVisibleTo(w))
        player_tab.InfoBar.success.assert_called_once()
        w._on_update_checked({"error": "offline"})
        self.assertIn("offline", player_tab.InfoBar.warning.call_args[0][1])

    def test_update_result_is_reported(self):
        from ui import player_tab
        w, state = self._player_tab()
        newer = {"status": "newer", "latest": "7.2.0.0", "url": "https://x/rel",
                 "installed": "7.1.2.0"}
        # winget's catalog hasn't got it yet -> the GitHub download page instead.
        w._on_update_checked(dict(newer))
        w._on_update_done(self.mpvnet.WINGET_NO_UPGRADE)
        self.assertIn("doesn't offer 7.2.0.0 yet", player_tab.InfoBar.warning.call_args[0][1])
        self.assertTrue(w.btn_update.isVisibleTo(w))                # still on offer
        # Not a winget install.
        w._on_update_done(self.mpvnet.WINGET_NOT_FOUND)
        self.assertIn("wasn't installed with winget", player_tab.InfoBar.warning.call_args[0][1])
        # Updated: the version read back is the new one.
        state["version"] = "7.2.0.0"
        w._on_update_done(0)
        self.assertFalse(w.btn_update.isVisibleTo(w))
        self.assertIn("7.2.0.0", player_tab.InfoBar.success.call_args[0][1])
        self.assertEqual("Installed 7.2.0.0", w.player_pill.text())

    def test_update_waits_for_mpvnet_to_close(self):
        from unittest import mock
        from ui import player_tab
        w, _state = self._player_tab()
        w._on_update_checked({"status": "newer", "latest": "7.2.0.0", "url": "https://x",
                              "installed": "7.1.2.0"})
        with mock.patch.object(self.mpvnet, "is_running", return_value=True), \
                mock.patch.object(player_tab, "WingetInstallThread") as thread:
            w.update_mpvnet()
        thread.assert_not_called()
        self.assertEqual("Close mpv.net first", player_tab.InfoBar.warning.call_args[0][0])


class KeyLayoutTests(unittest.TestCase):
    """mpv names a letter key by what the active layout types: on an Arabic layout
    K arrives as "ن" and no shortcut fired. scripts/key-layouts.lua presses the
    Latin key instead. Run against mpv.net's own libmpv when it is installed."""

    def test_non_latin_keys_reach_the_latin_bindings(self):
        import ctypes
        import time
        from utils import intro_detect, mpvnet
        dll = intro_detect.find_libmpv()
        if not dll:
            self.skipTest("mpv.net is not installed")
        lib = intro_detect._load(dll)
        lib.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.mpv_get_property_string.restype = ctypes.c_void_p
        lib.mpv_free.argtypes = [ctypes.c_void_p]
        tmp = tempfile.mkdtemp(prefix="aed_keys_")
        self.addCleanup(shutil.rmtree, tmp, True)
        conf = os.path.join(tmp, "input.conf")
        with open(conf, "w", encoding="utf-8") as f:
            f.write("k set user-data/hit k\nCtrl+s set user-data/hit ctrl-s\n"
                    "S set user-data/hit S\n; set user-data/hit semicolon\n"
                    "ق set user-data/hit own\n")
        script = os.path.join(mpvnet.bundle_dir(), "scripts", "key-layouts.lua")
        h = lib.mpv_create()
        self.assertTrue(h)
        self.addCleanup(lib.mpv_terminate_destroy, h)
        # config=no and load-scripts=no: nothing of the user's mpv.net is read.
        for k, v in {"config": "no", "load-scripts": "no", "scripts": script, "idle": "yes",
                     "input-default-bindings": "yes", "input-conf": conf, "terminal": "no",
                     "vo": "null", "ao": "null"}.items():
            lib.mpv_set_option_string(h, k.encode(), v.encode("utf-8"))
        self.assertEqual(0, lib.mpv_initialize(h))

        def command(*args):
            arr = (ctypes.c_char_p * (len(args) + 1))(*[a.encode("utf-8") for a in args], None)
            lib.mpv_command(h, arr)

        def press(key):
            command("set", "user-data/hit", "-")
            command("keypress", key)
            deadline = time.monotonic() + 2
            value = "-"
            while value == "-" and time.monotonic() < deadline:
                lib.mpv_wait_event(h, 0.05)
                p = lib.mpv_get_property_string(h, b"user-data/hit")
                value = ctypes.string_at(p).decode("utf-8").strip('"') if p else None
                if p:
                    lib.mpv_free(p)
            return value

        deadline = time.monotonic() + 10                  # the script loads in parallel
        while press("ن") != "k" and time.monotonic() < deadline:
            pass
        self.assertEqual("k", press("ن"))                 # Arabic K
        self.assertEqual("ctrl-s", press("Ctrl+س"))       # with a modifier
        self.assertEqual("S", press("ٍ"))                 # Shift layer
        self.assertEqual("semicolon", press("ك"))         # a key that needs quoting
        self.assertEqual("k", press("л"))                 # Russian
        self.assertEqual("k", press("ל"))                 # Hebrew
        self.assertEqual("own", press("ق"))               # input.conf still wins


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


class SkipTimesTests(unittest.TestCase):
    """Openings/endings found from the audio (utils/intro_detect.py), saved beside
    the episodes and offered by scripts/aed-skip.lua (utils/skip_times.py)."""

    RATE = 11025

    def setUp(self):
        import numpy as np
        from utils import intro_detect, skip_times
        self.np, self.d, self.st = np, intro_detect, skip_times
        self.tmp = tempfile.mkdtemp(prefix="aed_skiptest_")
        self.rng = np.random.default_rng(7)
        self.song = self.noise(90)          # the "opening": the same in every episode
        self.ending = self.noise(85)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def noise(self, seconds):
        return self.rng.standard_normal(int(seconds * self.RATE)).astype("float32") * 3000

    def episode(self, seconds, op_at=None, ed_at=None, dialogue=0.0, song_len=90):
        audio = self.noise(seconds)
        r = self.RATE
        if op_at is not None:
            song = self.song[:int(song_len * r)]
            seg = song + (self.noise(len(song) / r) * dialogue if dialogue else 0)
            audio[int(op_at * r):int(op_at * r) + len(seg)] = seg
        if ed_at is not None:
            audio[int(ed_at * r):int(ed_at * r) + len(self.ending)] = self.ending
        return audio

    def test_shared_opening_found_where_each_episode_puts_it(self):
        a = self.d.fingerprint(self.episode(300, op_at=30))
        b = self.d.fingerprint(self.episode(300, op_at=75))
        length, start_a, start_b, _agree = self.d.shared_segment(a, b)
        self.assertAlmostEqual(90, length, delta=2)
        self.assertAlmostEqual(30, start_a, delta=1)
        self.assertAlmostEqual(75, start_b, delta=1)

    def test_opening_under_dialogue_still_found(self):
        a = self.d.fingerprint(self.episode(300, op_at=10, dialogue=0.5))
        b = self.d.fingerprint(self.episode(300, op_at=120, dialogue=0.5))
        found = self.d.shared_segment(a, b)
        self.assertIsNotNone(found)
        self.assertAlmostEqual(10, found[1], delta=1.5)
        self.assertAlmostEqual(120, found[2], delta=1.5)

    def test_nothing_shared_or_too_short_finds_nothing(self):
        a = self.d.fingerprint(self.episode(300))
        b = self.d.fingerprint(self.episode(300))
        self.assertIsNone(self.d.shared_segment(a, b))
        # 30 s in common is a recap jingle or a reused cue, not an opening.
        a = self.d.fingerprint(self.episode(300, op_at=20, song_len=30))
        b = self.d.fingerprint(self.episode(300, op_at=90, song_len=30))
        self.assertIsNone(self.d.shared_segment(a, b))

    def test_a_segment_needs_two_neighbours_to_agree(self):
        self.assertIsNone(self.d._confirmed([(30.0, 120.0)]))
        self.assertIsNone(self.d._confirmed([(30.0, 120.0), (60.0, 150.0)]))
        self.assertEqual([30.5, 120.5], self.d._confirmed([(30.0, 120.0), (31.0, 121.0)]))

    def make_folder(self, ops, seconds=400, ed=True):
        """Episodes "Show Ep<n>.mp4" (n from 1) with their audio kept in memory."""
        audio = {}
        for n, op_at in enumerate(ops, 1):
            path = os.path.join(self.tmp, f"Show Ep{n}.mp4")
            with open(path, "wb") as f:
                f.write(b"\0" * (1000 + n))          # distinct sizes
            audio[path] = self.episode(seconds, op_at=op_at,
                                       ed_at=(seconds - 100) if ed else None)
        calls = []

        def decoder(path, start, length):
            calls.append(path)
            a = audio[path]
            s = int((len(a) / self.RATE + start if start < 0 else start) * self.RATE)
            return a[max(0, s):max(0, s) + int(length * self.RATE)]
        return audio, decoder, calls

    def test_folder_detection_saves_times_and_reuses_them(self):
        audio, decoder, calls = self.make_folder([5, 40, 0, 70])
        found = self.d.detect_folder(self.tmp, decoder=decoder, duration=lambda p: 400.0)
        self.assertEqual(4, found)
        data = self.st.read_skip_file(self.tmp)
        eps = data["episodes"]
        for n, op_at in enumerate([5, 40, 0, 70], 1):
            entry = eps[f"Show Ep{n}.mp4"]
            self.assertAlmostEqual(op_at, entry["op"][0], delta=1.5)
            self.assertAlmostEqual(op_at + 90, entry["op"][1], delta=2.5)
            self.assertAlmostEqual(300, entry["ed"][0], delta=1.5)
            self.assertEqual("audio", entry["source"])
        self.assertEqual((4, 4), self.st.skip_counts(self.tmp))
        # Second visit: every file unchanged, nothing decoded again.
        calls.clear()
        self.assertEqual(4, self.d.detect_folder(self.tmp, decoder=decoder,
                                                 duration=lambda p: 400.0))
        self.assertEqual([], calls)
        # A replaced episode is done again (with its neighbours, to compare).
        ep2 = os.path.join(self.tmp, "Show Ep2.mp4")
        with open(ep2, "wb") as f:
            f.write(b"\0" * 5000)
        self.assertEqual((3, 4), self.st.skip_counts(self.tmp))
        calls.clear()
        self.d.detect_folder(self.tmp, decoder=decoder, duration=lambda p: 400.0)
        self.assertIn(ep2, calls)
        self.assertEqual((4, 4), self.st.skip_counts(self.tmp))

    def test_fewer_than_three_episodes_or_no_mpvnet_saves_nothing(self):
        from unittest import mock
        _audio, decoder, _calls = self.make_folder([5, 40])
        self.assertEqual(-1, self.d.detect_folder(self.tmp, decoder=decoder))
        self.make_folder([5, 40, 0])
        with mock.patch.object(self.d, "find_libmpv", return_value=""):
            self.assertEqual(-1, self.d.detect_folder(self.tmp))
        # Not saved as "nothing found": it's tried again once mpv.net is there.
        self.assertFalse(os.path.exists(os.path.join(self.tmp, self.st.SKIP_FILE)))

    def test_an_episode_that_fails_to_decode_is_tried_again_later(self):
        _audio, decoder, _calls = self.make_folder([5, 40, 0, 70])
        bad = os.path.join(self.tmp, "Show Ep4.mp4")

        def flaky(path, start, length):
            if path == bad:
                raise self.d.DecodeError("broken file")
            return decoder(path, start, length)
        self.d.detect_folder(self.tmp, decoder=flaky, duration=lambda p: 400.0)
        eps = self.st.read_skip_file(self.tmp)["episodes"]
        self.assertNotIn("Show Ep4.mp4", eps)
        self.assertEqual(3, len(eps))

    def test_player_settings_round_trip(self):
        self.assertEqual({"opening": "ask", "ending": "off"}, self.st.read_settings(self.tmp))
        self.assertTrue(self.st.write_settings(self.tmp, "auto", "ask"))
        self.assertEqual({"opening": "auto", "ending": "ask"}, self.st.read_settings(self.tmp))
        self.assertFalse(self.st.write_settings(self.tmp, "sometimes", "ask"))
        with open(os.path.join(self.tmp, self.st.SETTINGS_FILE), "a", encoding="utf-8") as f:
            f.write("ending=loud\n")                  # a bad hand edit is ignored
        self.assertEqual("ask", self.st.read_settings(self.tmp)["ending"])

    def test_queue_runs_each_folder_once(self):
        import threading
        from unittest import mock
        done, seen = threading.Event(), []
        other = os.path.join(self.tmp, "Another Show")     # removed with self.tmp
        os.makedirs(other)
        gate = threading.Event()

        def fake(folder, force=False, should_stop=None):
            gate.wait(5)
            seen.append((folder, force))
            if len(seen) == 2:
                done.set()
        with mock.patch.object(self.d, "detect_folder", side_effect=fake):
            self.assertTrue(self.st.queue_folder(self.tmp))
            self.assertTrue(self.st.queue_folder(other))
            self.assertTrue(self.st.queue_folder(other, force=True))   # merged, now forced
            self.assertFalse(self.st.queue_folder(os.path.join(self.tmp, "missing")))
            gate.set()
            self.assertTrue(done.wait(10))
        self.assertEqual([(self.tmp, False), (other, True)], seen)

    def test_player_script_is_bundled_with_its_settings_and_key(self):
        import re
        from utils import mpvnet
        base = mpvnet.bundle_dir()
        with open(os.path.join(base, "scripts", mpvnet.SKIP_SCRIPT), encoding="utf-8") as f:
            lua = f.read()
        # read_options' name is the settings file the Video Player tab writes.
        name = re.search(r'read_options\(o, "([^"]+)"\)', lua).group(1)
        self.assertEqual(os.path.join("script-opts", name + ".conf"), self.st.SETTINGS_FILE)
        for key in ("opening", "ending"):
            self.assertRegex(lua, rf'{key} = "{self.st.DEFAULTS[key]}"')
        self.assertIn(self.st.SKIP_FILE, lua)
        # K belongs to the script (forced, so it beats input.conf) while "Skip" is
        # on screen, and to "ignore" otherwise -- unbound, mpv.net's own K opened
        # input.conf when pressed after the prompt.
        key = re.search(r'local KEY = "([^"]+)"', lua).group(1)
        self.assertIn("add_forced_key_binding(KEY", lua)
        with open(os.path.join(base, "input.conf"), encoding="utf-8") as f:
            self.assertRegex(f.read(), rf"(?m)^{re.escape(key)}\s+ignore\s*$")
        # The text shows for 10 s; K stays bound for the whole song (only clear()
        # unbinds it, and the timed hide only removes the overlay).
        self.assertIn("prompt_seconds = 10", lua)
        timed = re.search(r"elseif text_until and t >= text_until then(.*?)\n\s*end", lua, re.S)
        self.assertIsNotNone(timed)
        self.assertIn("overlay:remove()", timed.group(1))
        self.assertNotIn("remove_key_binding", timed.group(1))
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "tools", "build_release.py"), encoding="utf-8") as f:
            self.assertIn('"aed-skip.lua"', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
