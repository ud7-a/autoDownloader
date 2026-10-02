"""The app's own "new episode" Discord message. Standard library only.

This used to borrow create_discord_embed/send_discord_notification from the cloud
service (service.checker). The installed app bundles that module but not what it
imports on its first lines -- httpx, and the Postgres driver via service.store --
because the release build installs the app's requirements, not the server's. So
the import failed, inside a Qt callback, and the app was killed (0xC0000409) every
time a Watchlist check found a new episode with a Discord webhook set. From source
it worked: a developer machine has the server's packages too. Nothing in the
desktop app may import from service/ (a test enforces it).
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Discord asks API clients to identify themselves in this form.
_USER_AGENT = "DiscordBot (https://github.com/ud7-a/autoDownloader, 1)"


def release_embed(anime_title, anime_url, episode_num):
    """The "New Episode Released!" message, laid out like the cloud service's."""
    return {
        "embeds": [{
            "title": "🔔 New Episode Released!",
            "description": f"**{anime_title}**\n**Episode {episode_num}** is now available "
                           f"to watch and download.",
            "url": anime_url,
            "color": 0x4CC2FF,
            "fields": [
                {"name": "Anime", "value": f"[{anime_title}]({anime_url})", "inline": True},
                {"name": "Episode", "value": f"`Episode {episode_num}`", "inline": True},
            ],
            "footer": {"text": "Auto Episodes Downloader"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }


def send(webhook_url, payload, timeout=10, attempts=3):
    """POST a payload to a Discord webhook. True on success. Never raises.

    Backs off on Discord's 429 rate limit, up to `attempts` tries.
    """
    if not webhook_url:
        return False
    body = json.dumps(payload).encode("utf-8")
    for _ in range(attempts):
        req = urllib.request.Request(
            webhook_url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return False
            try:
                wait = float(e.headers.get("Retry-After", "2"))
            except (TypeError, ValueError):
                wait = 2.0
            time.sleep(min(max(wait, 0.5), 5.0))
        except Exception:
            return False
    return False
