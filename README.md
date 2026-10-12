<div align="center">

<img src="assets/app_icon.png" alt="Auto Episodes Downloader icon" width="96">

# Auto Episodes Downloader

**Find, download, and watch anime from one Windows desktop app.**

Follow weekly releases, manage your library, and receive Discord alerts even while your PC is off.

[![Latest release](https://img.shields.io/github/v/release/ud7-a/autoDownloader)](https://github.com/ud7-a/autoDownloader/releases/latest)
[![CI](https://img.shields.io/github/actions/workflow/status/ud7-a/autoDownloader/ci.yml?branch=main&label=CI)](https://github.com/ud7-a/autoDownloader/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/)
[![Windows](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue)](https://github.com/ud7-a/autoDownloader/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**[Download for Windows](https://github.com/ud7-a/autoDownloader/releases/latest)** · [Getting started](#getting-started) · [Features](#features) · [Development](#development) · [Documentation](#documentation)

</div>

## Getting started

**Requirements:** Windows 10 or 11 and Google Chrome. Python is only needed when running from source. mpv.net is optional; the **Video Player** tab can install it through winget.

### Install the app

1. Download **`AutoDownloader_Setup.exe`** from the [latest release](https://github.com/ud7-a/autoDownloader/releases/latest).
2. Run the installer and open the app using its desktop shortcut.
3. Open **Search Anime**, find a show, and select **Load**. Choose a season if prompted.
4. In **Downloader**, choose your download folder and episodes, then start the download.
5. When episodes are ready, select **Start Watching**, or open them from **Library**.

The installer places the app in `C:\Auto Episodes Downloader\App`. Settings, profiles, history, and browser data are stored under `C:\Auto Episodes Downloader`. Downloaded episodes are saved to the folder you choose. The installed app offers available updates on launch; you can also select **Check for updates** in the sidebar.

### Run without the installer

Download **`AutoDownloader_Portable.zip`**, extract it, and run **`AutoDownloader.exe`**. This skips installation, but the app still uses the default data directory above. It does not keep all user data beside the executable.

## Features

| Area | What you can do |
| --- | --- |
| Search | Browse both supported sites with cover art, pagination, and movie badges. Load a show using a built-in download profile. |
| Downloads | Download multiple episodes with aria2c, automatically adjust concurrency, try available mirrors in profile order, and extract ZIP, RAR, and 7z archives. |
| Episode selection | Select ranges, drag across an episode grid, skip filler when the source provides tags, and change pending selections while paused. |
| Recovery | Resume an unfinished session after a restart and retry missing episodes. |
| Library | Organize shows as Watching, Watch later, or Completed; import existing downloads; and continue watching or downloading. |
| Watchlist | Follow shows, see releases grouped by weekday, and download new episodes individually or together. |
| Notifications | Receive Windows notifications and Discord alerts, with optional cloud checking and remote download commands. |
| Playback | Open episodes in mpv.net with a playlist and optional upscaling shaders. |
| Interface | Use a Fluent-style dark interface with responsive layouts, high-DPI posters, and touch scrolling. |

### Supported sources

- [witanime](https://witanime.site)
- [animerco](https://eta.animerco.org)

Downloads depend on the source site's availability and the mirrors it offers. **Profile Manager** lets you adjust automation paths, import or export profiles as JSON, and build paths with the Smart Browser Picker. The automation browser includes ad and popup blocking.

### Library and playback

The Library keeps download history and source information for each show. It can scan your download folder for existing episodes and offers a **Continue watching** action.

With the bundled mpv.net progress script configured, playback at 90% or later marks an episode as watched. Playback recorded while AED is closed is picked up when the Library opens again.

The **Video Player** tab can install mpv.net, apply shader presets, and help configure it as the default player for `.mp4` and `.mkv` files. Existing mpv.net configuration is backed up before presets are applied.

| mpv.net shortcut | Action |
| --- | --- |
| `F1` | Anime shader profile: Anime4K |
| `F2` | Series and movie shader profile: FSRCNNX, KrigBilateral, SSimDownscaler, and adaptive sharpen |
| `Ctrl+1` | Toggle shaders |
| `P` | Show active shaders |

Shader performance depends on your GPU. See [third-party notices](assets/mpvnet/THIRD_PARTY.txt) for shader sources and licenses.

## Discord and cloud notifications

The optional cloud service checks followed shows and sends Discord alerts while your PC is off. In **Watchlist**, open the cloud notification settings, enter the service URL and your Discord webhook, and enable cloud notifications.

- The repository's [Cloud checker workflow](.github/workflows/cloud-checker.yml) requests a check every **10 minutes**. GitHub scheduling is best-effort, so alerts may arrive later.
- The API also starts a built-in checker by default, with a **15-minute wait between cycles**. Set `AED_DISABLE_CHECKER=1` when using only an external scheduler.
- The desktop Watchlist checks shows scheduled for today, plus shows with no known release day, on launch and again every **30 minutes** while the app is running.
- Discord alerts include a download link. Remote downloads require your PC to be on, with AED or its background watcher running and connected to the service.
- The service encrypts stored webhooks with Fernet and signs download actions with HMAC. Shows shared by multiple subscribers are checked once per cycle.

**Source limitation:** animerco blocks requests from cloud servers. Use witanime for cloud alerts; the desktop app can check animerco while your PC is on.

The service uses FastAPI and PostgreSQL. See the [deployment guide](docs/DEPLOYMENT.md) and [service README](service/README.md) for database, encryption key, and scheduling setup. Desktop webhook and token storage uses reversible obfuscation; cloud webhook storage uses Fernet encryption.

## Development

Use **Python 3.13** on Windows 10 or 11, with Google Chrome installed. Run these commands in PowerShell:

```powershell
git clone https://github.com/ud7-a/autoDownloader.git
cd autoDownloader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Keep development data separate from your installed app.
$env:AED_APP_DIR = Join-Path $env:LOCALAPPDATA "AED-Development"
.\.venv\Scripts\python.exe main.py
```

Set `AED_APP_DIR` before launching the app to redirect settings, history, browser data, and helper tools. Runs from source or with an alternate data directory do not install automatic updates over the installed app.

### Checks

```powershell
.\.venv\Scripts\python.exe tools/lint.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The desktop suite isolates app data in a temporary directory. CI runs lint, import checks, desktop tests, and a Windows build.

The cloud service has separate dependencies and tests. Use a dedicated test PostgreSQL database; the suite resets its test schema:

```powershell
.\.venv\Scripts\python.exe -m pip install -r service/requirements.txt
$env:DATABASE_URL = "postgresql://user:password@localhost:5432/aed_test"
$env:AED_NOTIFY_SCHEMA = "aed_test"
.\.venv\Scripts\python.exe -m unittest discover -s service/tests -v
```

Replace the example connection string with your test database credentials. Service tests supply a test encryption key; production deployments need their own `AED_NOTIFY_KEY`.

### Build

```powershell
# Build the app and setup installer in dist/.
.\.venv\Scripts\python.exe tools/build_release.py

# Build only dist/AutoDownloader/.
.\.venv\Scripts\python.exe tools/build_release.py --app-only
```

### Release

Maintainers can open **Actions → Release → Run workflow** on GitHub, enter an `X.Y.Z` version, and optionally mark it as a prerelease. The workflow runs checks, updates the version and changelog, builds the app, and publishes the installer and portable ZIP.

The [release workflow](.github/workflows/release.yml) also accepts `v*` tags. For that route, the tag must match `APP_VERSION` in `utils/config.py`. See the [release guide](docs/RELEASING.md) for details; its `publish.py` helper is maintainer-local and is not included in a clone.

## Architecture

AED uses **PyQt6 and QFluentWidgets** for the desktop interface, **Selenium and Chrome** for source navigation, and **aria2c** for downloads. Cloud notifications run independently of the desktop app.

```mermaid
flowchart TD
    App[Desktop app] -->|Selenium + Chrome| Sites[Anime sources]
    App -->|aria2c| Mirrors[Download mirrors]
    App -->|Sync follows and notifications| Service[Cloud service]
    Checker[Scheduled GitHub workflow] -->|Request check cycle| Service
    Service -->|Check followed shows| Sites
    Service --> Database[(PostgreSQL)]
    Service -->|Episode alerts| Discord[Discord]
    Discord -->|Signed download action| Service
    Watcher[Background watcher] -->|Heartbeat and command polling| Service
    Watcher -->|Start downloads| App
    App -->|Play episodes| Player[mpv.net]
```

| Path | Responsibility |
| --- | --- |
| `main.py` | Desktop entry point and background watcher mode (`--watcher`) |
| `ui/` | App window, tabs, reusable widgets, and layout helpers |
| `core/` | Download engine, site flows, scheduling, concurrency, watcher, and updater |
| `utils/` | Configuration, storage, player integration, notifications, and diagnostics |
| `service/` | Cloud API, checker, PostgreSQL store, and service tests |
| `tests/` | Desktop regression tests and site fixtures |
| `assets/` | Icons, sounds, mpv.net configuration, scripts, and shaders |
| `tools/` | Build and maintenance scripts, plus bundled download and extraction tools |

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Cloud deployment](docs/DEPLOYMENT.md)
- [Cloud service setup and tests](service/README.md)
- [Release process](docs/RELEASING.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy and vulnerability reporting](SECURITY.md)

Report bugs using the [bug report template](https://github.com/ud7-a/autoDownloader/issues/new?template=bug_report.yml), or suggest improvements using the [feature request template](https://github.com/ud7-a/autoDownloader/issues/new?template=feature_request.yml). Include the app version and steps to reproduce; remove webhooks, tokens, and other secrets from logs or screenshots before sharing them.

## License

The repository's own code is distributed under the [MIT License](LICENSE). Bundled shaders and other third-party components retain their respective licenses; see [third-party notices](assets/mpvnet/THIRD_PARTY.txt).
