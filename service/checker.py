"""Background worker that checks anime sources for new episodes and dispatches Discord notifications.

Deduplication principle: Anime rows are shared across all subscribers. The checker polls
each due anime once per cycle, finds its latest episode count, and fans out Discord webhooks
only to subscribers whose notified_max is behind the latest episode.
"""

import base64
from datetime import datetime, timezone
import logging
import re
import time
from urllib.parse import unquote

import httpx

from service import store

logger = logging.getLogger("aed_checker")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

# Extract episode number from url or anchor text
_EP_NUMBER_RE = re.compile(r"(?:الحلقة|episode|ep|hd)[\s\-_]*(\d+)", re.I)
_TRAILING_DIGIT_RE = re.compile(r"[-_/](\d+)/?$")


def extract_episodes_from_html(html: str, base_url: str = "") -> list[int]:
    """Extracts all episode numbers found in page HTML links or onclick handlers."""
    episodes = set()

    # 1. Base64 openEpisode('...') handlers used by witanime
    for match in re.finditer(r"openEpisode\(['\"]([A-Za-z0-9+/=]+)['\"]\)", html):
        try:
            raw_decoded = base64.b64decode(match.group(1)).decode("utf-8", errors="ignore")
            decoded = unquote(raw_decoded)
            num_match = _TRAILING_DIGIT_RE.search(decoded) or _EP_NUMBER_RE.search(decoded)
            if num_match:
                episodes.add(int(num_match.group(1)))
        except Exception:
            pass

    # 2. Direct href episode links
    # Match href="..." with /episode/, /episodes/, /watch/, /الحلقة/
    href_pattern = re.compile(r'href=[\'"]([^\'"]*(?:episode|episodes|الحلقة)[^\'"]*)[\'"]', re.I)
    for match in href_pattern.finditer(html):
        href = unquote(match.group(1))
        # Exclude common non-episode links
        if any(x in href.lower() for x in ("anime-genre", "anime-type", "anime-season", "tag", "category")):
            continue
        num_match = _TRAILING_DIGIT_RE.search(href) or _EP_NUMBER_RE.search(href)
        if num_match:
            episodes.add(int(num_match.group(1)))

    return sorted(episodes)


def fetch_with_playwright(anime_url: str, timeout_seconds: float = 25.0) -> int:
    """Uses headless Playwright Chromium to execute JavaScript, solve Cloudflare Turnstile challenges,
    and extract episode counts.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US,ar",
            )
            try:
                from playwright_stealth import stealth_sync
                page = context.new_page()
                stealth_sync(page)
            except Exception:
                page = context.new_page()

            page.goto(anime_url, timeout=int(timeout_seconds * 1000), wait_until="domcontentloaded")
            # Wait a few seconds for Cloudflare challenge redirect or openEpisode anchors to render
            page.wait_for_timeout(3500)
            html = page.content()
            browser.close()

            eps = extract_episodes_from_html(html, anime_url)
            return max(eps) if eps else 0
    except Exception as e:
        logger.warning(f"Playwright fetch failed for {anime_url}: {e}")
        return 0


def fetch_latest_episode(anime_url: str, client: httpx.Client | None = None) -> int:
    """Fetches anime page and returns the highest episode number detected (0 if none found)."""
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }

    # 1. Try curl_cffi first (fastest)
    try:
        from curl_cffi import requests as cffi_requests
        r = cffi_requests.get(anime_url, headers=headers, impersonate="chrome124", timeout=15.0)
        if r.status_code == 200:
            eps = extract_episodes_from_html(r.text, anime_url)
            if eps:
                return max(eps)
    except Exception as e:
        logger.debug(f"curl_cffi fetch attempt failed: {e}")

    # 2. Try standard httpx
    close_client = False
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=15.0, verify=False)
        close_client = True

    try:
        r = client.get(anime_url, headers=headers)
        if r.status_code == 200:
            eps = extract_episodes_from_html(r.text, anime_url)
            if eps:
                return max(eps)
    except Exception as e:
        logger.debug(f"httpx fetch failed: {e}")
    finally:
        if close_client:
            client.close()

    # 3. Fallback to Headless Playwright Chromium to solve Cloudflare Turnstile JS challenges
    return fetch_with_playwright(anime_url)


def create_discord_embed(anime_title: str, anime_url: str, episode_num: int) -> dict:
    """Builds a rich Discord Embed payload for the episode release notification."""
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        "embeds": [
            {
                "title": "🔔 New Episode Released!",
                "description": f"**{anime_title}**\n**Episode {episode_num}** is now available to watch and download.",
                "url": anime_url,
                "color": 0x4CC2FF,  # Fluent Cyan Blue
                "fields": [
                    {
                        "name": "Anime",
                        "value": f"[{anime_title}]({anime_url})",
                        "inline": True
                    },
                    {
                        "name": "Episode",
                        "value": f"`Episode {episode_num}`",
                        "inline": True
                    }
                ],
                "footer": {
                    "text": "Auto Episodes Downloader • Cloud Service"
                },
                "timestamp": now_iso
            }
        ]
    }


def send_discord_notification(webhook_url: str, payload: dict, client: httpx.Client | None = None) -> bool:
    """Sends webhook payload to Discord with retry on HTTP 429 rate limits."""
    close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        close_client = True

    try:
        for attempt in range(3):
            r = client.post(webhook_url, json=payload)
            if r.status_code in (200, 204):
                return True
            elif r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", 2.0))
                logger.warning(f"Discord rate limit hit; backing off for {retry_after}s...")
                time.sleep(min(retry_after, 5.0))
            else:
                logger.error(f"Discord webhook failed with HTTP {r.status_code}: {r.text}")
                return False
        return False
    except Exception as e:
        logger.error(f"Failed to post to Discord webhook: {e}")
        return False
    finally:
        if close_client:
            client.close()


def process_anime(anime: dict, client: httpx.Client | None = None) -> int:
    """Checks one anime and notifies followers if new episodes are released.

    Returns the number of notifications successfully sent.
    """
    anime_url = anime["url"]
    anime_title = anime.get("title") or anime_url
    last_seen_max = anime.get("last_seen_max", 0)

    current_max = fetch_latest_episode(anime_url, client=client)
    if current_max <= 0:
        # Page failed or no episodes detected; update check timestamp without altering max
        store.update_anime_progress(anime_url, last_seen_max)
        return 0

    notifications_sent = 0

    if last_seen_max == 0:
        # First discovery of this anime on the server:
        # Seed last_seen_max and advance any unseeded (0-notified) followers to current_max
        # to establish the baseline without spamming back-catalogues
        store.update_anime_progress(anime_url, current_max)
        with store.get_db() as db:
            db.execute("UPDATE follows SET notified_max = ? WHERE anime_url = ? AND notified_max = 0",
                       (current_max, anime_url))
        return 0

    if current_max > last_seen_max:
        # New episode(s) released!
        for ep_num in range(last_seen_max + 1, current_max + 1):
            to_notify = store.subscribers_to_notify(anime_url, ep_num)
            if to_notify:
                embed_payload = create_discord_embed(anime_title, anime_url, ep_num)
                for sid, webhook_url, _prev_notif in to_notify:
                    ok = send_discord_notification(webhook_url, embed_payload, client=client)
                    if ok:
                        store.advance_notified_max(sid, anime_url, ep_num)
                        notifications_sent += 1
                    time.sleep(0.1)  # small pause to avoid Discord webhook rate spikes

        store.update_anime_progress(anime_url, current_max)
    else:
        # Check if any subscriber is behind last_seen_max (e.g. joined recently with seen_max < current_max)
        to_notify_behind = store.subscribers_to_notify(anime_url, current_max)
        if to_notify_behind:
            embed_payload = create_discord_embed(anime_title, anime_url, current_max)
            for sid, webhook_url, _prev_notif in to_notify_behind:
                ok = send_discord_notification(webhook_url, embed_payload, client=client)
                if ok:
                    store.advance_notified_max(sid, anime_url, current_max)
                    notifications_sent += 1
                time.sleep(0.1)
        store.update_anime_progress(anime_url, last_seen_max)

    return notifications_sent


def today_key() -> str:
    """Current day of week in canonical format: saturday, sunday, monday, etc."""
    days = ["saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday"]
    # Python tm_wday: Monday=0, Tuesday=1, ... Saturday=5, Sunday=6
    # (tm_wday + 2) % 7 -> Monday is index 2, Saturday is index 0.
    return days[(time.gmtime().tm_wday + 2) % 7]


def active_day_keys() -> list[str]:
    """Returns [today, yesterday] in canonical format to account for timezone offsets."""
    days = ["saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday"]
    idx = (time.gmtime().tm_wday + 2) % 7
    today = days[idx]
    yesterday = days[(idx - 1) % 7]
    return [today, yesterday]


def run_checker_cycle(batch_limit: int = 50, client: httpx.Client | None = None, today_day: str = None) -> dict:
    """Runs a single pass over anime due for checking today. Returns statistics."""
    if today_day is not None:
        days_to_check = [today_day]
        current_day = today_day
    else:
        days_to_check = active_day_keys()
        current_day = days_to_check[0]

    due = store.due_anime(today_days=days_to_check, limit=batch_limit)
    total_notifications = 0
    errors = 0

    for anime in due:
        try:
            total_notifications += process_anime(anime, client=client)
            time.sleep(1.0)  # Gentle delay between source site requests to avoid IP bans
        except Exception as e:
            logger.error(f"Error processing anime {anime.get('url')}: {e}")
            errors += 1

    return {
        "day": current_day,
        "checked": len(due),
        "notifications_sent": total_notifications,
        "errors": errors
    }


def start_checker_loop(interval_seconds: int = 900, stop_event=None) -> None:
    """Continuously runs the checker loop with interval_seconds sleep between cycles."""
    logger.info(f"Starting cloud checker loop (interval={interval_seconds}s)...")
    while stop_event is None or not stop_event.is_set():
        try:
            stats = run_checker_cycle()
            logger.info(f"Checker cycle completed: {stats}")
        except Exception as e:
            logger.error(f"Unhandled error in checker cycle: {e}")

        # Sleep in small increments to respond quickly to stop_event
        sleep_elapsed = 0
        while sleep_elapsed < interval_seconds:
            if stop_event and stop_event.is_set():
                break
            time.sleep(1)
            sleep_elapsed += 1
