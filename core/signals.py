from PyQt6.QtCore import QObject, pyqtSignal

class WorkerSignals(QObject):
    update_status = pyqtSignal(str, str) 
    update_progress = pyqtSignal(int, int) 
    update_buttons = pyqtSignal(bool, bool, bool) 
    task_finished = pyqtSignal(list) 
    history_updated = pyqtSignal()
    add_active_download = pyqtSignal(int)
    update_active_download = pyqtSignal(int, str)
    update_active_bar = pyqtSignal(int, int)
    remove_active_download = pyqtSignal(int)
    task_started = pyqtSignal()
    task_cancelled = pyqtSignal()
    update_available = pyqtSignal(str, str)
    add_picked_step = pyqtSignal(object, str)
    concurrency_changed = pyqtSignal(str)   # human-readable auto-concurrency state
    remote_commands_received = pyqtSignal(list)
    # Settings changed on the paused download screen: {"limit", "auto", "headless"}.
    # The Downloader tab mirrors them so its controls and saved settings agree.
    paused_settings_changed = pyqtSignal(dict)
    # A run ended on its own (not cancelled by the user), success or not:
    # {"profile": str, "episodes": [ints actually in the run], "failed": [ints]}.
    # task_finished only fires when something succeeded, so it can't report a
    # run where everything failed.
    run_report = pyqtSignal(dict)
    # A profile's known episode range grew (a Watchlist check found newer
    # episodes); carries the profile name so an open Downloader can widen its boxes.
    profile_limits_changed = pyqtSignal(str)

# We instantiate it here so it's a true global singleton
signals = WorkerSignals()