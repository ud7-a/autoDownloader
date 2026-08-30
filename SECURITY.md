# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 4.4.x   | :white_check_mark: |
| < 4.4.0 | :x:                |

---

## 🔒 Security Architecture & Guarantees

1. **Discord Webhook Encryption**:
   - In the **Cloud Notification Microservice**, subscriber Discord Webhook URLs are encrypted at rest using **Fernet authenticated cryptography (AES-128-CBC + HMAC-SHA256)**.
   - Subscriber access tokens are stored as **SHA-256 hashes** and compared with `hmac.compare_digest` to prevent timing attacks.
   - Discord Webhook URLs are never written to logs or returned in plain text in REST API responses.
2. **Desktop Storage**:
   - Configuration files are stored safely in `%LOCALAPPDATA%\Auto Episodes Downloader` or user-configured directories.
   - Download history and sensitive keys are not synced outside of explicit user configuration.

---

## 🛡️ Reporting a Vulnerability

If you discover a security vulnerability within Auto Episodes Downloader, please do not file a public issue.

Instead, please report security vulnerabilities responsibly by creating a private security advisory on GitHub or by contacting the repository maintainers.
