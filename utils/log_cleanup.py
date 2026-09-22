"""Start every session with empty logs, so they cannot pile up over months of use.

Several logs are appended to and never trimmed: watcher.log (the background watcher
writes a status line every cycle), chromedriver.log, cloud.log, and aria2c_error.log
with its rotated copy. Left alone they grow without limit; this machine's
watcher.log had reached 380 KB.

Only files named *.log or *.log.<n> directly in the app folder are touched. That
folder also holds the user's data -- sites_config.json, download_history.db,
SeleniumProfile/, watchlist_covers/ -- none of which can match. install_log.txt is
deliberately not a match: the installer writes it immediately before launching the
app, so clearing it here would erase every update's log the moment it was written.
"""

import os
import re

_LOG_NAME = re.compile(r".+\.log(\.\d+)?$", re.IGNORECASE)


def clear_logs(app_dir):
    """Delete this app's log files. Returns (cleared names, names that were in use).

    A file another process still holds open (an orphaned chromedriver writing its
    log, say) cannot be deleted on Windows; it is emptied instead. If even that
    fails it is left for next time. Never raises -- a log must not stop the app.
    """
    cleared, busy = [], []
    try:
        names = os.listdir(app_dir)
    except OSError:
        return cleared, busy
    for name in names:
        path = os.path.join(app_dir, name)
        if not _LOG_NAME.match(name) or not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            cleared.append(name)
            continue
        except OSError:
            pass
        try:
            with open(path, "w", encoding="utf-8"):
                pass
            cleared.append(name)
        except OSError:
            busy.append(name)
    return cleared, busy
