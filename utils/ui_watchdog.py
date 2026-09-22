"""Records what the UI thread was doing when the window froze.

Off unless AED_UI_WATCHDOG is set or --watchdog is passed, so it costs nothing
normally.

A freeze is easy to feel and hard to catch: by the time the app responds again the
evidence is gone, and reproducing one from outside kept failing here because it
depends on the machine, the network and what the user is doing. This runs inside the
real app instead.

Two recorders, because one is not enough:

  * A plain Python watcher thread compares a heartbeat that a QTimer bumps on the GUI
    thread. When the heartbeat goes stale it grabs the main thread's stack through
    sys._current_frames(), which works precisely because that thread is stuck. This
    gives the cleanest answer -- but only when the frozen thread has released the GIL.

  * faulthandler, re-armed on every heartbeat. If the GUI thread is stuck holding the
    GIL, the Python watcher above never gets scheduled and records nothing at all.
    faulthandler's timer lives in C and dumps every thread's stack without needing the
    GIL, so it still fires. Each heartbeat cancels and re-arms it, so it only ever
    goes off when the heartbeat genuinely stops.

    set AED_UI_WATCHDOG=1     (PowerShell: $env:AED_UI_WATCHDOG="1")
    py main.py --watchdog     (works in any shell)

Then reproduce the freeze and read APP_DIR/ui_stalls.log.
"""

import faulthandler
import os
import sys
import threading
import time
import traceback

LOG_NAME = "ui_stalls.log"
MAX_LOG_BYTES = 512 * 1024

_timer = None      # module-level so the QTimer is never garbage collected
_handle = None     # kept open: faulthandler writes to this file descriptor


def enabled():
    """Either the environment variable or the flag turns this on.

    The flag exists because `set AED_UI_WATCHDOG=1` is cmd syntax: in PowerShell
    `set` is an alias for Set-Variable, so it creates a shell variable the process
    never sees and the watchdog silently stays off.
    """
    return bool(os.environ.get("AED_UI_WATCHDOG")) or "--watchdog" in sys.argv


def log_path():
    from utils.config import APP_DIR
    return os.path.join(APP_DIR, LOG_NAME)


def _write(text):
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        _handle.write(f"\n[{stamp}] {text}\n")
        _handle.flush()
    except Exception:
        pass


def start(threshold_seconds=3.0):
    """Begin watching. Returns True if armed. Raises nothing.

    The caller is expected to REPORT a False return rather than swallow it: this
    module once went missing entirely and the call site hid the ImportError, so the
    watchdog looked armed while recording nothing.
    """
    global _timer, _handle
    if not enabled():
        return False
    try:
        from PyQt6.QtCore import QTimer
    except Exception:
        return False

    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")
    except Exception:
        pass
    _handle = open(path, "a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=_handle)

    beat = {"at": time.monotonic()}
    main_id = threading.main_thread().ident

    def tick():
        beat["at"] = time.monotonic()
        try:
            # Cancels the previous arming and starts a fresh countdown. It only
            # fires if these ticks stop, i.e. the GUI thread has frozen.
            faulthandler.dump_traceback_later(threshold_seconds, repeat=False,
                                              file=_handle, exit=False)
        except Exception:
            pass

    _timer = QTimer()
    _timer.timeout.connect(tick)
    _timer.start(1000)

    def watch():
        while True:
            time.sleep(0.2)
            gap = time.monotonic() - beat["at"]
            if gap < threshold_seconds:
                continue
            frame = sys._current_frames().get(main_id)
            stack = "".join(traceback.format_stack(frame)) if frame else "<no frame>"
            _write(f"UI thread blocked ~{gap * 1000:.0f} ms so far. It is here:\n{stack}")
            froze_at = time.monotonic() - gap
            while time.monotonic() - beat["at"] >= threshold_seconds:
                time.sleep(0.2)
            _write(f"UI thread recovered; that freeze lasted "
                   f"~{(beat['at'] - froze_at) * 1000:.0f} ms")

    threading.Thread(target=watch, daemon=True, name="ui-watchdog").start()
    _write(f"watchdog armed (threshold {threshold_seconds}s, pid {os.getpid()})")
    return True
