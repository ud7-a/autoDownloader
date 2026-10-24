"""Releases and updates: what the build ships, the installed app's imports, release notes and the in-app updater.

Run with: py -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest

# The app's data, shortcut and mpv.net state folders point at a temp folder
# (tests/__init__.py) BEFORE any app module is imported. `discover -s tests`
# imports this file on its own, so the package is imported here explicitly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")


class ShippedImportTests(unittest.TestCase):
    """The installed app only has what requirements.txt installs. Importing anything
    else -- the cloud service's code pulled in httpx and the Postgres driver -- works
    from source on a developer machine and kills the shipped app."""

    CLIENT = ["main.py", "aed_watcher.pyw", "ui", "core", "utils"]
    # requirements.txt distribution name -> the module name it is imported as
    DIST_TO_MODULE = {"pyqt6": "PyQt6", "pyqt6-fluent-widgets": "qfluentwidgets",
                      "selenium": "selenium", "websocket-client": "websocket",
                      "py7zr": "py7zr", "rarfile": "rarfile", "pillow": "PIL",
                      "scipy": "scipy", "psutil": "psutil", "numpy": "numpy"}

    def client_files(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for entry in self.CLIENT:
            path = os.path.join(root, entry)
            if os.path.isfile(path):
                yield path
            else:
                for dirpath, _dirs, files in os.walk(path):
                    for name in files:
                        if name.endswith(".py"):
                            yield os.path.join(dirpath, name)

    def imports(self, path):
        import ast
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):                 # inside functions too
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield node.lineno, alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                yield node.lineno, node.module.split(".")[0]

    def test_desktop_app_never_imports_the_cloud_service(self):
        offenders = [f"{os.path.basename(p)}:{line}" for p in self.client_files()
                     for line, mod in self.imports(p) if mod == "service"]
        self.assertEqual(offenders, [])

    def test_every_import_is_stdlib_local_or_a_declared_requirement(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        declared = set()
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as f:
            for line in f:
                name = re.split(r"[=<>\[ #;]", line.strip(), maxsplit=1)[0].lower()
                if name in self.DIST_TO_MODULE:
                    declared.add(self.DIST_TO_MODULE[name])
        local = {"ui", "core", "utils", "main", "tools"}
        # Bundled only into the sandbox update test's build, imported under
        # `except ImportError` (core/updater.py): never in a release, on purpose.
        test_build_only = {"aed_update_test"}
        missing = []
        for p in self.client_files():
            for line, mod in self.imports(p):
                if (mod in sys.stdlib_module_names or mod in local or mod in declared
                        or mod in test_build_only):
                    continue
                missing.append(f"{os.path.relpath(p, root)}:{line} imports {mod!r}")
        self.assertEqual(missing, [], "not installed in the release build")


class UpdaterTests(unittest.TestCase):
    """core/updater.py: which release is offered, and the download that must
    never hang, run a damaged file, or install over the real app from source."""

    RELEASE = {"tag_name": "v5.0.1", "assets": [
        {"name": "AutoDownloader_Portable.zip", "browser_download_url": "https://x/p.zip",
         "size": 9, "digest": "sha256:" + "0" * 64},
        {"name": "AutoDownloader_Setup.exe", "browser_download_url": "https://x/s.exe",
         "size": 11, "digest": "sha256:" + "AB" * 32},
    ]}

    def setUp(self):
        from core import updater
        self.up = updater
        self.tmp = tempfile.mkdtemp(prefix="aed_up_")
        import shutil
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_only_a_strictly_newer_release_counts(self):
        newer = self.up.is_newer
        self.assertTrue(newer("v5.0.1", "5.0.0"))
        self.assertTrue(newer("v5.1", "5.0.9"))
        self.assertTrue(newer("release-v5.0.1", "5.0.0"))     # was read as 0.0.0
        self.assertFalse(newer("v4.9.0", "4.9"))               # same version, was offered
        self.assertFalse(newer("v5.0", "5.0.0"))
        self.assertFalse(newer("v4.9.1", "5.0.0"))             # never a downgrade
        self.assertFalse(newer("", "5.0.0"))
        self.assertTrue(newer("v5.0.1", "5"))

    def test_the_setup_asset_with_its_size_and_checksum(self):
        info = self.up.update_info(self.RELEASE, current="5.0.0")
        self.assertEqual({"version": "5.0.1", "url": "https://x/s.exe", "size": 11,
                          "sha256": "ab" * 32}, info)
        self.assertIsNone(self.up.update_info(self.RELEASE, current="5.0.1"))
        no_setup = {"tag_name": "v5.0.1", "assets": self.RELEASE["assets"][:1]}
        self.assertIsNone(self.up.update_info(no_setup, current="5.0.0"))
        no_digest = {"tag_name": "v5.0.1", "assets": [{"name": "AutoDownloader_Setup.exe",
                                                       "browser_download_url": "https://x/s.exe"}]}
        self.assertEqual("", self.up.update_info(no_digest, current="5.0.0")["sha256"])

    def _signals(self):
        from core.signals import signals
        got = []
        signals.update_available.connect(lambda info: got.append(("available", info)))
        signals.update_check_result.connect(lambda level, msg: got.append((level, msg)))
        self.addCleanup(signals.update_available.disconnect)
        self.addCleanup(signals.update_check_result.disconnect)
        return got

    def test_a_copy_run_from_source_never_offers_to_install(self):
        from unittest import mock
        got = self._signals()
        with mock.patch.object(self.up, "fetch_latest") as fetch:
            self.assertFalse(self.up.updates_enabled())        # tests: not frozen, isolated
            self.assertIsNone(self.up.check_for_updates(manual=False))
            self.assertIsNone(self.up.check_for_updates(manual=True))
            fetch.assert_not_called()
        self.assertEqual(["info"], [g[0] for g in got])          # only the manual one says so

    def test_check_offers_reports_up_to_date_or_the_error(self):
        from unittest import mock
        got = self._signals()
        with mock.patch.object(self.up, "updates_enabled", return_value=True):
            with mock.patch.object(self.up, "APP_VERSION", "5.0.1"), \
                    mock.patch.object(self.up, "fetch_latest", return_value=self.RELEASE):
                self.assertIsNone(self.up.check_for_updates(manual=True))
            self.assertEqual(("success", "You're up to date (version 5.0.1)."), got[-1])
            with mock.patch.object(self.up, "APP_VERSION", "5.0.0"), \
                    mock.patch.object(self.up, "fetch_latest", return_value=self.RELEASE):
                self.assertEqual("5.0.1", self.up.check_for_updates(manual=False)["version"])
            self.assertEqual("available", got[-1][0])
            with mock.patch.object(self.up, "fetch_latest", side_effect=OSError("offline")):
                self.assertIsNone(self.up.check_for_updates(manual=True))
            self.assertEqual("error", got[-1][0])
            self.assertIn("offline", got[-1][1])

    def test_check_emits_what_to_install(self):
        from unittest import mock
        got = self._signals()
        info = {"version": "5.0.1", "url": "https://x/s.exe", "size": 11, "sha256": ""}
        with mock.patch.object(self.up, "updates_enabled", return_value=True), \
                mock.patch.object(self.up, "fetch_latest", return_value=self.RELEASE), \
                mock.patch.object(self.up, "update_info", return_value=dict(info)):
            self.up.check_for_updates(manual=True)
        self.assertEqual([("available", {**info, "manual": True})], got)
        got.clear()
        with mock.patch.object(self.up, "updates_enabled", return_value=True), \
                mock.patch.object(self.up, "fetch_latest", return_value={"tag_name": "v1.0.0"}):
            self.up.check_for_updates(manual=True)
        self.assertEqual("success", got[0][0])                  # "You're up to date"

    def _opener(self, body, length=None, calls=None):
        class Response:
            def __init__(self):
                self.headers = {"Content-Length": str(len(body) if length is None else length)}
                self._data = body

            def read(self, n):
                chunk, self._data = self._data[:n], self._data[n:]
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout=None):
            if calls is not None:
                calls.append(timeout)
            return Response()
        return opener

    def test_download_is_checked_against_the_release(self):
        import hashlib
        from unittest import mock
        body = os.urandom(600_000)                               # a few chunks
        digest = hashlib.sha256(body).hexdigest()
        path = os.path.join(self.tmp, "setup.exe")
        seen, calls = [], []
        info = {"url": "https://x/s.exe", "size": len(body), "sha256": digest}
        with mock.patch.object(self.up, "log"):
            self.assertEqual(digest, self.up.download(info, path, progress=seen.append,
                                                      opener=self._opener(body, calls=calls)))
            self.assertEqual(self.up.READ_TIMEOUT, calls[0])     # a stall can't hang it
            self.assertEqual(100, seen[-1])
            with open(path, "rb") as f:
                self.assertEqual(body, f.read())

            for bad, why in (({**info, "size": len(body) + 1}, "incomplete"),
                             ({**info, "sha256": "0" * 64}, "SHA-256")):
                with self.assertRaises(IOError) as cm:
                    self.up.download(bad, path, opener=self._opener(body))
                self.assertIn(why, str(cm.exception))
                self.assertFalse(os.path.exists(path))           # nothing half-written left

            with self.assertRaises(self.up.DownloadCancelled):
                self.up.download(info, path, cancelled=lambda: True, opener=self._opener(body))
            self.assertFalse(os.path.exists(path))

    def test_the_setup_runs_only_if_it_is_still_the_downloaded_file(self):
        import hashlib
        from unittest import mock
        path = os.path.join(self.tmp, "setup.exe")
        with open(path, "wb") as f:
            f.write(b"setup")
        digest = hashlib.sha256(b"setup").hexdigest()
        with mock.patch.object(self.up.subprocess, "Popen") as popen, \
                mock.patch.object(self.up, "log"):
            self.assertEqual((True, ""), self.up.launch_setup(path, digest))
            # Run from its own folder: the inherited app folder couldn't be replaced.
            popen.assert_called_once_with([path, "--silent"], close_fds=True, cwd=self.tmp)
            popen.reset_mock()
            ok, why = self.up.launch_setup(path, "0" * 64)       # swapped after the download
            self.assertFalse(ok)
            self.assertIn("changed", why)
            popen.assert_not_called()
            popen.side_effect = OSError("blocked by policy")
            self.assertEqual((False, "blocked by policy"), self.up.launch_setup(path, digest))

    def test_the_download_thread_starts_the_setup_or_reports_why_not(self):
        """Download, check and launch all happen off the UI thread (the launch
        waits on the antivirus scanning the exe: ~9 s of frozen window)."""
        from unittest import mock
        info = {"url": "https://x/s.exe", "size": 1, "sha256": ""}
        for launched, expect in (((True, ""), "started"), ((False, "blocked"), "error")):
            thread = self.up.UpdateDownloaderThread(info)
            got = []
            thread.setup_started.connect(lambda p: got.append(("started", p)))
            thread.error.connect(lambda m: got.append(("error", m)))
            with mock.patch.object(self.up, "download", return_value="ab"), \
                    mock.patch.object(self.up, "launch_setup", return_value=launched) as launch, \
                    mock.patch.object(self.up.tempfile, "mkdtemp", return_value=self.tmp), \
                    mock.patch.object(self.up, "log"):
                thread.run()
            self.assertEqual(expect, got[0][0])
            launch.assert_called_once_with(os.path.join(self.tmp, "AutoDownloader_Setup.exe"), "ab")
            if expect == "error":
                self.assertEqual("Couldn't start the installer: blocked", got[0][1])
        thread = self.up.UpdateDownloaderThread(info)
        got = []
        thread.error.connect(got.append)
        with mock.patch.object(self.up, "download", side_effect=IOError("timed out")), \
                mock.patch.object(self.up, "launch_setup") as launch, \
                mock.patch.object(self.up, "log"):
            thread.run()
        self.assertEqual(["Couldn't download the update: timed out"], got)
        launch.assert_not_called()

    def test_old_update_downloads_are_cleaned_up(self):
        from unittest import mock
        for name in ("aed_update_abc", "aed_update_def", "unrelated"):
            os.makedirs(os.path.join(self.tmp, name, "sub"))
        with mock.patch.object(self.up, "log"):
            self.assertEqual(2, self.up.remove_old_downloads(self.tmp))
        self.assertEqual(["unrelated"], os.listdir(self.tmp))

    def test_shipped_builds_only_ever_update_from_github(self):
        """Only the sandbox test build (an extra aed_update_test module) can name
        another release URL; nothing a shipped build reads at runtime can."""
        import importlib
        import types
        from unittest import mock
        github = "https://api.github.com/repos/ud7-a/autoDownloader/releases/latest"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            with mock.patch.dict(os.environ, {"AED_UPDATE_API_URL": "http://127.0.0.1:1/x"}):
                self.assertEqual(github, importlib.reload(self.up).API_URL)
            hook = types.ModuleType("aed_update_test")
            hook.API_URL = "http://127.0.0.1:8765/release.json"
            with mock.patch.dict(sys.modules, {"aed_update_test": hook}):
                self.assertEqual(hook.API_URL, importlib.reload(self.up).API_URL)
        finally:
            importlib.reload(self.up)
        self.assertEqual(github, self.up.API_URL)
        self.assertFalse(os.path.exists(os.path.join(repo, "aed_update_test.py")))
        with open(os.path.join(repo, "tools", "build_release.py"), encoding="utf-8") as f:
            self.assertNotIn("aed_update_test", f.read())

    def test_installer_paths_are_escaped_for_powershell(self):
        import installer
        filled = installer.fill_template("$a = '{launcher_exe}'\n$b = '{dest_dir}'",
                                         {"launcher_exe": r"C:\Users\O'Brien\setup.exe",
                                          "dest_dir": r"C:\Auto Episodes Downloader\App"})
        self.assertEqual("$a = 'C:\\Users\\O''Brien\\setup.exe'\n"
                         "$b = 'C:\\Auto Episodes Downloader\\App'", filled)
        with open(installer.__file__, encoding="utf-8") as f:
            self.assertIn('encoding="utf-8-sig"', f.read())       # PowerShell 5.1 reads it as UTF-8


class UpdatePromptTests(unittest.TestCase):
    """AppWindow's side of an update: a failure or cancel never leaves the
    window stuck, and a later check can offer it again."""

    def _win(self, running=False):
        from types import SimpleNamespace
        from unittest import mock
        from ui.app_window import AppWindow
        win = SimpleNamespace(_update_busy=False, _download_running=running, progress_dialog=None,
                              setEnabled=mock.Mock(), close=mock.Mock(),
                              _download_update=mock.Mock())
        for name in ("_update_failed", "_close_update_dialog"):
            setattr(win, name, getattr(AppWindow, name).__get__(win))
        return win

    def test_no_or_failure_lets_a_later_check_ask_again(self):
        from unittest import mock
        from ui.app_window import AppWindow
        info = {"version": "5.0.1"}
        win = self._win(running=True)
        with mock.patch("qfluentwidgets.MessageBox") as box:
            box.return_value.exec.return_value = False
            AppWindow.prompt_update(win, info)
            self.assertIn("A download is running", box.call_args[0][1])
            self.assertFalse(win._update_busy)                   # declined: can be offered again
            box.return_value.exec.return_value = True
            AppWindow.prompt_update(win, info)
            self.assertTrue(win._update_busy)
            win._download_update.assert_called_once_with(info)
            AppWindow.prompt_update(win, info)                   # busy: not asked twice
            self.assertEqual(2, box.call_count)
        with mock.patch("qfluentwidgets.InfoBar") as bar:
            win._update_failed("Couldn't download the update: timed out")
        self.assertFalse(win._update_busy)
        win.setEnabled.assert_called_with(True)
        bar.error.assert_called_once()

    def test_a_closed_progress_dialog_never_comes_back(self):
        """Found by the sandbox update test: a download that failed within a
        second closed the dialog, then QProgressDialog's own timer showed it again
        4 s later -- modal, full bar, blocking the window."""
        from unittest import mock
        from PyQt6 import sip
        from PyQt6.QtWidgets import QApplication, QWidget
        from ui.app_window import AppWindow
        type(self)._app = QApplication.instance() or QApplication([])   # alive for the run

        class Host(QWidget):
            progress_dialog = None
            _download_update = AppWindow._download_update
            _close_update_dialog = AppWindow._close_update_dialog

        host = Host()
        # Destroyed here, on the GUI thread. Left to the garbage collector (the
        # lambdas wired to the mock thread make a cycle) it could be freed during
        # a later test on a worker thread, which aborts Qt (0xC0000409).
        self.addCleanup(lambda: sip.isdeleted(host) or sip.delete(host))
        thread = mock.Mock()
        with mock.patch("core.updater.UpdateDownloaderThread", return_value=thread):
            host._download_update({"url": "x"})
        dialog = host.progress_dialog
        self.assertTrue(dialog.isVisible())
        thread.start.assert_called_once()
        from PyQt6.QtCore import QTimer
        dialog.setValue(0)                   # the download's first report: re-arms Qt's
        dialog.setValue(37)                  # 4 s show timer unless it counts as shown
        host._close_update_dialog()
        self.assertFalse(dialog.isVisible())
        # Qt's show timer is the dialog's QTimer child. Still running, it shows the
        # closed dialog again. Checked directly rather than by waiting in the event
        # loop, which would also run whatever earlier tests left queued.
        self.assertEqual([], [t for t in dialog.findChildren(QTimer) if t.isActive()])

    def test_the_app_closes_normally_once_the_setup_runs(self):
        from unittest import mock
        from ui.app_window import AppWindow
        win = self._win()
        with mock.patch("ui.app_window.QApplication") as qapp:
            AppWindow._close_for_update(win)
        win.close.assert_called_once()                           # a normal close, not os._exit
        qapp.instance.return_value.quit.assert_called_once()


class ReleaseNotesTests(unittest.TestCase):
    """CHANGELOG.md's section for a version is the GitHub release's text
    (tools/release_notes.py, used by publish.py and the Release workflow)."""

    LOG = ("# Changelog\n\nIntro.\n\n## Unreleased\n\n### New\n- Library tab\n\n"
           "## 4.9.1 — 2026-09-30\n\n- Older fix\n")

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools"))
        import release_notes
        self.rn = release_notes

    def test_stamp_turns_unreleased_into_the_version(self):
        from datetime import date
        out = self.rn.stamp(self.LOG, "4.10.0", today=date(2026, 10, 11))
        self.assertIn("## Unreleased\n\n## 4.10.0 — 2026-10-11\n\n### New\n- Library tab", out)
        self.assertEqual(out, self.rn.stamp(out, "4.10.0"))          # once only
        self.assertEqual("### New\n- Library tab\n", self.rn.notes_for(out, "4.10.0"))
        self.assertEqual("", self.rn.notes_for(out, "4.11.0"))       # new Unreleased is empty

    def test_notes_for_a_version_or_unreleased(self):
        self.assertEqual("- Older fix\n", self.rn.notes_for(self.LOG, "4.9.1"))
        self.assertEqual("- Older fix\n", self.rn.notes_for(self.LOG, "v4.9.1".lstrip("v")))
        self.assertEqual("### New\n- Library tab\n", self.rn.notes_for(self.LOG, "4.10.0"))
        self.assertNotIn("4.9.10", self.rn.notes_for(self.LOG, "4.9.10"))   # no prefix match
        empty = "# Changelog\n\n## Unreleased\n\n## 4.9.1\n\n- x\n"
        self.assertEqual(empty, self.rn.stamp(empty, "4.10.0"))       # nothing to stamp

    def test_the_shipped_changelog_parses(self):
        with open(self.rn.CHANGELOG, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(any(t.lower() == "unreleased" for t, _b in self.rn._sections(text)))


class ReleaseBundleTests(unittest.TestCase):
    """What an update ships: the release build bundles only the files the app reads
    at run time. Whole "assets" and "tools" folders shipped the build scripts in
    tools/ to every user, plus any stray local file."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _build_release(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_release_for_test", os.path.join(self.ROOT, "tools", "build_release.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _bundled(self, br):
        """Every repo file the bundle data expands to, as repo paths."""
        out = []
        for src, _dest in br.BUNDLE_DATA:
            path = os.path.join(self.ROOT, src)
            self.assertTrue(os.path.exists(path), f"{src} is listed but missing")
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    out += [os.path.relpath(os.path.join(root, f), self.ROOT).replace("\\", "/")
                            for f in files]
            else:
                out.append(src)
        return out

    def test_only_runtime_files_are_bundled(self):
        import subprocess
        br = self._build_release()
        adds = [br.APP_FLAGS[i + 1] for i, f in enumerate(br.APP_FLAGS) if f == "--add-data"]
        self.assertEqual(len(br.BUNDLE_DATA), len(adds))
        self.assertNotIn(f"assets{os.pathsep}assets", adds)
        self.assertNotIn(f"tools{os.pathsep}tools", adds)
        self.assertEqual("main.py", br.APP_FLAGS[-1])
        bundled = self._bundled(br)
        self.assertFalse([p for p in bundled if p.endswith((".py", ".pyc", ".zip", ".log"))])
        # Every bundled file is in the repository, so a CI build ships the same.
        tracked = set(subprocess.run(["git", "ls-files"], cwd=self.ROOT, capture_output=True,
                                     text=True, encoding="utf-8").stdout.splitlines())
        if tracked:
            self.assertEqual([], [p for p in bundled if p not in tracked])

    def test_every_file_the_app_opens_is_bundled(self):
        br = self._build_release()
        bundled = set(self._bundled(br))
        for rel in ("assets/app_icon.ico", "assets/finishingDownloadSound.wav",
                    "assets/more_dark.svg", "assets/more_light.svg",
                    "assets/site_icons/witanime.site.png",
                    "assets/mpvnet/mpv.conf", "assets/mpvnet/input.conf",
                    "assets/mpvnet/mpvnet.conf", "assets/mpvnet/THIRD_PARTY.txt",
                    "assets/mpvnet/scripts/aed-progress.lua", "assets/mpvnet/scripts/aed-skip.lua",
                    "assets/mpvnet/scripts/sync-guard.lua",
                    "assets/mpvnet/scripts/shader-toggle.lua",
                    "tools/aria2c.exe", "tools/unrar.exe", "tools/ublock_lite.crx"):
            self.assertIn(rel, bundled)
        for name in os.listdir(os.path.join(self.ROOT, "assets", "mpvnet", "Shaders")):
            self.assertIn(f"assets/mpvnet/Shaders/{name}", bundled)
        for required in br.REQUIRED_IN_BUNDLE:
            rel = os.path.relpath(required, "_internal").replace("\\", "/")
            self.assertIn(rel, bundled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
