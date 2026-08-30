# Auto Episodes Downloader — Cloud Notification Service

This service enables **offline Discord notifications** for followed anime in your Watchlist. It periodically checks supported anime sources in the cloud and sends rich Discord notifications to subscribed users even when their personal computer is turned off.

---

## Key Features

1. **Catalogue Deduplication**: When multiple users follow the same anime (e.g. *One Piece*), the server checks the source page only **once** per cycle, keeping scraper load minimal.
2. **Encrypted at Rest**: Discord Webhook URLs are encrypted with **Fernet (AES-128-CBC + HMAC-SHA256)**. Plaintext webhooks are never stored, logged, or returned in API responses.
3. **Zero-Spam Baseline**: Adding an ongoing anime with 1,000 episodes will not spam your Discord channel with 1,000 messages. Initial subscription syncs to the current latest episode and only alerts on future releases.
4. **Rich Discord Embeds**: Formatted alerts with anime titles, episode numbers, links, timestamps, and colors.

---

## Quick Start (Docker / VPS)

### 1. Generate an Encryption Key
Run in Python:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 2. Run with Docker Compose
```bash
export AED_NOTIFY_KEY="<your_generated_fernet_key>"
docker compose up -d
```

The service will start on port `8000`.

---

## Manual Installation (Without Docker)

```bash
# 1. Install dependencies
pip install -r service/requirements.txt

# 2. Set environment variables
export AED_NOTIFY_KEY="<your_generated_fernet_key>"
export AED_NOTIFY_DB="/var/data/notify.db"

# 3. Start the API server
python -m uvicorn service.api:app --host 0.0.0.0 --port 8000
```

To run the background checker worker in standalone mode:
```python
from service.checker import start_checker_loop

start_checker_loop(interval_seconds=900)  # Checks every 15 minutes
```

---

## Desktop App Configuration

1. Open **Auto Episodes Downloader**.
2. Go to the **Watchlist** tab.
3. Look for the **☁️ Cloud Discord Notifications** card at the top.
4. Click the ⚙️ settings button, enter your **Cloud Service URL** (e.g. `https://your-domain.com` or `http://localhost:8000`) and your **Discord Webhook URL**.
5. Toggle the switch **ON**. Your followed anime will automatically sync with the cloud service!
