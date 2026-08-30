# System Architecture

Auto Episodes Downloader consists of two main components:
1. **Desktop Client Application** (`core/`, `ui/`, `utils/`, `tools/`)
2. **Cloud Notification Microservice** (`service/`)

---

## 🖥️ 1. Desktop Client Application

The desktop application is built with **Python 3.13** and **PyQt6** using **QFluentWidgets** for a native WinUI 3 Fluent Dark interface.

### Key Subsystems:
- **Downloader Tab (`ui/downloader_tab.py`)**:
  - Directs download requests to the multi-stream engine.
  - Controls concurrency, volume, directory path, and active profiles.
- **Watchlist Tab (`ui/watchlist_tab.py`)**:
  - Manages followed anime series and displays airing schedules.
  - Handles Cloud Discord Notifications configuration and synchronization.
- **Search Tab (`ui/search_tab.py`)**:
  - Scrapes search results from supported anime providers.
  - Allows 1-click following directly to the Watchlist.
- **Profile Manager (`ui/manager_tab.py`)**:
  - Configurable data-driven site flow definitions (XPaths, delays, and download button navigation).
- **History Tab (`ui/history_tab.py`)**:
  - SQLite database (`download_history.db`) recording all completed downloads with search and redownload capabilities.

---

## ☁️ 2. Cloud Notification Microservice (`service/`)

The cloud microservice runs independently in the cloud (hosted on Render or VPS) to provide 24/7 episode tracking and Discord alerts even when the user's PC is turned off.

### Architectural Principles:
1. **Catalogue Deduplication**: Multiple users following the same anime share a single record in the `anime` table. The scraper polls each show only once per cycle.
2. **Encrypted at Rest**: Subscriber Discord Webhooks are encrypted using **Fernet (AES-128-CBC + HMAC-SHA256)** with the secret `AED_NOTIFY_KEY`.
3. **Daily Schedule Filtering**: The checker only queries anime scheduled to air **today**, minimizing server load and external site traffic.
4. **Backlog Suppression**: When a user follows an existing show, their notification baseline is seeded to the current episode to prevent spamming older episodes.
