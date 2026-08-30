# Cloud Service Deployment Guide

The notification service can be hosted for **$0 / month** on **Render** or deployed on any Linux VPS or Docker host.

---

## 🚀 Option 1: Render.com (1-Click Blueprint)

1. Fork or push this repository to GitHub.
2. In [Render Dashboard](https://dashboard.render.com), click **New + → Blueprint**.
3. Select this repository.
4. Render will read `render.yaml` and configure the service automatically.
5. Click **Apply**.

---

## 🐳 Option 2: Docker Compose (VPS / Server)

```bash
# 1. Navigate to the service folder
cd service

# 2. Generate an encryption key
export AED_NOTIFY_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 3. Start the container
docker compose up -d
```

---

## 🔄 Keeping Render Awake (Free Tier)

Render puts free containers to sleep after 15 minutes of inactivity. To keep it active 24/7 for free:

1. Create a free account at [UptimeRobot.com](https://uptimerobot.com).
2. Add a new **HTTP(s)** monitor pointing to:
   `https://your-service.onrender.com/healthz`
3. Set the check interval to **every 5 minutes**.
