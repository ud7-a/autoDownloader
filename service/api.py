"""HTTP REST API for the cloud notification service.

Routes call service.store; no SQL lives here. Nothing in this module ever returns or
logs a Discord webhook URL. Webhooks are credentials and are stored encrypted.
"""

from contextlib import asynccontextmanager
import os
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, field_validator

from service import checker, crypto, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background checker worker on server startup (unless explicitly disabled in tests)
    stop_event = threading.Event()
    worker_thread = None
    if not os.environ.get("AED_DISABLE_CHECKER"):
        worker_thread = threading.Thread(
            target=checker.start_checker_loop,
            kwargs={"interval_seconds": 900, "stop_event": stop_event},
            daemon=True
        )
        worker_thread.start()
    yield
    stop_event.set()


app = FastAPI(
    title="Auto Episodes Downloader - Cloud Notification Service",
    version="1.0",
    lifespan=lifespan
)


class RegistrationRequest(BaseModel):
    webhook: str

    @field_validator("webhook")
    @classmethod
    def validate_discord_webhook(cls, v: str) -> str:
        v = v.strip()
        # Ensure it is a genuine Discord webhook URL to avoid open relay abuse
        if not (v.startswith("https://discord.com/api/webhooks/") or
                v.startswith("https://discordapp.com/api/webhooks/")):
            raise ValueError("Must be a valid https://discord.com/api/webhooks/ URL")
        return v


class WatchItem(BaseModel):
    url: str
    title: str = ""
    release_day: str = ""
    seen_max: int = 0


class WatchlistSyncRequest(BaseModel):
    items: list[WatchItem]


def require_subscriber(subscriber_id: str, authorization: str = Header(default="")) -> str:
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not token or not store.authenticate(subscriber_id, token):
        raise HTTPException(status_code=401, detail="Invalid subscriber ID or Bearer token")
    return subscriber_id


@app.get("/")
def root():
    return {
        "service": "Auto Episodes Downloader - Cloud Notification Service",
        "status": "online",
        "version": "1.0",
        "endpoints": {
            "health": "/v1/health",
            "check": "/v1/check",
            "docs": "/docs"
        }
    }


@app.get("/health")
@app.get("/healthz")
@app.get("/v1/health")
def health():
    return {"status": "ok"}


@app.post("/v1/check")
def trigger_check():
    """Manual trigger to immediately run a check cycle across all due anime."""
    stats = checker.run_checker_cycle()
    return {"status": "completed", "stats": stats}


@app.get("/v1/test_scrape")
def test_scrape(url: str):
    """Debug endpoint to inspect scraper results directly from Render."""
    max_ep = checker.fetch_latest_episode(url)
    scraper_key_present = bool(os.environ.get("SCRAPER_API_KEY", "").strip())
    return {
        "url": url,
        "scraper_api_key_configured": scraper_key_present,
        "max_episode": max_ep,
        "status": "success" if max_ep > 0 else "no_episodes_or_blocked"
    }


@app.post("/v1/subscribers", status_code=201)
def register(body: RegistrationRequest):
    sid, token = store.create_subscriber(body.webhook)
    # The plaintext token is shown only once in this response; only its hash is stored
    return {
        "id": sid,
        "token": token,
        "webhook": crypto.mask_webhook(body.webhook)
    }


@app.put("/v1/subscribers/{subscriber_id}/watchlist")
def sync_watchlist(
    subscriber_id: str,
    body: WatchlistSyncRequest,
    _auth: str = Depends(require_subscriber)
):
    items_dicts = [item.model_dump() for item in body.items]
    following_count = store.replace_follows(subscriber_id, items_dicts)
    return {"following": following_count}


@app.delete("/v1/subscribers/{subscriber_id}", status_code=204)
def unsubscribe(
    subscriber_id: str,
    _auth: str = Depends(require_subscriber)
):
    store.delete_subscriber(subscriber_id)
    store.prune_orphan_anime()
    return Response(status_code=204)


@app.post("/v1/subscribers/{subscriber_id}/heartbeat")
def heartbeat(
    subscriber_id: str,
    _auth: str = Depends(require_subscriber)
):
    """Updates the subscriber's online heartbeat timestamp."""
    store.record_heartbeat(subscriber_id)
    return {"status": "ok", "online": True}


@app.get("/v1/subscribers/{subscriber_id}/commands")
def get_commands(
    subscriber_id: str,
    _auth: str = Depends(require_subscriber)
):
    """Retrieves pending remote commands queued for this subscriber's PC."""
    store.record_heartbeat(subscriber_id)
    cmds = store.get_pending_commands(subscriber_id)
    return {"commands": cmds}


@app.post("/v1/subscribers/{subscriber_id}/commands/{command_id}/ack")
def ack_command(
    subscriber_id: str,
    command_id: int,
    _auth: str = Depends(require_subscriber)
):
    """Acknowledges execution of a remote download command."""
    ok = store.ack_command(subscriber_id, command_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Command not found or already acknowledged")
    return {"status": "acknowledged"}


@app.get("/v1/queue", response_class=Response)
def queue_remote_download(sid: str, url: str, title: str = "", ep: str = "", sig: str = ""):
    """Public web action triggered when user taps the Download button on Discord from their phone."""
    action_key = f"{url}:{ep}"
    if not crypto.verify_action(sid, action_key, sig):
        return Response(
            content="<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#1e1e1e;color:#fff;'><h2>❌ Unauthorized</h2><p>Invalid or expired download link.</p></body></html>",
            media_type="text/html",
            status_code=401
        )

    is_online = store.is_subscriber_online(sid)
    store.queue_command(sid, url, title or url, ep or "latest")

    status_badge = (
        "<div style='background:#107c41;color:#fff;padding:8px 18px;border-radius:20px;display:inline-block;font-weight:bold;margin:15px 0;font-size:14px;'>🟢 PC is Online — Downloading Now!</div>"
        if is_online else
        "<div style='background:#0078d4;color:#fff;padding:8px 18px;border-radius:20px;display:inline-block;font-weight:bold;margin:15px 0;font-size:14px;'>💤 PC is Offline — Queued to start when PC turns on!</div>"
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Download Queued • Auto Episodes Downloader</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0b0f19; color: #f3f3f3; margin: 0; padding: 40px 20px; text-align: center; }}
        .card {{ background: #151d2a; max-width: 440px; margin: 30px auto; padding: 32px 24px; border-radius: 20px; box-shadow: 0 12px 40px rgba(0,0,0,0.6); border: 1px solid #233045; }}
        h1 {{ font-size: 20px; color: #4CC2FF; margin-top: 12px; margin-bottom: 8px; }}
        .anime {{ font-size: 18px; font-weight: bold; margin: 15px 0 4px; color: #ffffff; }}
        .ep {{ font-size: 15px; color: #7db0eb; margin-bottom: 12px; }}
        .note {{ font-size: 13px; color: #8899a6; line-height: 1.5; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="card">
        <div style="font-size: 48px;">📥</div>
        <h1>Download Queued!</h1>
        <div class="anime">{title or 'Anime Episode'}</div>
        <div class="ep">Episode {ep if ep else 'Release'}</div>
        {status_badge}
        <div class="note">Auto Episodes Downloader on your PC will automatically receive this task and download the episode.</div>
    </div>
</body>
</html>"""
    return Response(content=html, media_type="text/html")
