"""Keep the app alive through unexpected errors, and write down what they were.

The installed app is a windowed build: there is no console, so an unexpected Python
error has nowhere to go. Worse, when one happens inside a Qt callback (a button, a
timer, a signal), PyQt's default reaction is to call qFatal and abort the process --
Windows records that as exception 0xC0000409 in Qt6Core.dll, which is exactly what
the Discord-triggered downloads kept dying with, three times in a minute on
2026-09-30, with no trace of the error anywhere.

install() changes both halves of that:

  * sys.excepthook is replaced. PyQt only aborts when the hook is the default one;
    with ours in place it reports the error to us and the app carries on, so one
    failing callback no longer takes the whole app down.
  * every such error -- on the GUI thread, in a worker thread, or a native crash
    via faulthandler -- is appended with its traceback to last_errors.txt in the app
    folder. Not a .log file on purpose: the clear-on-start deletes *.log, and a
    crash report that vanishes on the next launch would be useless.
"""

import datetime
import faulthandler
import os
import sys
import threading
import traceback

REPORT_NAME = "last_errors.txt"
# Separate file: faulthandler keeps it open for the whole session, so it must never
# be rewritten underneath it by the trimming below. Only ever written on a hard crash.
NATIVE_NAME = "last_native_crash.txt"
MAX_BYTES = 256 * 1024          # keeps the newest errors; old ones are trimmed away

_path = None
_native = None                  # kept open: faulthandler writes to it on a hard crash
_lock = threading.Lock()


def report_path():
    return _path


def _version():
    try:
        from utils.config import APP_VERSION
        return APP_VERSION
    except Exception:
        return "?"


def _trim():
    """Keep the file bounded: drop the oldest half once it passes MAX_BYTES."""
    try:
        if os.path.getsize(_path) <= MAX_BYTES:
            return
        with open(_path, "rb") as f:
            data = f.read()
        keep = data[len(data) // 2:]
        cut = keep.find(b"\n=== ")
        with open(_path, "wb") as f:
            f.write(keep[cut + 1:] if cut != -1 else keep)
    except Exception:
        pass


def record(kind, text):
    """Append one error to the report. Never raises -- it runs while handling one."""
    if not _path:
        return
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"=== {stamp}  {kind}  (version {_version()}, "
    entry += f"{'installed app' if getattr(sys, 'frozen', False) else 'source'})\n{text.rstrip()}\n\n"
    with _lock:
        try:
            with open(_path, "a", encoding="utf-8") as f:
                f.write(entry)
            _trim()
        except Exception:
            pass


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc, tb)
        return
    record("unhandled error", "".join(traceback.format_exception(exc_type, exc, tb)))
    # Still show it when there is a console (running from source).
    if sys.stderr is not None:
        try:
            traceback.print_exception(exc_type, exc, tb)
        except Exception:
            pass


def _thread_excepthook(args):
    if args.exc_type is SystemExit:
        return
    name = getattr(args.thread, "name", "?")
    record(f"unhandled error in thread {name!r}",
           "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))


def install(app_dir):
    """Install the hooks and point the report at app_dir. Safe to call once, early."""
    global _path, _native
    try:
        os.makedirs(app_dir, exist_ok=True)
        _path = os.path.join(app_dir, REPORT_NAME)
    except Exception:
        _path = None
        return False
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    try:
        # A hard crash (access violation, abort) can't run Python code afterwards,
        # but faulthandler, living in C, still writes every thread's stack.
        native_path = os.path.join(app_dir, NATIVE_NAME)
        # One line per launch adds up; trim it here, before faulthandler holds it open.
        if os.path.exists(native_path) and os.path.getsize(native_path) > MAX_BYTES:
            with open(native_path, "rb") as f:
                tail = f.read()[-(MAX_BYTES // 2):]
            with open(native_path, "wb") as f:
                f.write(tail)
        _native = open(native_path, "a", encoding="utf-8", buffering=1)
        _native.write(f"--- session started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}, "
                      f"version {_version()}; a stack below this line means it crashed ---\n")
        faulthandler.enable(file=_native, all_threads=True)
    except Exception:
        _native = None
    return True
