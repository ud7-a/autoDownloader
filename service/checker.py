"""Background worker that checks anime sources for new episodes and dispatches Discord notifications.

Deduplication principle: Anime rows are shared across all subscribers. The checker polls
each due anime once per cycle, finds its latest episode count, and fans out Discord webhooks
only to subscribers whose notified_max is behind the latest episode.
"""

import base64
from datetime import datetime, timezone
import json
import logging
import os
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
# "الحلقة-14" is unambiguous. It goes first for links, because the trailing number
# is not always the episode: animerco writes "...-الحلقة-14-الموسم-1/", where the
# trailing 1 is the season.
_ARABIC_EP_RE = re.compile(r"الحلقة[\s\-_]*(\d+)")
# animerco season pages are ".../seasons/<show>-season-N/" but their episode links
# are ".../episodes/<show>-الحلقة-X/" -- without "season-N" (season 1 especially).
_SEASON_SUFFIX_RE = re.compile(r"-season-\d+$", re.I)
_SEASON_LINK_RE = re.compile(r'''href=['"]([^'"]*/seasons/[^'"]+)['"]''', re.I)


def extract_episodes_from_html(html: str, base_url: str = "") -> list[int]:
    """Extracts all episode numbers found in page HTML links, JSON blocks, or onclick handlers."""
    episodes = set()

    # 0. Base64 encodedEpisodeData JSON array used by witanime
    for match in re.finditer(r'var\s+encodedEpisodeData\s*=\s*[\'"]([A-Za-z0-9+/=]+)[\'"]', html):
        try:
            raw_b64 = match.group(1)
            decoded_json = base64.b64decode(raw_b64).decode("utf-8", errors="ignore")
            data = json.loads(decoded_json)
            for item in data:
                num_str = str(item.get("number", "")).strip()
                if num_str.isdigit():
                    episodes.add(int(num_str))
        except Exception:
            pass

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
    href_pattern = re.compile(r'href=[\'"]([^\'"]*(?:episode|episodes|/watch/|الحلقة)[^\'"]*)[\'"]', re.I)
    
    slug = ""
    if base_url:
        parts = [p for p in base_url.split("?")[0].strip("/").split("/") if p]
        if parts:
            # Decoded, like the hrefs it is compared with below. Left encoded, an
            # Arabic slug ("%d9%81%d9%8a%d9%84%d9%85-...") never appeared in any
            # decoded link, so every episode was filtered out and the anime's
            # notifications stopped without an error. The "-season-N" of a season
            # page is dropped for the same reason: its episode links don't carry it.
            slug = _SEASON_SUFFIX_RE.sub("", unquote(parts[-1]).lower())
            
    for match in href_pattern.finditer(html):
        href = unquote(match.group(1))
        # Exclude common non-episode links
        if any(x in href.lower() for x in ("anime-genre", "anime-type", "anime-season", "tag", "category")):
            continue
        
        # Restrict to current anime's episodes (ignore sidebar links to other shows)
        if slug and slug not in href.lower():
            continue
            
        num_match = (_ARABIC_EP_RE.search(href) or _TRAILING_DIGIT_RE.search(href)
                     or _EP_NUMBER_RE.search(href))
        if num_match:
            episodes.add(int(num_match.group(1)))

    return sorted(episodes)


def latest_season_url(html: str, base_url: str) -> str:
    """On an animerco anime page, the URL of its newest season page, else "".

    animerco lists episodes on season pages only; the anime page -- which is what a
    Watchlist follow stores -- links seasons and no episodes, so it always read as
    0. Only this show's seasons count (the slug must appear in the link), so a
    sidebar link to some other show can never be picked up. The highest
    "season-N" wins; with no numbered season (an OVA-only show) the last one listed.
    """
    from urllib.parse import urljoin, urlparse
    if not html:
        return ""
    parts = [p for p in (base_url or "").split("?")[0].strip("/").split("/") if p]
    show = _SEASON_SUFFIX_RE.sub("", unquote(parts[-1]).lower()) if parts else ""
    links = []
    for m in _SEASON_LINK_RE.finditer(html):
        url = urljoin(base_url, m.group(1))
        path = unquote(urlparse(url).path).lower().rstrip("/")
        if path.endswith("/seasons") or (show and show not in path):
            continue
        if url not in links:
            links.append(url)
    if not links:
        return ""

    def number(u):
        m = re.search(r"season-(\d+)/?$", u, re.I)
        return int(m.group(1)) if m else -1

    numbered = [u for u in links if number(u) >= 0]
    return max(numbered, key=number) if numbered else links[-1]


def fetch_with_playwright(anime_url: str, timeout_seconds: float = 30.0,
                          seen: dict | None = None) -> tuple[int, dict]:
    """Uses headless Playwright Chromium to execute JavaScript, solve Cloudflare Turnstile challenges,
    and extract episode counts. `seen`, if given, records the status and page for diagnostics.
    """
    debug_info = {}
    seen = {} if seen is None else seen
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
                viewport={"width": 1366, "height": 768},
                locale="en-US,ar",
            )
            try:
                from playwright_stealth import stealth_sync
                page = context.new_page()
                stealth_sync(page)
            except Exception:
                page = context.new_page()

            response = page.goto(anime_url, timeout=int(timeout_seconds * 1000), wait_until="domcontentloaded")
            if response is not None:
                seen["status"] = response.status

            # Wait up to 8 seconds for Cloudflare challenge redirect or episode container to appear
            for _ in range(8):
                page.wait_for_timeout(1000)
                if "just a moment" not in page.title().lower():
                    break

            html = page.content()
            seen["html"] = html
            debug_info["title"] = page.title()
            debug_info["html_len"] = len(html)
            debug_info["preview"] = html[:200]
            browser.close()

            eps = extract_episodes_from_html(html, anime_url)
            debug_info["episodes"] = eps
            return (max(eps) if eps else 0), debug_info
    except Exception as e:
        logger.warning(f"Playwright fetch failed for {anime_url}: {e}")
        debug_info["error"] = str(e)
        seen["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return 0, debug_info


_PROXY_CACHE = []
_PROXY_CACHE_TIME = 0.0


def get_fresh_proxies() -> list[str]:
    """Fetches a list of open elite HTTP proxies."""
    global _PROXY_CACHE, _PROXY_CACHE_TIME
    now = time.time()
    if _PROXY_CACHE and (now - _PROXY_CACHE_TIME < 600):
        return list(_PROXY_CACHE)

    urls = [
        "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=3000&country=all&ssl=all&anonymity=elite",
        "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    ]
    proxies = []
    for u in urls:
        try:
            r = httpx.get(u, timeout=5.0)
            if r.status_code == 200:
                for line in r.text.splitlines():
                    p = line.strip()
                    if p and ":" in p and len(p.split(":")) == 2:
                        proxies.append(p)
                if proxies:
                    break
        except Exception:
            pass

    if proxies:
        _PROXY_CACHE = proxies
        _PROXY_CACHE_TIME = now
    return _PROXY_CACHE


def fetch_with_proxy_rotation(anime_url: str, max_attempts: int = 6,
                              seen: dict | None = None) -> int:
    """Tries fetching through rotating anonymous proxies to bypass Cloudflare datacenter IP blocks.
    `seen`, if given, records attempts and outcomes for diagnostics."""
    seen = {} if seen is None else seen
    proxies = get_fresh_proxies()
    seen["proxies_available"] = len(proxies)
    if not proxies:
        return 0

    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }
    import random
    selected = random.sample(proxies, min(len(proxies), max_attempts * 2))

    outcomes = seen.setdefault("attempts", {})
    for p in selected:
        try:
            proxy_url = f"http://{p}"
            with httpx.Client(proxy=proxy_url, follow_redirects=True, timeout=7.0, verify=False) as client:
                r = client.get(anime_url, headers=headers)
                outcomes[str(r.status_code)] = outcomes.get(str(r.status_code), 0) + 1
                if r.status_code == 200 and "just a moment" not in r.text.lower():
                    seen["status"], seen["html"] = 200, r.text
                    eps = extract_episodes_from_html(r.text, anime_url)
                    if eps:
                        return max(eps)
        except Exception as e:
            key = type(e).__name__
            outcomes[key] = outcomes.get(key, 0) + 1
            continue
    return 0


def fetch_with_scraperapi(anime_url: str, api_key: str, seen: dict | None = None) -> int:
    """Fetches anime page via ScraperAPI residential proxy to bypass Cloudflare Turnstile.
    `seen`, if given, records the status and page for diagnostics -- never the key."""
    seen = {} if seen is None else seen
    try:
        endpoint = "http://api.scraperapi.com"
        params = {
            "api_key": api_key,
            "url": anime_url,
        }
        with httpx.Client(timeout=25.0) as client:
            r = client.get(endpoint, params=params)
            seen["status"], seen["html"] = r.status_code, r.text
            if r.status_code == 200 and "just a moment" not in r.text.lower():
                eps = extract_episodes_from_html(r.text, anime_url)
                if eps:
                    return max(eps)
            elif r.status_code != 200:
                # ScraperAPI explains its own refusals (credits used up, bad key,
                # rate limit) in a short plain-text body. Kept short; it never
                # contains the key.
                seen["error"] = r.text[:160].replace(api_key, "***")
    except Exception as e:
        # Type only, in the log too: an httpx error can quote the request URL, and
        # that URL carries the API key -- the full message put it in Render's logs.
        logger.warning(f"ScraperAPI fetch failed for {anime_url}: {type(e).__name__}")
        seen["error"] = type(e).__name__
    return 0


def _fetch_curl_cffi(anime_url: str, headers: dict, seen: dict) -> int:
    from curl_cffi import requests as cffi_requests
    r = cffi_requests.get(anime_url, headers=headers, impersonate="chrome124", timeout=15.0)
    seen["status"], seen["html"] = r.status_code, r.text
    if r.status_code == 200:
        eps = extract_episodes_from_html(r.text, anime_url)
        if eps:
            return max(eps)
    return 0


def _fetch_httpx(anime_url: str, headers: dict, client: httpx.Client | None, seen: dict) -> int:
    close_client = client is None
    if close_client:
        client = httpx.Client(follow_redirects=True, timeout=15.0, verify=False)
    try:
        r = client.get(anime_url, headers=headers)
        seen["status"], seen["html"] = r.status_code, r.text
        if r.status_code == 200:
            eps = extract_episodes_from_html(r.text, anime_url)
            if eps:
                return max(eps)
        return 0
    finally:
        if close_client:
            client.close()


# Fetchers whose 404 can be believed. A random open proxy answering 404 says nothing
# about the site, so the proxy pool's answers never end the search.
_TRUSTED_404 = {"scraperapi", "curl_cffi", "httpx", "playwright"}


def fetch_latest_episode(anime_url: str, client: httpx.Client | None = None,
                         trace: list | None = None, _depth: int = 0) -> int:
    """Fetches anime page and returns the highest episode number detected (0 if none found).

    The fetchers are tried in order until one finds episodes. Before, a page that
    downloaded fine but held no episode links counted as a failure, so the search
    ran through every fetcher -- about a minute per anime per cycle, delaying every
    notification behind it -- and still returned 0. Two cases now end it early:

      * a trusted 404: the address is wrong, and asking again elsewhere won't help;
      * a genuine page listing this show's seasons (animerco's anime page): the
        episodes are on the newest season page, which is read next, once.

    `trace`, if given, receives one entry per step (fetcher, status, size, whether
    it was a Cloudflare challenge, episodes found, error) -- what /v1/test_scrape
    reports, so a zero can be told apart from a block.
    """
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    }

    steps = []
    # 0. Try ScraperAPI if SCRAPER_API_KEY is configured (100% residential Cloudflare bypass)
    scraper_key = os.environ.get("SCRAPER_API_KEY", "").strip()
    if scraper_key:
        steps.append(("scraperapi", lambda seen: fetch_with_scraperapi(anime_url, scraper_key, seen=seen)))
    # 1. curl_cffi first (fastest)   2. standard httpx
    steps.append(("curl_cffi", lambda seen: _fetch_curl_cffi(anime_url, headers, seen)))
    steps.append(("httpx", lambda seen: _fetch_httpx(anime_url, headers, client, seen)))
    # 3. rotating proxy pool   4. headless Playwright Chromium
    steps.append(("proxy_rotation", lambda seen: fetch_with_proxy_rotation(anime_url, seen=seen)))
    steps.append(("playwright", lambda seen: fetch_with_playwright(anime_url, seen=seen)[0]))

    genuine_page = ""
    not_found = False
    for name, run in steps:
        seen = {}
        started = time.time()
        try:
            max_ep = run(seen)
        except Exception as e:
            logger.debug(f"{name} fetch failed: {e}")
            max_ep = 0
            seen.setdefault("error", type(e).__name__)

        status, html = seen.get("status"), seen.get("html") or ""
        challenge = "just a moment" in html.lower()
        if status == 200 and html and not challenge:
            genuine_page = html
        if status == 404 and name in _TRUSTED_404:
            not_found = True
        if trace is not None:
            entry = {"url": anime_url, "fetcher": name, "seconds": round(time.time() - started, 1),
                     "status": status, "bytes": len(html), "challenge": challenge,
                     "max_episode": max_ep}
            for key in ("error", "attempts", "proxies_available"):
                if key in seen:
                    entry[key] = seen[key]
            trace.append(entry)

        if max_ep > 0:
            return max_ep
        if not_found or latest_season_url(genuine_page, anime_url):
            break

    if not_found:
        return 0
    season = latest_season_url(genuine_page, anime_url) if _depth == 0 else ""
    if season:
        if trace is not None:
            trace.append({"url": anime_url, "following_season": season})
        return fetch_latest_episode(season, client=client, trace=trace, _depth=1)
    return 0


def create_discord_embed(
    anime_title: str,
    anime_url: str,
    episode_num: int,
    subscriber_id: str = "",
    is_online: bool = False
) -> dict:
    """Builds a rich Discord Embed payload for the episode release notification."""
    now_iso = datetime.now(timezone.utc).isoformat()
    fields = [
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
    ]

    if subscriber_id:
        from service import crypto
        from urllib.parse import quote
        action_key = f"{anime_url}:{episode_num}"
        sig = crypto.sign_action(subscriber_id, action_key)
        cloud_base = os.environ.get("AED_CLOUD_URL") or "https://aed-notification-service.onrender.com"
        queue_url = f"{cloud_base}/v1/queue?sid={subscriber_id}&url={quote(anime_url)}&title={quote(anime_title)}&ep={episode_num}&sig={sig}"
        button_label = "📥 Start Downloading on PC (Online 🟢)" if is_online else "📥 Download when PC Turns On (Offline 💤)"
        fields.append({
            "name": "Remote Action",
            "value": f"[{button_label}]({queue_url})",
            "inline": False
        })

    return {
        "embeds": [
            {
                "title": "🔔 New Episode Released!",
                "description": f"**{anime_title}**\n**Episode {episode_num}** is now available to watch and download.",
                "url": anime_url,
                "color": 0x4CC2FF,  # Fluent Cyan Blue
                "fields": fields,
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
            db.execute("UPDATE follows SET notified_max = %s WHERE anime_url = %s AND notified_max = 0",
                       (current_max, anime_url))
        return 0

    if current_max > last_seen_max:
        # New episode(s) released!
        for ep_num in range(last_seen_max + 1, current_max + 1):
            to_notify = store.subscribers_to_notify(anime_url, ep_num)
            if to_notify:
                for sid, webhook_url, _prev_notif in to_notify:
                    is_online = store.is_subscriber_online(sid)
                    embed_payload = create_discord_embed(anime_title, anime_url, ep_num, subscriber_id=sid, is_online=is_online)
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
            for sid, webhook_url, _prev_notif in to_notify_behind:
                is_online = store.is_subscriber_online(sid)
                embed_payload = create_discord_embed(anime_title, anime_url, current_max, subscriber_id=sid, is_online=is_online)
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
    """Returns [today, yesterday, tomorrow] in canonical format to account for timezone offsets across the globe."""
    days = ["saturday", "sunday", "monday", "tuesday", "wednesday", "thursday", "friday"]
    idx = (time.gmtime().tm_wday + 2) % 7
    today = days[idx]
    yesterday = days[(idx - 1) % 7]
    tomorrow = days[(idx + 1) % 7]
    return [today, yesterday, tomorrow]


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
