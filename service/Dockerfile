# Multi-stage Dockerfile for Auto Episodes Downloader Cloud Notification Service
FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service codebase
COPY . /app/service

# Default environment variables
ENV PYTHONUNBUFFERED=1
ENV AED_NOTIFY_DB=/data/notify.db
ENV PORT=8000

# Volume mount point for persistent SQLite database
VOLUME ["/data"]

EXPOSE 8000

CMD ["sh", "-c", "python -m uvicorn service.api:app --host 0.0.0.0 --port ${PORT}"]
