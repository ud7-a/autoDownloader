"""Run the app from source against a throwaway sandbox -- never your real data.

    py tools/dev_run.py                    # the sandbox, created with sample data on first run
    py tools/dev_run.py --fresh            # start over from the sample data
    py tools/dev_run.py --real-downloads   # real browser downloads (into the sandbox)
    py tools/dev_run.py --whats-new 5.0.0  # as the first launch after updating from 5.0.0
    py tools/dev_run.py -- --library       # anything after -- is passed to the app

Everything lives under one folder (default %TEMP%\\AED-sandbox, or --dir):

    app\\        the app's data: settings, profiles, Library, history, browser profile
    animes\\     the download folder, with sample episodes that play in mpv.net
    mpv.net\\    mpv.net's config folder as the app sees it (Apply shaders lands here)
    shortcuts\\  keyboard shortcut files (Windows never sees them)
    mpv-state\\  mpv resume files

Sample data: a Watching anime part-way through (Continue, Free up space and the
episode dialog have something to show), one on Watch later with nothing
downloaded, one Completed, and a profile for each.

Stubbed, because they would reach outside the sandbox:
  * downloads -- each episode is a copy of the sample clip "downloaded" over a
    few seconds, with the same signals, history and Library records (unless
    --real-downloads);
  * killing leftover browsers at start-up (it would kill the real app's);
  * the Windows start-up entry and the background watcher;
  * making mpv.net the default player, and installing or updating it with winget.
It runs side by side with the installed app: its own single-instance lock, and a
"[SANDBOX]" window title the real app's shortcuts never hand over to.

Still real: Search and the Watchlist check (they read the sites), playing an
episode (the installed mpv.net, with its own config -- so playback in the sandbox
isn't ticked off in its Library), Windows notifications, and cloud notifications
if you turn them on in the sandbox.
"""
import argparse
import json
import os
import runpy
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = ".aed-sandbox"          # only a folder holding this is ever deleted by --fresh
SAMPLE_CLIP = "sample.mp4"
STEP_SECONDS = 0.15              # per 5% of a pretend download (tests set 0)

# (title, status, episodes, downloaded, finished in mpv.net, part-way: (ep, percent))
SAMPLE_ANIME = [
    ("Sample Show", "watching", 12, range(1, 9), range(1, 6), (6, 40.0)),
    ("Demo Adventure", "later", 24, (), (), None),
    ("Test Movie Saga", "completed", 3, range(1, 4), range(1, 4), None),
]


def paths(root):
    return {name: os.path.join(root, name)
            for name in ("app", "animes", "mpv.net", "shortcuts", "mpv-state")}


def point_at_sandbox(root):
    """Before any app module is imported: its data folders are read at import."""
    p = paths(root)
    os.environ["AED_APP_DIR"] = p["app"]
    os.environ["AED_SHORTCUT_DIR"] = p["shortcuts"]
    os.environ["AED_MPV_STATE_DIR"] = p["mpv-state"]
    for folder in p.values():
        os.makedirs(folder, exist_ok=True)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


# ------------------------------------------------------------------ sample data

def make_clip(path):
    """A short playable video (test pattern and a tone), made offline by mpv.net's
    libmpv. Without mpv.net the episodes are placeholders that won't play."""
    try:
        import ctypes
        from utils import intro_detect
        dll = intro_detect.find_libmpv()
        if not dll:
            raise RuntimeError("mpv.net is not installed")
        lib = intro_detect._load(dll)
        h = lib.mpv_create()
        opts = {"o": path, "of": "mp4", "ovc": "libx264", "ovcopts": "preset=ultrafast,crf=34",
                "oac": "aac", "audio-files": "av://lavfi:sine=frequency=440:duration=20",
                "terminal": "no", "config": "no", "load-scripts": "no", "idle": "no", "ytdl": "no"}
        for k, v in opts.items():
            lib.mpv_set_option_string(h, k.encode(), v.encode())
        try:
            if lib.mpv_initialize(h) < 0:
                raise RuntimeError("libmpv could not start")
            args = (ctypes.c_char_p * 3)(
                b"loadfile", b"av://lavfi:testsrc2=size=480x270:rate=24:duration=20", None)
            lib.mpv_command(h, args)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if lib.mpv_wait_event(h, 1.0).contents.event_id in (1, 7):   # shutdown, end-file
                    break
        finally:
            lib.mpv_terminate_destroy(h)
        if os.path.getsize(path) > 0:
            return True
    except Exception as e:
        print(f"[sandbox] no playable sample clip ({e}); episodes are placeholders")
    with open(path, "wb") as f:
        f.write(b"\0" * 1024)
    return False


def seed(root):
    """Sample settings, profiles and Library, written through the app's own code
    so they look exactly like the real thing."""
    import json
    from core.site_flows import DEFAULT_SITE_FLOWS
    from utils import watch_later as wl
    from utils.config import app_settings, config_lock, load_config, save_config, sites_data
    from utils.naming import safe_folder_name

    p = paths(root)
    clip = os.path.join(root, SAMPLE_CLIP)
    make_clip(clip)
    load_config()
    flow = DEFAULT_SITE_FLOWS["witanime.site"]
    with config_lock:
        app_settings["download_dir"] = p["animes"]
        for title, *_rest in SAMPLE_ANIME:
            slug = title.lower().replace(" ", "-")
            sites_data[title] = {
                "url": f"https://witanime.site/episode/{slug}-الحلقة-{{x}}/",
                "next_btn_xpath": flow["next_btn_xpath"],
                "step_paths": json.loads(json.dumps(flow["step_paths"])),
            }
    save_config()

    for title, status, count, downloaded, finished, partway in SAMPLE_ANIME:
        slug = title.lower().replace(" ", "-")
        url = f"https://witanime.site/anime/{slug}/"
        wl.add(title, url, "witanime.site")
        wl.set_parts(url, [{"template": sites_data[title]["url"], "max_ep": count, "first_ep": 1}])
        wl.link_profile(url, 0, title)
        folder = os.path.join(p["animes"], safe_folder_name(title))
        os.makedirs(folder, exist_ok=True)
        for ep in downloaded:
            shutil.copyfile(clip, os.path.join(folder, f"{safe_folder_name(title)} Ep{ep}.mp4"))
        # Played "in mpv.net": the same log lines the progress script writes.
        lines = [json.dumps({"path": os.path.join(folder, f"{title} Ep{ep}.mp4"),
                             "percent": 100.0, "eof": True}) for ep in finished]
        if partway:
            ep, percent = partway
            lines.append(json.dumps({"path": os.path.join(folder, f"{title} Ep{ep}.mp4"),
                                     "percent": percent, "eof": False}))
        if lines:
            wl.apply_progress(lines)
        wl.set_status(url, status)
    with open(os.path.join(root, MARKER), "w", encoding="utf-8") as f:
        f.write("Auto Episodes Downloader sandbox (tools/dev_run.py). Safe to delete.\n")


# ------------------------------------------------------------- fake downloads

def make_fake_download(clip):
    def fake_download(site_key, episodes_list, download_dir, headless, webhook_url,
                      selected_sound, volume, concurrency, step_paths_override=None):
        """Stands in for core.selenium_engine.run_selenium_task: the same signals,
        history and Library records, but each episode is the sample clip
        "downloaded" over a few seconds. Pause and Cancel work; nothing goes online."""
        import core.selenium_engine as eng
        from core.concurrency import ConcurrencyController
        from core.signals import signals
        from utils.database import log_history
        from utils.naming import safe_folder_name

        eng.CURRENT_TASK_ID += 1
        task = eng.CURRENT_TASK_ID
        eng.cancel_event.clear()
        eng.pause_event.clear()

        def stopped():
            return eng.cancel_event.is_set() or eng.CURRENT_TASK_ID != task

        episodes = list(episodes_list)
        name = safe_folder_name(site_key)
        folder = os.path.join(download_dir, name)
        os.makedirs(folder, exist_ok=True)
        controller = ConcurrencyController(start=concurrency, enabled=False)
        with eng._run_lock:
            eng.RUN_STATE.clear()
            eng.RUN_STATE.update(task_id=task, episodes=episodes, started=set(),
                                 headless=bool(headless), controller=controller)
        signals.concurrency_changed.emit(controller.describe())
        signals.update_progress.emit(0, len(episodes))
        signals.update_status.emit(f"Status: [sandbox] Pretend-downloading {len(episodes)} "
                                   "episode(s)...", "#f39c12")
        done = []
        for ep in episodes:
            if stopped():
                break
            signals.add_active_download.emit(ep)
            for pct in range(0, 101, 5):
                while eng.pause_event.is_set() and not stopped():
                    time.sleep(0.2)
                if stopped():
                    break
                signals.update_active_bar.emit(ep, pct)
                signals.update_active_download.emit(ep, f"⚡ 8.0 MB/s   •   {pct}% of 400 MB")
                time.sleep(STEP_SECONDS)
            if stopped():
                break
            shutil.copyfile(clip, os.path.join(folder, eng.episode_filename(name, ep, ".mp4")))
            # As the real engine does: a finished episode leaves the session that
            # "resume after a crash" offers.
            from utils.config import app_settings, config_lock, save_config
            with config_lock:
                session = app_settings.get("unfinished_session")
                if session and ep in session.get("episodes", []):
                    session["episodes"].remove(ep)
                    if not session["episodes"]:
                        app_settings.pop("unfinished_session", None)
            save_config()
            signals.remove_active_download.emit(ep)
            done.append(ep)
            signals.update_progress.emit(len(done), len(episodes))

        with eng._run_lock:
            if eng.RUN_STATE.get("task_id") == task:
                eng.RUN_STATE.clear()
        status = "Cancelled" if stopped() else "Success" if len(done) == len(episodes) else "Partial"
        log_history(site_key, eng.compact_spec(episodes), status, "Sandbox: pretend download.")
        if done:
            from utils.skip_times import queue_folder
            from utils.watch_later import record_sources
            from utils.config import sites_data
            record_sources(site_key, done, sites_data.get(site_key, {}).get("url", ""), folder)
            queue_folder(folder)
        signals.update_buttons.emit(True, False, True)
        if stopped():
            signals.update_status.emit("Status: ❌ Download Cancelled.", "#e74c3c")
            time.sleep(1)
            signals.task_cancelled.emit()
        else:
            signals.update_status.emit("Status: 🎉 [sandbox] All episodes 'downloaded'.", "#2ecc71")
            signals.run_report.emit({"profile": site_key, "episodes": episodes, "failed": []})
            signals.task_finished.emit([])
    return fake_download


# ------------------------------------------------------------------ the stubs

def stub_outside_effects(root, real_downloads):
    """Replace what would act outside the sandbox. Applied before the app imports
    the modules that use them, so every `from x import y` picks up the stub."""
    import core.selenium_engine as eng
    import utils.config as config
    import utils.instance as instance
    import utils.mpvnet as mpvnet

    p = paths(root)
    # Leftover-browser sweep: matches any "SeleniumProfile" or headless Chrome --
    # the real app's included.
    eng.kill_stuck_chrome_processes = lambda started_before=None: None
    if not real_downloads:
        eng.run_selenium_task = make_fake_download(os.path.join(root, SAMPLE_CLIP))
    # Windows start-up entry and the background watcher.
    config.set_windows_autostart = lambda enabled: False
    config.start_background_watcher = lambda: False
    # mpv.net: its config folder is the sandbox's; Windows-wide changes are refused.
    mpvnet.config_dir = lambda exe_path=None: p["mpv.net"]
    refused = 1223                      # ERROR_CANCELLED, as if the UAC prompt was declined

    def refuse(what):
        def stub(*_args, **_kwargs):
            print(f"[sandbox] refused: {what}")
            return refused
        return stub
    mpvnet.enable_default_policy = refuse("make mpv.net the default player")
    mpvnet.disable_default_policy = refuse("stop managing the default player")
    mpvnet.run_winget_install = refuse("install mpv.net with winget")
    mpvnet.run_winget_upgrade = refuse("update mpv.net with winget")
    # Its own single-instance lock, so it runs beside the installed app.
    instance.MUTEX_NAME = "Local\\AED_Sandbox_Running_Mutex"

    # A title the real app's shortcut hand-over (utils/instance.py) never matches.
    from ui.app_window import AppWindow
    original_init = AppWindow.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.setWindowTitle(f"[SANDBOX] {self.windowTitle()}")
    AppWindow.__init__ = init


def next_version(version):
    """"5.2.0" after "5.1.0": the release the Unreleased notes will become."""
    parts = [int(p) for p in version.split(".")] + [0, 0]
    return f"{parts[0]}.{parts[1] + 1}.0"


def pretend_update(root, seen):
    """This launch acts as the first after updating from `seen`: the app takes the
    next version's number when CHANGELOG.md has Unreleased notes (as a release
    would), and the sandbox's settings say What's new was last shown for `seen`.
    Must run before anything imports APP_VERSION by name. Returns the version."""
    from utils import changelog, config
    unreleased = dict((t.lower(), b) for t, b in changelog.sections(changelog.read_bundled()))
    if changelog.features(unreleased.get(changelog.UNRELEASED.lower(), "")):
        config.APP_VERSION = next_version(config.APP_VERSION)
    path = os.path.join(paths(root)["app"], "sites_config.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("settings", {})["whats_new_seen"] = seen
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    return config.APP_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=os.path.join(tempfile.gettempdir(), "AED-sandbox"),
                        help="sandbox folder (default: %%TEMP%%\\AED-sandbox)")
    parser.add_argument("--fresh", action="store_true", help="start over from the sample data")
    parser.add_argument("--real-downloads", action="store_true",
                        help="download for real (still into the sandbox's folders)")
    parser.add_argument("--whats-new", metavar="FROM",
                        help="start as the first launch after updating from version FROM "
                             "(What's new shows; Unreleased notes count as the next version)")
    parser.add_argument("app_args", nargs="*", help="arguments for the app, after --")
    args = parser.parse_args()
    root = os.path.abspath(args.dir)

    if args.fresh and os.path.isdir(root):
        if not os.path.isfile(os.path.join(root, MARKER)):
            sys.exit(f"[sandbox] {root} isn't a sandbox (no {MARKER}); not deleting it.")
        shutil.rmtree(root)
    point_at_sandbox(root)
    from utils import config
    if config.APP_DIR != paths(root)["app"] or not config.IS_ISOLATED:
        sys.exit("[sandbox] the app's data folder isn't the sandbox; stopping.")
    if not os.path.isfile(os.path.join(root, MARKER)):
        print(f"[sandbox] creating sample data in {root} ...")
        seed(root)
    if args.whats_new:
        version = pretend_update(root, args.whats_new)
        print(f"[sandbox] acting as the first launch after updating {args.whats_new} -> {version}")
    stub_outside_effects(root, args.real_downloads)
    print(f"[sandbox] {root}")

    os.chdir(ROOT)
    sys.argv = [os.path.join(ROOT, "main.py"), *args.app_args]
    runpy.run_path(os.path.join(ROOT, "main.py"), run_name="__main__")


if __name__ == "__main__":
    main()
