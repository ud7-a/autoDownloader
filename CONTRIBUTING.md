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
