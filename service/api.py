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
    max_ep, debug_info = checker.fetch_with_playwright(url)
    return {
        "url": url,
        "max_episode": max_ep,
        "debug": debug_info,
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
