# 🌐 Cloud Notifications & Remote Download Trigger — Architecture & Implementation Handover

> **Purpose of this Document**: This document provides a complete, developer-ready specification of the **24/7 Cloud Episode Notification & One-Click Remote Download Execution** feature in **Auto Episodes Downloader (AED)**. It contains the architecture, data models, security protocols, lifecycle flows, and bug fixes applied to guide any future development in Claude Code.

---

## 1. Executive Summary & Core Objective

The user needed two interrelated capabilities:
1. **24/7 Cloud Release Monitoring Without Leaving PC On**: Monitor Arabic anime releases (specifically WitAnime and Animerco) around the clock from a zero-maintenance cloud backend. When a new episode releases, dispatch an immediate rich embed notification to the user's private Discord channel.
2. **One-Click Remote Download Execution from Mobile**: An interactive action link in the Discord notification that allows the user (on their phone) to trigger the episode download on their PC with a single tap.
   - If the PC is **ONLINE 🟢**: The PC app immediately opens, switches to the Downloader tab, and begins downloading the episode via Aria2.
   - If the PC is **OFFLINE 🔴 / TURNED OFF**: The command queues safely in the cloud backend. As soon as the PC boots or turns on, the app launches automatically and executes the download.
3. **Ultra-Low Resource Footprint**: When the main PyQt6 GUI window is closed, it must not consume memory. A background service monitors for incoming commands using **< 25 MB RAM and 0.0% CPU**.

---

## 2. System Architecture & Components

```
                +-------------------------------------------------------------+
                |                    Render Cloud Backend                     |
                |                 (FastAPI + SQLite + httpx)                  |
                |          https://aed-notification-service.onrender.com      |
                +-------------------------------------------------------------+
                        |                                       ^
         1. Hourly Release Check                       2. Heartbeat (every 15s)
         & WitAnime / Animerco Scrape                  & Command Polling / ACKs
                        v                                       |
                +---------------+                       +----------------------+
                | Discord Webhook|                      | Local PC Environment |
                +---------------+                       +----------------------+
                        |                                          |
         3. Rich Embed Notification                                |
            [ 📥 Start Download on PC (Online 🟢) ]                |
                        |                                          |
                        v (User taps on phone)                     v
                +-------------------------------+       +----------------------+
                |  Render Action Landing Page   |       | Background Watcher   |
                |    (/v1/queue?sid=...&sig=...) | ----> | (aed_watcher.pyw)    |
                +-------------------------------+       | ~20 MB RAM / 0.0% CPU|
                        |                               +----------------------+
                 4. User confirms                                  |
                    [ 🚀 Start Download on PC ]                    | 5. Detects Command
                        |                                          v
                        v                               +----------------------+
                +---------------+                       | Main AED GUI Window  |
                | Cloud Command | --------------------> | (main.py / PyQt6)    |
                |     Queue     |                       | - Pops to foreground |
                +---------------+                       | - Downloader tab     |
                                                        | - Aria2 downloads ep |
                                                        | - Plays 1 chime      |
                                                        +----------------------+
```

### Component A: Cloud Notification Service (`service/`)
- **Framework**: FastAPI + SQLite (`service/store.py`, `service/api.py`, `service/checker.py`, `service/crypto.py`).
- **Hosted At**: `https://aed-notification-service.onrender.com` (deployed from GitHub `main` branch).
- **Scheduled Job**: Runs an automated release check every hour on WitAnime & Animerco.
- **Security**:
  - Webhook URLs are encrypted at rest using AES-GCM-256 (`AED_NOTIFY_KEY`).
  - Action URLs are signed via HMAC-SHA256 (`AED_NOTIFY_KEY`) to prevent URL forgery or unauthorized queuing.
  - Client authentication uses Bearer tokens hashed with SHA-256 (`token_hash`).

### Component B: Featherweight Background Watcher (`core/watcher.py` / `aed_watcher.pyw`)
- **Executable**: `pythonw.exe` (`C:\Users\Admin\AppData\Local\Programs\Python\Python313\pythonw.exe`).
- **Resource Footprint**: Consistently **~20 MB RAM** and **0.0% CPU**.
- **Process Characteristics**:
  - Windowless background daemon (`.pyw`), no console terminal window.
  - Heartbeat cycle: Sends a lightweight `POST /v1/subscribers/{sid}/heartbeat` every 15s so the cloud backend knows the PC is **ONLINE 🟢**.
  - Command poll: Calls `GET /v1/subscribers/{sid}/commands`. If commands exist, it spawns `main.py` and waits.
  - Mutex/Process coordination: If the full GUI window is active, the watcher pauses to avoid duplicate network traffic.
  - Logging: Detailed status written to `C:\Auto Episodes Downloader\watcher.log`.

### Component C: Main Desktop GUI Application (`main.py` / `ui/`)
- **Framework**: PyQt6 + QFluentWidgets (WinUI 3 Fluent Dark Theme).
- **Startup Logic**:
  - Checks for pending remote commands on boot / manual launch (`initial_commands = cloud_fetch_commands()`).
  - Enforces Single Instance via Windows Named Mutex (`Local\AED_Main_App_Running_Mutex`).
  - If a second instance is launched, it enumerates windows, restores the existing window to the foreground, and exits cleanly.
  - Brings window into active focus using Windows Win32 API (`ShowWindow(hwnd, SW_RESTORE)`, `SetForegroundWindow(hwnd)`, `BringWindowToTop(hwnd)`, `SwitchToThisWindow(hwnd, True)`).
- **Command Execution (`ui/app_window.py::_execute_remote_commands`)**:
  - Acknowledges received commands (`cloud_ack_command`) to Render immediately to prevent duplicate polls.
  - Maintains `_processed_cmd_ids` session set to guarantee no command is executed twice.
  - Switches to `DownloaderWidget`, maps the target anime from `watchlist` to get the URL and template, and starts the Aria2 download task.
  - Plays the completion sound **once** when the download finishes.

---

## 3. Key Design Decisions & Critical Fixes

### 1. Permanent Deterministic Subscriber IDs
- **Problem**: Previously, every sync or registration generated a random UUID (`uuid.uuid4().hex`). If older notifications remained in Discord, tapping them sent commands to an old subscriber queue that the PC was no longer listening to.
- **Solution**: Subscriber IDs are now **deterministically derived from the user's Discord Webhook URL**:
  ```python
  subscriber_id = hashlib.sha256(webhook_url.strip().encode("utf-8")).hexdigest()[:32]
  ```
  This guarantees that all notifications—past, present, and future—always route to the exact same queue on the user's PC.

### 2. Render Ephemeral Container Self-Healing
- **Problem**: On Render free tier, containers restart on deploy or idle spin-down, wiping the ephemeral SQLite database. Subsequent web requests or heartbeats returned 401/404/500 ("Session Expired").
- **Solution**:
  - `store.authenticate(subscriber_id, token)`: If a valid 32-character subscriber ID and token arrive on an uninitialized container, it automatically provisions the subscriber in SQLite on the fly.
  - `store.queue_command(...)`: Auto-provisions the subscriber row (`INSERT OR IGNORE`) so verified HMAC-signed action links from Discord never throw foreign key errors.

### 3. Desktop Window Elevation & Avoiding Terminal Window
- **Problem**:
  - Using `os.startfile(main_py)` caused Windows file associations to launch `py.exe`, opening a black Windows Terminal console instead of the GUI.
  - Background processes on Windows are restricted from stealing focus, causing Qt windows to minimize or remain hidden behind other windows.
- **Solution**:
  - Watcher explicitly spawns `main.py` with `pythonw.exe` (`C:\...\Python313\pythonw.exe`), eliminating the console terminal.
  - Added Windows Win32 API elevation in `main.py` and `ui/app_window.py`:
    ```python
    import ctypes
    hwnd = int(window.winId())
    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    ctypes.windll.user32.BringWindowToTop(hwnd)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    ctypes.windll.user32.SwitchToThisWindow(hwnd, True)
    ```

### 4. Single-Instance Named Mutex with Prefix Window Matching
- **Problem**: When `main.py` tried to check if another instance was running, it looked for exact title `"Auto Episodes Downloader"`. But the app window title is versioned: `"Auto Episodes Downloader | Version 4.4.0"`. `FindWindowW` returned NULL, causing duplicate instances or premature exits.
- **Solution**: Implemented `EnumWindows` with prefix matching (`buf.value.startswith("Auto Episodes Downloader")`) to reliably identify and restore the existing window across all version updates.

### 5. Double Completion Sound Elimination
- **Problem**: The finishing sound was playing twice because:
  1. An 8-second startup check in `WatchlistCheckThread` detected the new episode and played `_play_notify_sound()`.
  2. The Aria2 download finished in `selenium_engine.py` and played the download finish chime.
- **Solution**:
  - Suppressed `_play_notify_sound()` in `ui/watchlist_tab.py` whenever `cloud_notify_enabled` is active or a download is in progress.
  - Added sound debouncing (15s minimum gap between notification sounds).

### 6. Client Token Obfuscation Decryption in Watcher
- **Problem**: `utils/config.py` saves `cloud_token` to `sites_config.json` with reversed base64 obfuscation. The watcher was reading the raw obfuscated string and sending it as a Bearer token, causing `401 Unauthorized`.
- **Solution**: Added `_decrypt_token()` in `core/watcher.py` so the watcher always transmits the cleartext Bearer token.

---

## 4. Key File & Directory Map

| Path | Purpose |
|---|---|
| `service/api.py` | Cloud FastAPI backend: endpoints for registration, sync, heartbeats, queuing, command polling, and web confirmation landing page (`/v1/queue`). |
| `service/store.py` | SQLite data layer for subscribers, follows, anime metadata, heartbeats, and commands queue. |
| `service/checker.py` | Core scraping engine for WitAnime & Animerco releases; builds Discord embeds with dynamic online status buttons and HMAC signatures. |
| `service/crypto.py` | AES-GCM encryption for webhooks; HMAC-SHA256 URL signing; SHA-256 token hashing. |
| `core/watcher.py` | Lightweight background polling service (~20 MB RAM). Sends 15s heartbeats, fetches commands, and launches `main.py`. |
| `aed_watcher.pyw` | Windowless entry point script for the background watcher service. |
| `main.py` | Main application bootstrap: handles single-instance mutex, startup command execution, Qt styling, and window elevation. |
| `ui/app_window.py` | Main Qt window: handles tray icon, command deduplication (`_execute_remote_commands`), Downloader switching, and download triggering. |
| `ui/watchlist_tab.py` | Watchlist UI tab: includes Cloud Notifications configuration dialog, webhook syncing, and sound debouncing. |
| `utils/config.py` | Application settings manager (`sites_config.json`), cloud sync helpers (`cloud_send_heartbeat`, `cloud_fetch_commands`, `cloud_ack_command`). |

---

## 5. API Endpoints Reference (Cloud Backend)

- `GET /v1/health` $\rightarrow$ Health check (`{"status": "ok"}`).
- `POST /v1/subscribers` $\rightarrow$ Register or get subscriber ID (deterministic hash of webhook URL). Returns `{"id": "...", "token": "...", "masked_webhook": "..."}`.
- `PUT /v1/subscribers/{id}/watchlist` $\rightarrow$ Sync subscriber's followed anime list with `seen_max` and release days.
- `POST /v1/subscribers/{id}/heartbeat` $\rightarrow$ Records active heartbeat; marks subscriber online.
- `GET /v1/subscribers/{id}/commands` $\rightarrow$ Fetches pending remote download commands (`[{"id": 1, "anime_url": "...", "episodes": "14"}]`).
- `POST /v1/subscribers/{id}/commands/{cmd_id}/ack` $\rightarrow$ Acknowledges and deletes command from queue.
- `GET /v1/queue?sid={sid}&url={url}&ep={ep}&sig={sig}` $\rightarrow$ User-facing mobile landing page from Discord button; displays status, anime title, episode number, and confirmation button.
- `POST /v1/queue/submit` $\rightarrow$ Enqueues verified command to subscriber's queue on Render.
- `POST /v1/check` $\rightarrow$ Triggers manual release check sweep across all subscribers.

---

## 6. Maintenance & Troubleshooting Quick Guide

1. **How to verify watcher is running**:
   - Open PowerShell: `Get-Process -Name python, pythonw | Select-Object Id, ProcessName, WorkingSet64`
   - Check log file: `C:\Auto Episodes Downloader\watcher.log` (shows timestamped heartbeats every 15s).
2. **How to run test suite**:
   - `py -m unittest discover -s service/tests -v` (All 55 tests should pass).
   - `py tools/lint.py` (Must report `lint: clean`).
3. **How to start the watcher manually if stopped**:
   - Run: `Start-Process "C:\Users\Admin\AppData\Local\Programs\Python\Python313\pythonw.exe" -ArgumentList "c:\Users\Admin\Desktop\pyQt backUp\aed_watcher.pyw" -WorkingDirectory "c:\Users\Admin\Desktop\pyQt backUp"`
4. **How Render receives changes**:
   - Render automatically builds and deploys from GitHub repository `https://github.com/ud7-a/autoDownloader` on branch `main`. Any `git push origin main` triggers automatic deployment within ~30–45 seconds.
