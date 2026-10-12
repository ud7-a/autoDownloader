# Security Policy

## Supported versions

Security fixes target the **latest stable release**. Users of older releases should
upgrade before reporting an issue that has already been fixed. Check the
[releases page](https://github.com/ud7-a/autoDownloader/releases/latest) for the
current stable version; prereleases are intended for testing.

## Desktop storage

- The default data directory is `C:\Auto Episodes Downloader`. The installed
  application lives in its `App` subfolder.
- Settings, profiles, browser data, and download history use that data directory.
  Set `AED_APP_DIR` before launch to use a different directory. Downloaded media
  uses the folder selected in the app.
- Desktop Discord webhooks and cloud tokens in `sites_config.json` use reversed
  Base64 encoding. This is **reversible obfuscation, not encryption**. Anyone who
  can read that file can recover those credentials.
- Cloud notification setup sends the configured webhook to the service and
  synchronizes followed-show information. Authenticated service calls use the
  subscriber token. This does not synchronize the local download-history database.

## Cloud service

- Webhook values stored in PostgreSQL are encrypted with Fernet using
  `AED_NOTIFY_KEY`. Subscriber access tokens are stored as SHA-256 hashes and
  compared using `hmac.compare_digest`.
- Registration returns a token and a masked webhook. Plaintext webhooks are not
  returned by the API.
- Remote download actions use HMAC-SHA256 signatures derived from
  `AED_NOTIFY_KEY`. Losing or replacing the key prevents existing webhook
  decryption and invalidates previously signed actions.
- Configure `DATABASE_URL` for persistent PostgreSQL storage. A local SQLite
  file or container volume is not the service database.
- `/v1/health` reports API availability; it does not verify the database,
  encryption key, source availability, or successful notification delivery.

See the [deployment guide](docs/DEPLOYMENT.md) and
[service README](service/README.md) for configuration.

## Reporting a vulnerability

Report vulnerabilities through the repository's private security advisory flow,
when available, or contact the maintainer privately. Do not include credentials
or exploit details in a public issue. Include the affected version, reproduction
steps, and the impact.
