# Cloud Notification Microservice with Headless Playwright Chromium for Cloudflare Bypass
FROM python:3.13-slim

WORKDIR /app

# Install system utilities and SSL certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY service/requirements.txt /app/service/requirements.txt
RUN pip install --no-cache-dir -r /app/service/requirements.txt

# Install Playwright Chromium and required OS browser dependencies
RUN playwright install --with-deps chromium

# Copy codebase
COPY . /app

# Set PYTHONPATH so service.api and service.checker are found cleanly
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV AED_NOTIFY_DB=/data/notify.db
ENV PORT=8000

# Volume mount point for persistent SQLite database
VOLUME ["/data"]

EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn service.api:app --host 0.0.0.0 --port ${PORT}"]
