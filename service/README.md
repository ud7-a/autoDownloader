# Cloud Notification Service

The optional AED service checks followed anime and sends Discord notifications
while the user's PC is off. Remote download commands are handled by the desktop
app or background watcher when the PC is on.

The API uses FastAPI; subscribers, follows, episode watermarks, and commands live
in **PostgreSQL**. A shared source URL is fetched once per check cycle. Webhooks
are encrypted with Fernet, access tokens are stored as hashes, and download
actions are HMAC-signed.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Required PostgreSQL connection string |
| `AED_NOTIFY_KEY` | Required Fernet key for webhook encryption and action signatures |
| `AED_NOTIFY_SCHEMA` | Database schema; defaults to `public` |
| `AED_DISABLE_CHECKER` | Any non-empty value disables the API's built-in checker |
| `PORT` | Container HTTP port; defaults to `8000` |

Keep the database and encryption key across restarts. The service creates its
schema and tables on database access; the database account needs permission to
create and update them. There is no SQLite backend, and `AED_NOTIFY_DB` is not
used by the store.

Generate a key **once**, then save it outside the host and reuse it:

```bash
python -m pip install cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Losing or replacing `AED_NOTIFY_KEY` makes existing webhook values undecryptable
and invalidates old signed download links. A missing key causes encryption and
signing operations to fail; a successful health response does not prove that the
key or database is configured.

## Docker setup

Run from the repository root on a Docker host. The following commands use Bash:

```bash
export AED_NOTIFY_KEY="<saved_fernet_key>"
export DATABASE_URL="postgresql://user:password@database-host:5432/aed"
docker compose -f service/docker-compose.yml up -d --build
```

Replace both placeholders. The PostgreSQL host must be reachable **from the
container**; `localhost` inside the container refers to the container itself.
Compose requires both variables and builds with the repository root as context,
matching the Dockerfile's `COPY service/requirements.txt` instruction.
The API is exposed on port `8000`. No local database volume is required.

For Render and scheduling setup, see [the deployment guide](../docs/DEPLOYMENT.md).

## Run from source

Use Python 3.13. From the repository root, in a virtual environment:

```bash
python -m pip install -r service/requirements.txt
python -m playwright install --with-deps chromium
export AED_NOTIFY_KEY="<saved_fernet_key>"
export DATABASE_URL="postgresql://user:password@database-host:5432/aed"
python -m uvicorn service.api:app --host 0.0.0.0 --port 8000
```

These server commands use Bash. On Windows, assign variables using
`$env:NAME = "value"` in PowerShell.

## Scheduling

The API starts `checker.start_checker_loop` in a daemon thread by default. It
checks immediately, then waits **900 seconds after each cycle** before checking
again. You do not need to start a second standalone checker beside the API.

The repository also includes a [GitHub Cloud checker workflow](../.github/workflows/cloud-checker.yml)
that requests `POST /v1/check` every **10 minutes**. GitHub's scheduler is
best-effort. Set `AED_DISABLE_CHECKER=1` to use external scheduling alone, and
configure the repository variable `CLOUD_SERVICE_URL` with the service's base URL.

By default, a cycle includes shows scheduled for today, yesterday, or tomorrow
in UTC, plus shows without a known release day. Source availability still
determines whether a check can find episodes; animerco cloud requests may be
blocked.

## Desktop configuration

1. Open **Watchlist** in AED.
2. Open the cloud notification settings.
3. Enter the service URL and Discord webhook URL, then enable cloud notifications.

The desktop synchronizes followed shows. Its initial episode counts seed
notification baselines; first server discovery also establishes a baseline
instead of sending the full backlog. If the service loses a subscriber record,
the desktop can re-register after an authentication failure. This does not
restore lost follow or notification history from the database.

## Health and diagnostics

- `GET /v1/health` reports API availability. `/health` and `/healthz` are aliases.
- `POST /v1/check` runs a cycle and returns counts of checks, notifications, and errors.
- `GET /docs` exposes the API documentation.

The health endpoint does not test PostgreSQL access, the encryption key, or
notification delivery. Use a configured follow and a check cycle to verify the
notification path.

## Tests

Install the service dependencies, point `DATABASE_URL` at a dedicated test
PostgreSQL database, and run from the repository root:

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/aed_test"
export AED_NOTIFY_SCHEMA="aed_test"
python -m unittest discover -s service/tests -v
```

The tests supply a test encryption key and clear their test schema.
`reset_for_tests()` refuses to clear `public`. The desktop development
requirements do not include service dependencies, and the current CI and Release
workflows do not run the service suite.
