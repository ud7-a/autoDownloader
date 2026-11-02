"""tools/dev_run.py: the sandbox the app runs in from source. Each test runs it in
its own process -- the sandbox points the app's data folders somewhere else and
replaces functions, which this test process must never see.

Run with: py -m unittest discover -s tests
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
__import__("tests")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_python(code, timeout=180):
    """Run `code` in a fresh interpreter (repo root on the path, Qt offscreen) and
    return its last stdout line parsed as JSON."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    prelude = f"import sys, json; sys.path.insert(0, {ROOT!r}); sys.path.insert(0, {os.path.join(ROOT, 'tools')!r})\n"
    res = subprocess.run([sys.executable, "-B", "-c", prelude + textwrap.dedent(code)],
                         cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    if res.returncode != 0:
        raise AssertionError(f"exit {res.returncode}\n{res.stdout[-2000:]}\n{res.stderr[-4000:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


class DevRunTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aed_sandbox_")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_sample_data_is_written_through_the_app(self):
        got = run_python(f"""
            import os, dev_run
            root = {self.root!r}
            dev_run.point_at_sandbox(root)
            dev_run.seed(root)
            from utils import config, watch_later as wl
            config.load_config()
            show = wl.find("https://witanime.site/anime/sample-show/")
            part = show["parts"][0]
            print(json.dumps({{
                "isolated": config.IS_ISOLATED, "app_dir": config.APP_DIR,
                "download_dir": config.app_settings.get("download_dir"),
                "profiles": sorted(config.sites_data),
                "statuses": sorted((e["title"], e["status"]) for e in wl.entries()),
                "watched": part["watched"], "finished": part["finished"],
                "progress": part["progress"],
                "files": sorted(os.listdir(os.path.join(root, "animes", "Sample Show"))),
                "marker": os.path.isfile(os.path.join(root, dev_run.MARKER)),
            }}))
        """)
        self.assertTrue(got["isolated"])
        self.assertEqual(os.path.join(self.root, "app"), got["app_dir"])
        self.assertEqual(os.path.join(self.root, "animes"), got["download_dir"])
        for title in ("Sample Show", "Demo Adventure", "Test Movie Saga"):
            self.assertIn(title, got["profiles"])
        self.assertEqual([("Demo Adventure", "later"), ("Sample Show", "watching"),
                          ("Test Movie Saga", "completed")], [tuple(s) for s in got["statuses"]])
        self.assertEqual([1, 2, 3, 4, 5], got["watched"])
        self.assertEqual([1, 2, 3, 4, 5], got["finished"])
        self.assertEqual({"6": 40.0}, got["progress"])
        self.assertEqual([f"Sample Show Ep{n}.mp4" for n in sorted(range(1, 9), key=str)],
                         got["files"])
        self.assertTrue(got["marker"])

    def test_pretending_an_update_shows_whats_new(self):
        """--whats-new FROM: the app takes the next version (Unreleased notes exist)
        and starts as the first launch after updating from FROM."""
        got = run_python(f"""
            import dev_run
            root = {self.root!r}
            dev_run.point_at_sandbox(root)
            dev_run.seed(root)
            from utils import config
            before = config.APP_VERSION
            version = dev_run.pretend_update(root, "5.0.0")
            config.load_config()
            from ui import whats_new
            print(json.dumps({{"before": before, "version": version, "now": config.APP_VERSION,
                              "seen": config.app_settings["whats_new_seen"],
                              "shown": [v for v, _d, _f in whats_new.pending()]}}))
        """)
        self.assertEqual("5.0.0", got["seen"])
        self.assertEqual(got["version"], got["now"])
        from utils import changelog
        has_unreleased = changelog.features(dict(
            (t.lower(), b) for t, b in changelog.sections(changelog.read_bundled())).get("unreleased", ""))
        if has_unreleased:
            import tools.dev_run as dr
            self.assertEqual(dr.next_version(got["before"]), got["version"])
        self.assertEqual(got["version"], got["shown"][0])
        self.assertGreater(len(got["shown"]), 1)                     # every version since 5.0.0

    def test_next_version(self):
        import tools.dev_run as dr
        self.assertEqual("5.2.0", dr.next_version("5.1.0"))
        self.assertEqual("6.1.0", dr.next_version("6.0.9"))
        self.assertEqual("5.1.0", dr.next_version("5"))

    def test_outside_effects_are_stubbed_and_downloads_are_pretend(self):
        got = run_python(f"""
            import os, dev_run
            root = {self.root!r}
            dev_run.point_at_sandbox(root)
            dev_run.seed(root)
            dev_run.STEP_SECONDS = 0
            dev_run.stub_outside_effects(root, real_downloads=False)
            import core.selenium_engine as eng, utils.config as config
            import utils.instance as instance, utils.mpvnet as mpvnet
            from utils import watch_later as wl
            from utils.database import init_db
            init_db()
            animes = os.path.join(root, "animes")
            eng.run_selenium_task("Sample Show", [9, 10], animes, True, "", "", 5, 3)
            show = wl.find("https://witanime.site/anime/sample-show/")
            print(json.dumps({{
                "files": sorted(f for f in os.listdir(os.path.join(animes, "Sample Show"))
                                if f.endswith(("Ep9.mp4", "Ep10.mp4"))),
                "history": show["history"][:1],
                "sources": sorted(wl.part_sources(wl.find("https://witanime.site/anime/sample-show/")["parts"][0], animes)),
                "mpv_config": mpvnet.config_dir("C:/x/mpvnet.exe"),
                "mutex": instance.MUTEX_NAME,
                "autostart": config.set_windows_autostart(True),
                "watcher": config.start_background_watcher(),
                "default_player": mpvnet.enable_default_policy("C:/x/mpvnet.exe"),
                "winget": mpvnet.run_winget_install(print),
                "sweep": eng.kill_stuck_chrome_processes(),
            }}))
        """)
        self.assertEqual(["Sample Show Ep10.mp4", "Sample Show Ep9.mp4"], got["files"])
        self.assertIn("Sample Show", json.dumps(got["history"]))
        self.assertIn("Success", json.dumps(got["history"]))
        self.assertIn(9, got["sources"])
        self.assertEqual(os.path.join(self.root, "mpv.net"), got["mpv_config"])
        self.assertNotEqual("Local\\AED_Main_App_Running_Mutex", got["mutex"])
        self.assertFalse(got["autostart"])
        self.assertFalse(got["watcher"])
        self.assertEqual(1223, got["default_player"])
        self.assertEqual(1223, got["winget"])
        self.assertIsNone(got["sweep"])

    def test_fresh_never_deletes_a_folder_that_isnt_a_sandbox(self):
        precious = os.path.join(self.root, "precious.txt")
        with open(precious, "w") as f:
            f.write("keep me")
        res = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "tools", "dev_run.py"),
                              "--dir", self.root, "--fresh"], cwd=ROOT, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=60,
                             env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        self.assertNotEqual(0, res.returncode)
        self.assertIn("isn't a sandbox", res.stderr + res.stdout)
        self.assertTrue(os.path.isfile(precious))


if __name__ == "__main__":
    unittest.main(verbosity=2)
