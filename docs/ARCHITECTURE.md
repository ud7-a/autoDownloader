# System Architecture

Auto Episodes Downloader has a Windows desktop app and an optional cloud
notification service. Their dependencies and persistence are separate.

## Desktop app

The interface uses **Python 3.13**, **PyQt6**, and **QFluentWidgets**. It has a
Fluent-style dark appearance; the widgets are Qt widgets.

| Module | Responsibility |
| --- | --- |
| `main.py` | App startup, single-instance handling, and background watcher mode |
| `ui/downloader_tab.py` | Download configuration, episode selection, and session recovery |
| `ui/search_tab.py` | Source search, show details, and loading built-in profiles |
| `ui/watchlist_tab.py` | Followed shows, release schedules, and cloud notification settings |
| `ui/watch_later_tab.py` | Library: Watching, Watch later, Completed, and playback actions |
| `ui/manager_tab.py` | Editable site automation paths and profile import/export |
| `core/selenium_engine.py` | Chrome navigation, mirror selection, and aria2c download execution |
| `core/watcher.py` | Remote command polling when the main app is closed |
| `utils/config.py` | Settings, profiles, and desktop cloud-service calls |
| `utils/database.py` | Local SQLite download history |
| `utils/watch_later.py` | Library persistence and watched-episode state |
| `utils/library_scan.py` | Importing local media and mpv playback state |

The default desktop data directory is `C:\Auto Episodes Downloader`;
`AED_APP_DIR` redirects it. Media uses the selected download folder.
The installer places executables in the data directory's `App` subfolder.
The portable build also uses the default data directory unless redirected.

The Library stores entries in `watch_later.json` and associates download history
with each show. Local entries use `local://<folder>` identities until merged
with a source-backed entry. The bundled `aed-progress.lua` records playback on
pause, file end, shutdown, and every 30 seconds. The Library reads that log and
mpv resume state to update watched episodes.

Desktop code does not import the cloud service modules: those require packages
that are not part of the desktop runtime.

## Cloud service

The service uses **FastAPI**, **PostgreSQL**, and the fetchers in
`service/checker.py`. `service/api.py` handles HTTP requests,
`service/store.py` owns database access, and `service/crypto.py` handles webhook
encryption, token hashing, and signed download actions.

- Each source URL has a shared `anime` record. A check cycle queries it once and
  fans out notifications to its subscribers.
- Release filtering includes **today, yesterday, and tomorrow in UTC**, plus
  shows without a known release day. This accommodates timezone differences.
- Initial discovery establishes a baseline rather than sending the full backlog.
- Webhooks are encrypted with Fernet. PostgreSQL holds subscribers, follows,
  episode watermarks, and remote commands. The desktop's credential storage
  uses reversible obfuscation, as described in [SECURITY.md](../SECURITY.md).

## Scheduling and remote downloads

| Mechanism | Current behavior |
| --- | --- |
| Desktop Watchlist | Checks today's shows and unknown release days on launch and every 30 minutes |
| Built-in API checker | Enabled by default; waits 900 seconds after each cycle |
| GitHub Cloud checker workflow | Requests `POST /v1/check` every 10 minutes; scheduled runs can be delayed |
| Remote command handling | The open app polls commands; the background watcher handles them when the app is closed |

The API checker and GitHub workflow are separate triggers. Set
`AED_DISABLE_CHECKER=1` when using only an external scheduler. Remote downloads
require the user's PC to be on and the app or watcher to be connected.

See the [deployment guide](DEPLOYMENT.md) and
[service README](../service/README.md) for required environment variables.
