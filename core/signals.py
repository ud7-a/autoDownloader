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

# We instantiate it here so it's a true global singleton
signals = WorkerSignals()