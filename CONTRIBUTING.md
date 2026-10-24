# Contributing to Auto Episodes Downloader

## Development setup

The desktop app requires **Python 3.13**, **Windows 10 or 11**, and **Google Chrome**.
Fork the repository, then run these commands in PowerShell:

```powershell
git clone https://github.com/<your-username>/autoDownloader.git
cd autoDownloader
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# Keep development settings and history separate from the installed app.
$env:AED_APP_DIR = Join-Path $env:LOCALAPPDATA "AED-Development"
.\.venv\Scripts\python.exe main.py
```

Without `AED_APP_DIR`, source runs use `C:\Auto Episodes Downloader`, the same
default data directory as the installed app. Set the variable before importing
app modules or launching the app.

## Code and tests

Keep changes focused, follow the surrounding style, and add type annotations
where they clarify interfaces. Add regression tests for changes to site parsing,
downloads, or storage. Keep network and browser operations out of pure logic
tests. Desktop production code must not import from `service/`: the packaged
app installs desktop dependencies separately from server dependencies.

Run the desktop checks from the repository root:

```powershell
.\.venv\Scripts\python.exe tools/lint.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The desktop test suite redirects app data, shortcuts, and mpv resume state to
temporary directories. CI runs lint, an offscreen import check, desktop tests,
and a Windows app/installer build.

Desktop tests are split by area, so one area can run on its own, for example
`python -m unittest tests.test_library`:

| File | Covers |
| --- | --- |
| `test_downloads.py` | Episode ranges, the download engine, mirrors and tabs, retries, pausing, the Downloader tab |
| `test_sites.py` | witanime and animerco flows, seasons, search, filler, the shipped site definitions |
| `test_watchlist.py` | Release days, the daily check, new-episode notices, covers |
| `test_library.py` | Watched tracking, Continue and its shortcut, folder import, the episode dialog, Free up space |
| `test_player.py` | The mpv.net config and scripts, skip openings and endings, keyboard layouts |
| `test_cloud.py` | Windows and Discord notifications, the cloud checker, remote downloads, the watcher |
| `test_app.py` | Config loading and migration, start-up speed, layout, tooltips, menus, touch, error reports |
| `test_release.py` | What the build ships, the installed app's imports, release notes, the updater |
| `test_isolation.py` | The suite never touching real data; every temp folder a test makes is removed |

| `test_dev_run.py` | The sandbox below: sample data, pretend downloads, nothing reaching outside it |

Each file imports the `tests` package before any app module, so the redirect
above also applies when a single file runs.

### Trying the app from source

Run the app against a sandbox, never your own data:

```powershell
.\.venv\Scripts\python.exe tools/dev_run.py            # created with sample data on first run
.\.venv\Scripts\python.exe tools/dev_run.py --fresh    # start over
```

The sandbox (`%TEMP%\AED-sandbox` by default) holds the app's data, a download
folder with playable sample episodes, mpv.net's config as the app sees it, and the
shortcut files. It has an anime part-way through, one on Watch later and a
completed one. Downloads are pretended (`--real-downloads` downloads for real, into
the sandbox). The start-up entry, the background watcher, the default-player and
winget actions are switched off, and the sandbox runs beside the installed app
under a `[SANDBOX]` title. See the top of `tools/dev_run.py` for the full list.

### Service changes

Install the service dependencies and use a dedicated PostgreSQL test database:

```powershell
.\.venv\Scripts\python.exe -m pip install -r service/requirements.txt
$env:DATABASE_URL = "postgresql://user:password@localhost:5432/aed_test"
$env:AED_NOTIFY_SCHEMA = "aed_test"
.\.venv\Scripts\python.exe -m unittest discover -s service/tests -v
```

Replace the example connection string with your test database credentials.
Service tests clear their test schema and supply a test encryption key.
`requirements-dev.txt` does not install the service dependencies. The current
CI and Release workflows run the desktop suite; run the service suite separately
when changing the service.

See the [service README](service/README.md) for runtime configuration and
[README](README.md#development) for build commands.

## Submitting a pull request

1. Create a feature branch, such as `git checkout -b codex/your-feature-name`.
2. Make the change and run the applicable checks above.
3. Commit with a descriptive message and push the branch to your fork.
4. Open a pull request against `main`. Explain the resulting behavior, validation,
   and any checks you could not run.

Use the [bug report](.github/ISSUE_TEMPLATE/bug_report.yml) or
[feature request](.github/ISSUE_TEMPLATE/feature_request.yml) template for issues.
Remove credentials from logs and screenshots. Report vulnerabilities as described
in [SECURITY.md](SECURITY.md).
