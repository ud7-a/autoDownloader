"""Test isolation: the suite never reads or writes the user's real data.

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


class IsolationGuardTests(unittest.TestCase):
    """Tests must never read or write the real user data directory. If this fails,
    stop and fix the isolation before running anything else -- a test that writes to
    the live config can wipe the user's saved profiles."""

    def test_app_dir_is_redirected(self):
        from utils import config
        self.assertTrue(config.IS_ISOLATED,
                        f"tests are pointed at the REAL data dir: {config.APP_DIR}")

    def test_paths_live_under_the_temp_dir(self):
        from utils import config
        self.assertNotEqual(config.APP_DIR, config.DEFAULT_APP_DIR)
        for path in (config.CONFIG_FILE, config.DB_FILE, config.PROFILE_DIR):
            self.assertTrue(path.startswith(config.APP_DIR), path)
            self.assertNotIn(config.DEFAULT_APP_DIR, path)

    def test_keyboard_shortcuts_never_touch_the_real_start_menu(self):
        from utils import shortcut
        start_menu = os.path.join("Microsoft", "Windows", "Start Menu").lower()
        for action in shortcut.ACTIONS:
            self.assertNotIn(start_menu, shortcut.shortcut_path(action).lower(), action)
        for folder in shortcut._shortcut_folders():
            self.assertNotIn(start_menu, folder.lower())
            self.assertNotIn("desktop", folder.lower())

    def test_every_temp_folder_a_test_makes_is_removed(self):
        """Each tempfile.mkdtemp() in a test file is followed by a cleanup that
        deletes it. Nine setups had none: ~6,000 folders (139 MB of them mpv.net
        config copies) had piled up in %TEMP%."""
        import glob
        import re
        leaks = []
        for path in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "test_*.py"))):
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for i, line in enumerate(lines):
                m = re.match(r"\s*([\w.]+) = tempfile\.mkdtemp\(", line)
                if not m:
                    continue
                var = re.escape(m.group(1))
                after = "\n".join(lines[i + 1:i + 8])
                if not re.search(rf"rmtree[^\n]*\b{var}\b", after):
                    leaks.append(f"{os.path.basename(path)} line {i + 1}: {line.strip()}")
        self.assertEqual([], leaks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
