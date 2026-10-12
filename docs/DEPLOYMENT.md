# Cloud Service Deployment

The notification service needs a persistent **PostgreSQL database** and a saved
**Fernet encryption key**. Hosting the API container does not provision the
database. See [the service README](../service/README.md#configuration) for key
generation and required environment variables.

## Render blueprint

1. Fork or push the repository to GitHub.
2. In Render, create a Blueprint from the repository's [render.yaml](../render.yaml).
3. Configure `DATABASE_URL` with your PostgreSQL connection string and
   `AED_NOTIFY_KEY` with your saved Fernet key.
4. Apply the blueprint and wait for the container to build.
5. Open `https://<your-service>/v1/health` to check HTTP availability.
6. Configure the desktop cloud settings and run a check cycle to verify database
   access and notification delivery.

The blueprint uses the `free` web-service plan. Database hosting is configured
separately; confirm the selected providers' current limits and billing before
deployment. No persistent container disk is used for the service database.

## Scheduling

The API enables its built-in checker by default: it runs a cycle, then waits
15 minutes. The GitHub [Cloud checker workflow](../.github/workflows/cloud-checker.yml)
is an additional trigger that requests `POST /v1/check` every 10 minutes.

To use that workflow as the only scheduler:

1. Add `AED_DISABLE_CHECKER=1` to the service environment and restart the service.
2. Set the repository's **Actions variable** `CLOUD_SERVICE_URL` to the service
   base URL, such as `https://your-service.onrender.com`.
3. Ensure the **Cloud checker** workflow is enabled, then run it manually once.
4. Inspect the workflow summary for checks, notifications, and errors.

Scheduled GitHub runs can be delayed. Regular requests may reduce idle periods,
but this configuration does not guarantee uninterrupted availability or exact
notification timing. An HTTP health monitor checks availability; it does not
perform an episode check.

## Docker or VPS

Use a PostgreSQL server reachable from the container and configure your saved
key. From the repository root, in Bash:

```bash
export AED_NOTIFY_KEY="<saved_fernet_key>"
export DATABASE_URL="postgresql://user:password@database-host:5432/aed"
docker compose -f service/docker-compose.yml up -d --build
```

The compose file builds using the repository root and exposes port `8000`.
It requires both variables and does not create a PostgreSQL server. Leave
`AED_DISABLE_CHECKER` unset for built-in scheduling, or set it to `1` when an
external scheduler will request `POST /v1/check`.

For a public service, expose the API through HTTPS and use that HTTPS base URL
in the desktop cloud settings. The container must be able to reach the database,
source sites, and Discord.

## Persistence and recovery

Back up the PostgreSQL database and keep a separate copy of `AED_NOTIFY_KEY`.
Changing the key breaks decryption of stored webhooks and existing signed actions.
Container rebuilds do not remove externally stored PostgreSQL data.

For setup, tests, health endpoint behavior, and desktop configuration, see the
[service README](../service/README.md).
