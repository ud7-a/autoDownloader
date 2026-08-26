"""Builds the shippable release artifacts. Used by CI and reproducible locally.

    py tools/build_release.py                     # app + installer
    py tools/build_release.py --app-only          # just dist/AutoDownloader/
    py tools/build_release.py --expect-version 4.2.3

Two artifacts come out of this, in order:

  1. dist/AutoDownloader/            -- the app itself (--onedir; faster to launch
                                        than --onefile, which re-extracts on every run)
  2. dist/AutoDownloader_Setup.exe   -- a --onefile installer with (1) bundled inside
                                        it as data; installer.py reads it back out of
                                        sys._MEIPASS/AutoDownloader

The Setup name is a contract, not a preference: core/updater.py picks the release
asset whose name contains "Setup" and ends with .exe, then runs it with --silent.
Renaming the output here breaks in-app updates for every installed copy.

exeCompile.py is intentionally untracked (maintainer-local deploy convenience), so
the PyInstaller flags are duplicated here rather than imported -- this file is the
one CI can actually see. Keep the two in sync when either changes.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "AutoDownloader"
SETUP_NAME = "AutoDownloader_Setup"
ICON = os.path.join("assets", "app_icon.ico")

APP_FLAGS = [
    "--noconfirm", "--onedir", "--windowed",
    "--name", APP_NAME,
    "--collect-all", "selenium",
    "--collect-all", "qfluentwidgets",
    "--hidden-import", "PyQt6.QtXml",
    "--hidden-import", "PyQt6.QtSvg",
    # Imported inside core.extensions.load_into (browser-level CDP call that installs
    # the ad blocker), so name it explicitly rather than rely on static analysis.
    "--hidden-import", "websocket",
    "--exclude-module", "PyQt5",
    # Trim packages installed in the dev environment but never imported by the app.
    # Every extra file costs launch time (disk I/O + antivirus scanning).
    # numpy and PIL stay: qfluentwidgets genuinely uses them.
    #
    # scipy does NOT stay. qfluentwidgets imports gaussian_filter for one thing --
    # AcrylicLabel's blur -- and this app forces Mica and Acrylic off, so that code
    # never runs. It was still costing 48 MB across 85 files on every cold start.
    # utils.fast_start already stubs the import out; its fallback now returns the
    # image unblurred when scipy is absent, so nothing can crash on it.
    "--exclude-module", "scipy",
    "--exclude-module", "cv2",
    "--exclude-module", "Pythonwin",
    "--exclude-module", "tkinter",
    "--exclude-module", "matplotlib",
    "--exclude-module", "pandas",
    "--exclude-module", "pytest",
    "--exclude-module", "IPython",
    "--exclude-module", "notebook",
    "--icon", ICON,
    "--add-data", "assets;assets",
    "--add-data", "tools;tools",
    "main.py",
]

# Files that must survive into the bundle. A build that silently loses one of these
# still launches, then fails at runtime on a user's machine -- so fail here instead.
REQUIRED_IN_BUNDLE = [
    os.path.join("_internal", "tools", "aria2c.exe"),
    os.path.join("_internal", "tools", "unrar.exe"),
    os.path.join("_internal", "tools", "ublock_lite.crx"),
    os.path.join("_internal", "assets", "finishingDownloadSound.wav"),
]


def log(msg):
    print(f"[build] {msg}", flush=True)


def run(cmd):
    log(" ".join(cmd))
    if subprocess.run(cmd, cwd=REPO).returncode != 0:
        sys.exit("[build] FAILED")


def pyinstaller(flags):
    run([sys.executable, "-m", "PyInstaller"] + flags)


def app_version():
    """Reads APP_VERSION out of utils/config.py without importing it (importing
    pulls in Qt and touches the real config directory)."""
    src = open(os.path.join(REPO, "utils", "config.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']', src, re.M)
    if not m:
        sys.exit("[build] APP_VERSION not found in utils/config.py")
    return m.group(1)


def ensure_icon():
    if os.path.exists(os.path.join(REPO, ICON)):
        return
    log("icon missing, generating it")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    subprocess.run([sys.executable, os.path.join("tools", "make_icon.py")],
                   cwd=REPO, env=env, check=True)


def clean():
    for folder in ("build", "dist"):
        path = os.path.join(REPO, folder)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
    for spec in (f"{APP_NAME}.spec", f"{SETUP_NAME}.spec"):
        path = os.path.join(REPO, spec)
        if os.path.exists(path):
            os.remove(path)


def build_app():
    log("compiling app")
    ensure_icon()
    pyinstaller(APP_FLAGS)

    out = os.path.join(REPO, "dist", APP_NAME)
    if not os.path.exists(os.path.join(out, f"{APP_NAME}.exe")):
        sys.exit("[build] app exe missing from dist/")
    missing = [f for f in REQUIRED_IN_BUNDLE if not os.path.exists(os.path.join(out, f))]
    if missing:
        sys.exit("[build] bundle is missing required files:\n  " + "\n  ".join(missing))
    log(f"app OK ({_dir_mb(out):.0f} MB)")


def build_installer():
    """Wraps dist/AutoDownloader/ into the single-file Setup exe."""
    app_dir = os.path.join(REPO, "dist", APP_NAME)
    if not os.path.exists(app_dir):
        sys.exit("[build] build the app before the installer")

    log("compiling installer")
    # --windowed: installer talks to the user through ctypes MessageBoxW, so a console
    # would only flash on screen during a silent auto-update.
    pyinstaller([
        "--noconfirm", "--onefile", "--windowed",
        "--name", SETUP_NAME,
        "--icon", ICON,
        "--add-data", f"{app_dir}{os.pathsep}{APP_NAME}",
        "installer.py",
    ])

    setup = os.path.join(REPO, "dist", f"{SETUP_NAME}.exe")
    if not os.path.exists(setup):
        sys.exit("[build] setup exe missing from dist/")
    log(f"installer OK ({os.path.getsize(setup) / 1e6:.0f} MB) -> {setup}")
    return setup


def _dir_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-only", action="store_true")
    ap.add_argument("--installer-only", action="store_true")
    ap.add_argument("--expect-version", metavar="X.Y.Z",
                    help="fail unless utils/config.py APP_VERSION matches (leading v ok)")
    args = ap.parse_args()

    version = app_version()
    if args.expect_version:
        want = args.expect_version.lstrip("v")
        if want != version:
            sys.exit(f"[build] version mismatch: tag says {want}, "
                     f"utils/config.py APP_VERSION is {version}")
        log(f"version {version} matches the tag")

    if not args.installer_only:
        clean()
        build_app()
    if not args.app_only:
        build_installer()
    log(f"done (version {version})")


if __name__ == "__main__":
    main()
