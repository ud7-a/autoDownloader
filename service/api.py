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
    store.record_heartbeat(sid)
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
    store.record_heartbeat(subscriber_id)
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


class QueueSubmitRequest(BaseModel):
    sid: str
    url: str
    title: str = ""
    ep: str = ""
    sig: str = ""


@app.post("/v1/queue/submit")
def submit_remote_download(body: QueueSubmitRequest):
    """Interactive AJAX endpoint to queue or re-trigger a remote download."""
    action_key = f"{body.url}:{body.ep}"
    # Verify signature against original ep or bare url
    if not (crypto.verify_action(body.sid, action_key, body.sig) or crypto.verify_action(body.sid, f"{body.url}:", body.sig)):
        raise HTTPException(status_code=401, detail="Invalid action signature")

    cmd_id = store.queue_command(body.sid, body.url, body.title or body.url, body.ep or "latest")
    is_online = store.is_subscriber_online(body.sid)
    return {
        "status": "success",
        "command_id": cmd_id,
        "online": is_online,
        "message": "PC is Online — Downloading Now!" if is_online else "PC is Offline — Queued for next boot!"
    }


@app.get("/v1/queue", response_class=Response)
def queue_remote_download(sid: str, url: str, title: str = "", ep: str = "", sig: str = ""):
    """Interactive web application triggered from Discord to manage and send downloads to PC."""
    action_key = f"{url}:{ep}"
    if not (crypto.verify_action(sid, action_key, sig) or crypto.verify_action(sid, f"{url}:", sig)):
        return Response(
            content="<html><body style='font-family:sans-serif;text-align:center;padding:50px;background:#1e1e1e;color:#fff;'><h2>❌ Unauthorized</h2><p>Invalid or expired download link.</p></body></html>",
            media_type="text/html",
            status_code=401
        )

    # Initial auto-queue on first visit
    store.queue_command(sid, url, title or url, ep or "latest")
    is_online = store.is_subscriber_online(sid)

    status_badge_class = "badge-online" if is_online else "badge-offline"
    status_text = "🟢 PC is Online — Downloading Now!" if is_online else "💤 PC is Offline — Queued for PC Boot"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title or 'Anime'} • Auto Episodes Downloader</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0b0f19;
            color: #f3f3f3;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .card {{
            background: #151d2a;
            width: 100%;
            max-width: 440px;
            padding: 32px 24px;
            border-radius: 24px;
            box-shadow: 0 16px 48px rgba(0,0,0,0.6);
            border: 1px solid #233045;
            text-align: center;
            animation: fadeIn 0.4s ease-out;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(12px); }}
            to {{ opacity: 1; transform: translateY(0); }}
        }}
        .icon {{ font-size: 50px; margin-bottom: 8px; }}
        h1 {{ font-size: 20px; color: #4CC2FF; margin-bottom: 6px; font-weight: 700; }}
        .anime-title {{ font-size: 17px; font-weight: 600; color: #ffffff; margin: 12px 0 4px; line-height: 1.4; }}
        .ep-badge {{ font-size: 14px; color: #7db0eb; margin-bottom: 16px; }}
        
        .status-badge {{
            display: inline-block;
            padding: 8px 18px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 13px;
            margin: 10px 0 20px;
            transition: all 0.3s;
        }}
        .badge-online {{ background: rgba(16, 124, 65, 0.25); color: #3cd47b; border: 1px solid #107c41; }}
        .badge-offline {{ background: rgba(0, 120, 212, 0.25); color: #69b8ff; border: 1px solid #0078d4; }}

        .control-group {{
            background: #0f1522;
            padding: 20px;
            border-radius: 16px;
            border: 1px solid #1f2a3c;
            margin: 15px 0;
            text-align: center;
        }}
        .ep-box {{
            background: #172133;
            border: 1px solid #2d3e58;
            border-radius: 12px;
            padding: 12px 16px;
            margin-bottom: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .ep-box-label {{ font-size: 13px; color: #8bb4e7; font-weight: 600; }}
        .ep-box-val {{ font-size: 16px; color: #4CC2FF; font-weight: 700; }}

        .btn-action {{
            width: 100%;
            background: #0078d4;
            color: #ffffff;
            border: none;
            border-radius: 14px;
            padding: 15px 20px;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s;
            box-shadow: 0 4px 16px rgba(0, 120, 212, 0.35);
        }}
        .btn-action:hover {{ background: #1a88e0; transform: translateY(-1px); box-shadow: 0 6px 20px rgba(0, 120, 212, 0.5); }}
        .btn-action:active {{ transform: translateY(1px); }}
        .btn-action:disabled {{ background: #334255; cursor: not-allowed; box-shadow: none; }}

        .feedback-box {{
            margin-top: 16px;
            padding: 12px;
            border-radius: 12px;
            font-size: 13px;
            display: none;
            line-height: 1.4;
        }}
        .feedback-success {{ background: rgba(16, 124, 65, 0.2); color: #4ade80; border: 1px solid #107c41; }}
        .feedback-error {{ background: rgba(220, 38, 38, 0.2); color: #f87171; border: 1px solid #dc2626; }}

        .note {{ font-size: 12px; color: #738499; margin-top: 22px; line-height: 1.5; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">📥</div>
        <h1>Download Manager</h1>
        <div class="anime-title">{title or 'Anime Episode'}</div>
        <div class="ep-badge">{title or 'Anime'}</div>
        
        <div id="status-badge" class="status-badge {status_badge_class}">{status_text}</div>

        <div class="control-group">
            <div class="ep-box">
                <span class="ep-box-label">Target Episode</span>
                <span class="ep-box-val">Episode {ep if ep else 'Latest'}</span>
            </div>
            
            <button id="btn-submit" class="btn-action" onclick="triggerDownload()">
                🚀 Start Download on PC
            </button>
        </div>

        <div id="feedback" class="feedback-box feedback-success">
            ✅ <b>Download Queued!</b> Your PC is receiving the command.
        </div>

        <div class="note">
            Auto Episodes Downloader on your PC will automatically receive this task and download the episode.
        </div>
    </div>

    <script>
        async function triggerDownload() {{
            const btn = document.getElementById('btn-submit');
            const feedback = document.getElementById('feedback');
            
            btn.disabled = true;
            btn.innerText = '⏳ Sending to PC...';
            feedback.style.display = 'none';

            try {{
                const res = await fetch('/v1/queue/submit', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        sid: '{sid}',
                        url: '{url}',
                        title: '{title}',
                        ep: '{ep}',
                        sig: '{sig}'
                    }})
                }});

                const data = await res.json();
                if (res.ok) {{
                    feedback.className = 'feedback-box feedback-success';
                    feedback.innerHTML = '✅ <b>Success!</b> ' + (data.message || 'Download queued for PC.');
                    feedback.style.display = 'block';

                    const badge = document.getElementById('status-badge');
                    if (data.online) {{
                        badge.className = 'status-badge badge-online';
                        badge.innerText = '🟢 PC is Online — Downloading Now!';
                    }} else {{
                        badge.className = 'status-badge badge-offline';
                        badge.innerText = '💤 PC is Offline — Queued for PC Boot';
                    }}
                }} else {{
                    feedback.className = 'feedback-box feedback-error';
                    feedback.innerHTML = '❌ ' + (data.detail || 'Could not queue download.');
                    feedback.style.display = 'block';
                }}
            }} catch (err) {{
                feedback.className = 'feedback-box feedback-error';
                feedback.innerHTML = '❌ Network error connecting to cloud server.';
                feedback.style.display = 'block';
            }} finally {{
                btn.disabled = false;
                btn.innerText = '🚀 Start Download on PC';
            }}
        }}
    </script>
</body>
</html>"""
    return Response(content=html, media_type="text/html")
