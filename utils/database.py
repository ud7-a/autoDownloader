import os
import sqlite3
import threading
from datetime import datetime
from utils.config import APP_DIR, DB_FILE

db_lock = threading.RLock()

def init_db():
    os.makedirs(APP_DIR, exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS downloads_v2
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      date TEXT,
                      profile TEXT,
                      episodes TEXT,
                      status TEXT,
                      notes TEXT)''')
        conn.commit()
        conn.close()

def log_history(profile, episodes_str, status, notes):
    try:
        # We import signals here to avoid Circular Import errors when starting the app!
        from core.signals import signals 
        
        date_str = datetime.now().strftime("%b %d, %Y • %I:%M %p")
        with db_lock:
            conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            c = conn.cursor()
            c.execute("INSERT INTO downloads_v2 (date, profile, episodes, status, notes) VALUES (?, ?, ?, ?, ?)",
                      (date_str, profile, str(episodes_str), status, str(notes)))
            conn.commit()
            conn.close()
        signals.history_updated.emit()
    except Exception as e:
        print(f"Database Error: {e}")