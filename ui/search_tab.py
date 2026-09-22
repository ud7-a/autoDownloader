import os
import re
import sys
import copy
import time
import base64
import tempfile
import threading
from collections import defaultdict
from urllib.parse import urlparse, quote, unquote

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel)
from qfluentwidgets import (LineEdit, PrimaryPushButton, ComboBox, ToolButton,
                            SimpleCardWidget, SmoothScrollArea, FluentIcon as FIF,
                            InfoBar, InfoBarPosition, IndeterminateProgressRing)

from utils.config import app_settings, sites_data, save_config, config_lock
from core.site_health import (site_key, lookup as site_lookup, record_landing,
                              record_detection, broken_sites)
from ui.styles import rounded_pixmap

# Fixed list of supported websites (domain -> search URL template). The Search tab
# only allows these; users cannot add arbitrary sites.
SUPPORTED_SITES = {
    "witanime.site": "https://witanime.site/search?q={query}",
    "eta.animerco.org": "https://eta.animerco.org/?s={query}",
}

# Built-in download click-flows for supported sites, used when a freshly created
# search profile has no existing same-domain profile to inherit steps from. Without
# this a new profile would have empty step_paths and couldn't download.
DEFAULT_SITE_FLOWS = {
    # Taken from a hand-tuned working profile ("template witanime.json"). Order is
    # the fallback order: Mediafire first, then Google Drive, then witanime's own
    # wtsrv mirror, then Workupload, then gofile.
    "witanime.site": {
        "next_btn_xpath": "الحلقة التالية",
        "step_paths": {
            "FHD - Mediafire": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mediafire')]", "delay": 5.0},
                {"xpath": '//*[@id="downloadButton"]', "delay": 2.0},
            ],
            "FHD - Google Drive": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 2.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'google')]", "delay": 3.0},
                {"xpath": "Download anyway", "delay": 2.0},
            ],
            "FHD - wtsrv": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'wtsrv')]", "delay": 5.0},
                {"xpath": '//*[@id="downloadButton"]', "delay": 2.0},
            ],
            "FHD - Workupload": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 2.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'workupload')]", "delay": 5.0},
                {"xpath": '//*[@id="file"]/div[3]/div/a', "delay": 2.0},
            ],
            # gofile opens in the same tab (no popup) on a folder page holding the
            # episode; its Download button is the only [data-action=download] there.
            "FHD - gofile": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'gofile')]", "delay": 5.0},
                {"xpath": "//button[@data-action='download']", "delay": 2.0},
            ],
        },
    },
    # animerco shows downloads as a table (رابط/خادم/جودة/لغة); each row's "تحميل"
    # button opens a /links/<id> redirect that lands directly on the host. We target
    # the Google Drive row by its favicon domain; the engine then rewrites the Drive
    # file-preview page to a direct download so the "Download anyway" confirm appears.
    "eta.animerco.org": {
        "next_btn_xpath": "الحلقة التالية",
        "step_paths": {
            "google drive": [
                {"xpath": "//tr[.//div[contains(@data-src,'drive.google')]]//a[contains(@class,'labeled')]",
                 "delay": 5.0},
                {"xpath": "Download anyway", "delay": 3.0},
            ],
            # Fallback: episodes that also offer MediaFire. The engine tries this
            # path only if the Google Drive one above didn't find its row.
            "mediafire": [
                {"xpath": "//tr[.//div[contains(@data-src,'mediafire')]]//a[contains(@class,'labeled')]",
                 "delay": 5.0},
                {"xpath": '//*[@id="downloadButton"]', "delay": 3.0},
            ],
        },
    },
}


def extract_domain(url):
    """Return the bare host (no scheme, no leading www.) for a URL or domain string."""
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def site_display_name(domain):
    """Domain -> the site's name alone: "eta.animerco.org" becomes "animerco".

    The dropdown and the watchlist cards show this instead of the raw host. Users
    pick a website by its name; the TLD and the subdomain in front of it are
    plumbing that only makes the entries harder to tell apart at a glance.

    Same function the site tables are keyed through (site_health.site_key), on
    purpose: a second implementation of "which site is this" is how a subdomain
    move ends up fixed in one place and still broken in another.
    """
    return site_key(domain)


def clean_title(title):
    """A result title as a person would write it: HTML entities decoded, spaces tidied.

    Titles scraped from page markup can arrive still encoded -- witanime's live
    search page carries "I&#039;ll" -- and from the card they flow into profile
    names, download folder names and the Watchlist, and break schedule matching,
    which compares plain titles. Applied to every site's results.
    """
    import html
    text = html.unescape(title or "")
    return re.sub(r"\s+", " ", text).strip()


def is_movie_link(url):
    """True if a search result links to a movie rather than a series.

    Both sites say so in the path: witanime uses /movie/<slug>, animerco
    /movies/<slug> (a mixed "slime" page came back as three /animes/ and one
    /movies/). Whole path segments only, so a series whose slug merely contains
    "movie" is not flagged.
    """
    try:
        segments = urlparse(url or "").path.lower().split("/")
    except Exception:
        return False
    return "movie" in segments or "movies" in segments


def site_icon_path(domain, must_exist=True):
    """Path to a supported site's bundled favicon, or "" when it has none.

    Icons ship as assets rather than being fetched: the sites reset a plain Python
    HTTPS request, and the dropdown is built on the startup path where nothing may
    touch the network. tools/fetch_site_icons.py refreshes them through Chrome.
    """
    host = extract_domain(domain)
    if not host:
        return ""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icons = os.path.join(base, "assets", "site_icons")
    path = os.path.join(icons, f"{host}.png")
    if not must_exist or os.path.exists(path):
        return path
    # A site that moves subdomain keeps its icon: animerco now redirects
    # eta.animerco.org -> det.animerco.org, and a watchlist entry that recorded the
    # landing host would otherwise show a generic globe. Assets stay keyed by exact
    # host (that is what the fetch tool writes), so match on the name instead.
    key = site_key(host)
    for supported in SUPPORTED_SITES:
        if supported != host and site_key(supported) == key:
            alt = os.path.join(icons, f"{supported}.png")
            if os.path.exists(alt):
                return alt
    return ""


# Both favicons are hard-edged squares of solid colour, which read as stickers next
# to the app's rounded cards and buttons. The corners are softened when the icon is
# built rather than baked into the asset, so the file stays the site's real favicon
# and the radius can change without re-fetching anything. Rendered at 64px and
# scaled down by Qt, so the rounding scales with it and stays sharp on a 200% display.
SITE_ICON_PX = 64
SITE_ICON_RADIUS = 12


def site_icon(domain):
    """QIcon for a supported site: its favicon with rounded corners, else a globe."""
    path = site_icon_path(domain)
    if path:
        pix = rounded_pixmap(path, SITE_ICON_PX, SITE_ICON_PX, SITE_ICON_RADIUS)
        if pix is not None:
            return QIcon(pix)
    return FIF.GLOBE.icon()


def friendly_browser_error(message, domain=""):
    """Turn a raw Selenium/Chrome failure into something a user can act on.

    Chrome reports network problems as net::ERR_* inside a long chromedriver
    stacktrace; showing that verbatim tells the user nothing about what went wrong.
    """
    raw = str(message or "")
    # Match on the message only. The stacktrace mentions "chromedriver" on every
    # single error, which would otherwise swallow genuinely unknown failures.
    head = raw.split("Stacktrace:")[0]
    site = domain or "the website"
    mapping = [
        ("ERR_NAME_NOT_RESOLVED",
         f"Couldn't look up {site}. The address didn't resolve, which usually means "
         f"your DNS server or network is blocking it, or the site has moved."),
        ("ERR_INTERNET_DISCONNECTED",
         "No internet connection. Reconnect and try again."),
        ("ERR_PROXY_CONNECTION_FAILED",
         "Your proxy refused the connection. Check your proxy or VPN settings."),
        ("ERR_TUNNEL_CONNECTION_FAILED",
         "A proxy or VPN blocked the connection to this website."),
        ("ERR_CONNECTION_TIMED_OUT",
         f"{site} took too long to respond. It may be down or slow right now."),
        ("ERR_TIMED_OUT",
         f"{site} took too long to respond. It may be down or slow right now."),
        ("ERR_CONNECTION_REFUSED",
         f"{site} refused the connection."),
        ("ERR_CONNECTION_RESET",
         f"The connection to {site} was reset -- often a network filter or the site "
         f"blocking automated traffic."),
        ("ERR_CONNECTION_CLOSED",
         f"The connection to {site} closed unexpectedly."),
        ("ERR_CERT_", f"{site} has an invalid security certificate."),
        ("ERR_BLOCKED_BY", f"Your network or an extension blocked access to {site}."),
        ("session not created",
         "Chrome and the automation driver don't match. Updating Chrome usually fixes it."),
        ("cannot find chrome binary",
         "Google Chrome wasn't found on this computer. Install Chrome and try again."),
        ("chromedriver", "The browser engine failed to start."),
    ]
    for needle, friendly in mapping:
        if needle.lower() in head.lower():
            return friendly
    # Unknown failure: show only the first line, never the stacktrace dump.
    first = head.strip().splitlines()
    return first[0][:200] if first else "Unknown error."


def resolve_site_flow(domain):
    """Return (step_paths, next_btn_xpath) for a download on `domain`. Shared by the
    Search profile creator and the Watchlist direct downloader.

    A built-in flow always wins for the sites shipped support for. These selectors are
    maintained against the live site, whereas an older profile for the same domain can
    carry stale steps or delays that were hand-edited for one anime -- and the previous
    behaviour (inherit the same-domain profile with the most steps) copied exactly that
    into every newly loaded anime and every watchlist download.

    Domains with no built-in flow still inherit from the richest same-domain profile,
    since that is the only source of steps they have.
    """
    # Matched by site name, not exact host: animerco redirects eta.* -> det.*, and
    # keying on the host meant a profile built from the redirected page had no
    # built-in flow at all and silently fell through to inheritance.
    flow = site_lookup(DEFAULT_SITE_FLOWS, domain)
    if flow:
        return copy.deepcopy(flow["step_paths"]), flow.get("next_btn_xpath", "")

    key = site_key(domain)
    paths, nxt, best = {}, "", -1
    with config_lock:
        for cfg in sites_data.values():
            if site_key(cfg.get("url", "")) != key or not key:
                continue
            sp = cfg.get("step_paths", {}) or {}
            count = sum(len(v) for v in sp.values() if isinstance(v, list))
            if count > best:
                best = count
                paths = copy.deepcopy(sp)
                nxt = cfg.get("next_btn_xpath", "")
    return paths, nxt


def _make_headless_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from subprocess import CREATE_NO_WINDOW

    options = webdriver.ChromeOptions()
    # Hand the page back once the DOM is parsed instead of waiting for every image,
    # font and ad script to settle. Search and detection only ever read the DOM, and
    # both poll for content afterwards, so anything rendered late is still picked up.
    # Measured on animerco: a search page load went 617 -> 532 ms and an anime page
    # 623 -> 355 ms -- and detection pays that once per season, up to 25 of them.
    options.page_load_strategy = "eager"
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--mute-audio")
    # Detection only reads the DOM (hrefs, attributes) and fetches covers via
    # fetch() -- it never needs rendered pixels. These flags trade away rendering
    # work we don't use for markedly lower RAM/CPU per browser.
    options.add_argument("--window-size=1024,768")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-renderer-backgrounding")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-client-side-phishing-detection")
    options.add_argument("--disable-sync")
    options.add_argument("--disable-default-apps")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--metrics-recording-only")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disk-cache-size=1")
    options.add_argument("--disable-features=Translate,MediaRouter,OptimizationHints,"
                         "InterestFeedContentSuggestions,BackForwardCache")
    # Don't fetch/decode images -- the biggest per-page memory + bandwidth saver.
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
    })
    # "enable-logging" excluded: it makes Chrome open a separate console window.
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_argument("--log-level=3")
    # Resolve the supported sites through Google Public DNS rather than the machine's
    # resolver, which on some networks fails to resolve them at all.
    from utils.browser_flags import apply_dns_flags
    apply_dns_flags(options)
    service = Service()
    service.creation_flags = CREATE_NO_WINDOW
    driver = webdriver.Chrome(options=options, service=service)
    driver.set_page_load_timeout(45)
    return driver


# One shared headless Chrome reused across all search/detail operations, so we pay
# the (slow) browser launch once instead of on every search. A lock serializes
# access -- searches run one at a time, so this is safe.
_shared_driver = None
_shared_lock = threading.Lock()

def acquire_driver():
    """Return the shared search driver, creating it if needed. The caller MUST
    call release_driver() when finished (the lock is held until then)."""
    global _shared_driver
    _shared_lock.acquire()
    try:
        if _shared_driver is not None:
            try:
                _ = _shared_driver.title  # health check -- raises if the browser died
            except Exception:
                try: _shared_driver.quit()
                except Exception: pass
                _shared_driver = None
        if _shared_driver is None:
            _shared_driver = _make_headless_driver()
            _shared_driver.set_script_timeout(20)
        return _shared_driver
    except Exception:
        _shared_lock.release()
        raise

def release_driver():
    try:
        _shared_lock.release()
    except RuntimeError:
        pass

# Deliberately NOT pre-warming the browser here.
#
# Launching Chrome ahead of the first search looks like free speed -- it takes ~1.4 s
# and only the first query pays it. Measured with an empty cover cache, though, doing
# it on a background thread froze the UI: 1 stall of 880 ms with the warm-up on, none
# with it off, same search. Starting a browser is not idle work. _make_headless_driver
# resolves ~15 hostnames over HTTPS through apply_dns_flags before Chrome even starts,
# and that Python work holds the GIL against the UI thread. A 1.4 s wait the user
# chose by pressing Search beats a freeze that arrives while they are typing.
def shutdown_shared_driver():
    """Tear down the shared search browser. Never blocks indefinitely: if a search
    is mid-flight holding the lock, we grab the driver reference under a bounded
    wait and quit it outside the lock, so app close can't hang on it."""
    global _shared_driver
    got = _shared_lock.acquire(timeout=3)
    try:
        drv = _shared_driver
        _shared_driver = None
    finally:
        if got:
            _shared_lock.release()
    if drv is not None:
        try: drv.quit()
        except Exception: pass


def _prune_cover_cache(cache_dir, max_files=300):
    """Keep the cover cache bounded -- drop the oldest files beyond max_files.

    Uses scandir so the is-file check and mtime come from the directory entry that
    was already read, instead of two extra stat() syscalls per cached cover.
    """
    try:
        with os.scandir(cache_dir) as it:
            files = [(e.stat().st_mtime, e.path) for e in it if e.is_file()]
        if len(files) <= max_files:
            return
        files.sort(key=lambda t: t[0])
        for _mtime, path in files[:len(files) - max_files]:
            try: os.remove(path)
            except Exception: pass
    except Exception:
        pass


# JS heuristic: find result cards two ways, so it works whether the poster is an
# <img> (title usually in alt, e.g. witanime) OR a CSS background-image on an
# anchor (title in the link text, e.g. animerco). Fully generic -- no selectors.
_AUTODETECT_JS = r"""
var query = arguments[0];
var callback = arguments[1];
try {
    // Single characters are normally dropped as noise, but if that leaves nothing
    // the user really did search for one letter -- keep it, or every card is
    // rejected and a valid search returns empty. Such a query matches almost any
    // title, so `broad` then demands a poster to keep nav/genre links out.
    let all = query.toLowerCase().split(/\s+/).filter(w => w.length > 0);
    let words = all.filter(w => w.length > 1);
    let broad = false;
    if (words.length === 0) { words = all; broad = true; }
    let matches = (t) => {
        if (!t || words.length === 0) return false;
        let lt = t.toLowerCase();
        let c = 0; words.forEach(w => { if (lt.includes(w)) c++; });
        return c >= Math.max(1, Math.ceil(words.length * 0.5));
    };
    let bgUrl = (el) => {
        let s = getComputedStyle(el).backgroundImage;
        if (s && s.indexOf('url(') === 0 && s.indexOf('gradient') === -1) {
            let m = s.match(/url\(["']?(.*?)["']?\)/);
            if (m && /^https?:/.test(m[1])) return m[1];
        }
        return '';
    };
    // Lazy-loading attribute on ANY element, not just <img>. animerco hangs the real
    // poster off the card's own anchor (data-src) and only paints a spinner
    // placeholder into background-image until the card scrolls into view -- so
    // reading this is what makes covers appear without waiting on lazy-load.
    let lazyUrl = (el) => {
        if (!el || !el.getAttribute) return '';
        let ds = el.getAttribute('data-src') || el.getAttribute('data-lazy-src')
              || el.getAttribute('data-original') || el.getAttribute('data-bg') || '';
        return /^https?:/.test(ds) ? ds : '';
    };
    // Pull an image URL out of an <img> (real src or lazy attr), skipping placeholders.
    let imgUrl = (img) => {
        let src = img.getAttribute('src') || '';
        let ds = img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('data-original') || '';
        if (/^https?:/.test(ds)) return ds;
        if (/^https?:/.test(src) && !/thumbnail-default|placeholder|blank\.|lazy-|spacer/i.test(src)) return src;
        return '';
    };
    let results = [];
    let seen = {};
    // Anchor-centric: for every content link, look in its card (self, parent,
    // grandparent) for a cover (sibling <img> or CSS background) and a title
    // (link text, else a nearby image's alt). Handles posters that are <img>
    // OR background-image, and cards where the img and link are siblings.
    document.querySelectorAll('a[href]').forEach(a => {
        let link = a.href;
        try { let u = new URL(link); if (u.pathname === '' || u.pathname === '/') return; } catch(e) { return; }
        if (seen[link]) return;
        let title = (a.innerText || a.title || a.getAttribute('aria-label') || '').trim();
        let cover = '', altTitle = '';
        let scopes = [a, a.parentElement, a.parentElement && a.parentElement.parentElement].filter(Boolean);
        for (let sc of scopes) {
            let img = sc.querySelector('img');
            if (img) {
                if (!cover) cover = imgUrl(img);
                if (!altTitle) altTitle = (img.getAttribute('alt') || img.getAttribute('title') || '').trim();
            }
            // Lazy attribute first: it holds the real poster even before the image
            // has loaded, whereas background-image is still a placeholder then.
            if (!cover) cover = lazyUrl(sc);
            if (!cover) {
                for (let e of sc.querySelectorAll('[data-src],[data-lazy-src],[data-original],[data-bg]')) {
                    let u = lazyUrl(e); if (u) { cover = u; break; }
                }
            }
            if (!cover) {
                let b = bgUrl(sc);
                if (b) cover = b;
                else { for (let e of sc.querySelectorAll('*')) { let u = bgUrl(e); if (u) { cover = u; break; } } }
            }
            if (cover && (title || altTitle)) break;
        }
        let finalTitle = title || altTitle;
        if (finalTitle && matches(finalTitle) && (!broad || cover)) {
            seen[link] = 1;
            results.push({ title: finalTitle, link: link, img: cover });
        }
    });
    callback(results);
} catch(e) {
    callback("Error: " + e.message);
}
"""

_FETCH_IMG_JS = r"""
var src = arguments[0];
var callback = arguments[1];
fetch(src)
    .then(response => response.blob())
    .then(blob => {
        var reader = new FileReader();
        reader.onloadend = function() { callback(reader.result); };
        reader.readAsDataURL(blob);
    })
    .catch(err => callback("Error: " + err));
"""

# Fetch many cover images at once, in parallel inside the browser. Returns an
# array of data: URLs (empty string for any that failed). One round-trip instead
# of one per image -- the main search speed-up.
_FETCH_IMGS_JS = r"""
var srcs = arguments[0];
var maxEdge = arguments[1];
var callback = arguments[2];
// Downscale inside the browser before handing the bytes back. A poster is often
// 150KB+ at full size but only ~20KB once resized to what we actually draw, and
// everything here crosses the automation channel base64-encoded (a third larger
// again) -- so shrinking first is most of the cost of loading a grid of covers.
// The blob comes from fetch(), so the canvas is not tainted and can be exported.
function shrink(blob) {
    return createImageBitmap(blob).then(function (bmp) {
        var scale = Math.min(1, maxEdge / Math.max(bmp.width, bmp.height));
        var w = Math.max(1, Math.round(bmp.width * scale));
        var h = Math.max(1, Math.round(bmp.height * scale));
        var canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(bmp, 0, 0, w, h);
        bmp.close();
        return canvas.toDataURL('image/jpeg', 0.85);
    });
}
function asDataUrl(blob) {          // fallback if the canvas route is unavailable
    return new Promise(function (res) {
        var fr = new FileReader();
        fr.onloadend = function () { res(fr.result); };
        fr.onerror = function () { res(''); };
        fr.readAsDataURL(blob);
    });
}
Promise.all(srcs.map(function (src) {
    return fetch(src)
        .then(function (r) { return r.blob(); })
        .then(function (b) { return shrink(b).catch(function () { return asDataUrl(b); }); })
        .catch(function () { return ''; });
})).then(function (results) { callback(results); });
"""


def _covers_cache_dir():
    d = os.path.join(tempfile.gettempdir(), "AnimeSearchCovers")
    os.makedirs(d, exist_ok=True)
    return d


# WordPress stores a full-res original alongside sized variants named
# "<name>-<W>x<H>.<ext>". Strip that suffix to fetch the sharp original instead of
# the tiny 90x135 thumbnail the season list lazy-loads.
_WP_SIZE_RE = re.compile(r'-\d{2,4}x\d{2,4}(?=\.(?:jpg|jpeg|png|webp)(?:$|\?))', re.I)


def _full_res(url):
    return _WP_SIZE_RE.sub("", url or "")


# Covers are drawn at 164px in search results and 56px in the Watchlist, so anything
# past ~500px is wasted download, transfer and decode time.
COVER_MAX_EDGE = 500
_WP_SIZE_DIMS = re.compile(r'-(\d{2,4})x(\d{2,4})(?=\.(?:jpg|jpeg|png|webp)(?:$|\?))', re.I)


def _best_cover_url(url):
    """Pick the cheapest version of a cover that is still sharp on screen.

    WordPress serves sized variants next to the original. A listing that already
    links a reasonably large one (witanime's 323x470) is used as-is instead of
    upgrading to the multi-hundred-KB original, while a tiny lazy-load thumbnail
    (animerco's 90x135) is still swapped for the full image so it does not look soft.
    """
    url = (url or "").strip()
    m = _WP_SIZE_DIMS.search(url)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
        if width >= 300 or height >= 400:
            return url          # already big enough for the sizes we draw
        return _full_res(url)   # a thumbnail -- fetch the original instead
    return url


# Reading a cover file for the FIRST time after it was written costs around half a
# second on Windows -- the on-access scan of bytes that have just arrived from the
# net. Measured on a page of 19: downloading and decoding them all took 528 ms, the
# next search re-reading those same files took 10171 ms, and a third search over the
# now-settled files took 47 ms. So the cost is real, once per file, and serial.
#
# Two answers, both cheap. Read the cached files concurrently, which caps that tail;
# and after writing new ones, read them once in the background so the charge is paid
# while nobody is waiting. Only the read is spread across threads -- decoding and the
# signal that follows stay on the calling thread, because Qt objects belong to the
# thread that made them.
_COVER_IO_WORKERS = 8


def _read_cover_files(paths):
    """Read several cache files at once. Returns {path: bytes}, skipping failures."""
    if not paths:
        return {}

    def one(p):
        try:
            with open(p, "rb") as f:
                return p, f.read()
        except OSError:
            return p, None

    from concurrent.futures import ThreadPoolExecutor
    workers = min(_COVER_IO_WORKERS, len(paths))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return {p: data for p, data in pool.map(one, paths) if data}


def _warm_cover_files(paths):
    """Pay the first-read cost of freshly written covers off the critical path.

    One plain daemon thread reading in sequence -- deliberately not the thread pool
    above. A pool's workers are joined at interpreter exit, and closing the app while
    one was still warming left chromedriver running behind it.
    """
    paths = [p for p in paths if p]
    if not paths:
        return

    def warm():
        for p in paths:
            try:
                with open(p, "rb") as f:
                    f.read()
            except OSError:
                pass

    threading.Thread(target=warm, daemon=True, name="cover-warm").start()


def download_covers(driver, img_urls, cache_dir=None, on_image=None):
    """Download image URLs (from the current same-origin page) to local cache files.

    Returns a list of local paths aligned with img_urls ("" for any that failed or
    were empty). Cached by MD5 of the URL, batch-fetched in one round-trip.

    `on_image(index, path, image)`, if given, is called with each cover as a decoded
    QImage the moment it is ready, so a caller can show them one by one instead of
    waiting for the batch. Decoding happens HERE, from the bytes already in memory:
    reading back the file just written costs 0.7-2.9 s per cover on Windows, because
    the first access to newly-downloaded bytes waits for the virus scanner. Nineteen
    covers measured 55.6 s that way against 620 ms to download them all. Decoding a
    file the scanner has already seen is ~0.3 ms, so the cached branch below can
    still read from disk.
    """
    import hashlib
    if cache_dir is None:
        cache_dir = _covers_cache_dir()
    _prune_cover_cache(cache_dir)

    from PyQt6.QtGui import QImage

    def announce(index, path, data=None):
        """Hand a decoded cover to the caller, from memory when we have the bytes."""
        if on_image is None:
            return
        try:
            img = QImage()
            ok = img.loadFromData(data) if data is not None else img.load(path)
            if ok and not img.isNull():
                on_image(index, path, img)
        except Exception:
            pass

    paths = [""] * len(img_urls)
    to_fetch = []   # (index, url, cache_path)
    cached = []     # (index, cache_path)
    for i, url in enumerate(img_urls):
        url = _best_cover_url(url)
        if not url or url.startswith("data:"):
            continue
        key = hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]
        p = os.path.join(cache_dir, f"{key}.img")
        if os.path.exists(p) and os.path.getsize(p) > 500:
            paths[i] = p
            cached.append((i, p))
        else:
            to_fetch.append((i, url, p))

    if cached:
        blobs = _read_cover_files([p for _i, p in cached])
        for i, p in cached:
            announce(i, p, blobs.get(p))

    if to_fetch:
        try:
            data_urls = driver.execute_async_script(
                _FETCH_IMGS_JS, [f[1] for f in to_fetch], COVER_MAX_EDGE) or []
        except Exception:
            data_urls = []
        written = []
        for j, (i, _url, p) in enumerate(to_fetch):
            du = data_urls[j] if j < len(data_urls) else ""
            if isinstance(du, str) and du.startswith("data:image"):
                try:
                    raw = base64.b64decode(du.split(",", 1)[1])
                except Exception:
                    continue
                # Show it first, write it second. The cache write is only for the
                # next search; nothing in this one reads the file back.
                announce(i, p, raw)
                try:
                    with open(p, "wb") as f:
                        # Already downscaled in the browser, so it lands ready to draw.
                        f.write(raw)
                    paths[i] = p
                    written.append(p)
                except Exception:
                    pass
        _warm_cover_files(written)
    return paths


# Every anchor on the page, with the fields detection needs, in ONE round-trip.
#
# Selenium's get_attribute() is a separate HTTP call to chromedriver -- 2-4 ms each
# on this machine. Detection read up to four attributes plus a nested find_element
# off every anchor, and witanime's One Piece page carries 2428 of them, so a single
# _find_season_links pass measured 22.4 s and a single _derive_page pass 10.0 s
# (43.3 s for the whole detection). The same data collected here takes 26 ms.
_ANCHORS_JS = """
return Array.prototype.map.call(document.getElementsByTagName('a'), function (a) {
    var img = a.querySelector('img');
    return {
        href: a.href || '',
        onclick: a.getAttribute('onclick') || '',
        // innerText rather than textContent, to match what Selenium's .text returned
        // here (rendered text only) so season labels come out identical.
        text: (a.innerText || a.textContent || '').trim(),
        title: a.getAttribute('title') || '',
        poster: a.getAttribute('data-src') || '',
        // The poster often sits on a descendant <img> instead of the anchor itself.
        imgPoster: img ? (img.getAttribute('data-src') || img.getAttribute('src') || '') : ''
    };
});
"""


def page_anchors(driver):
    """Anchors on the currently-loaded page as plain dicts. Never raises: a page
    that navigates mid-script just yields no anchors, and the caller polls again."""
    try:
        rows = driver.execute_script(_ANCHORS_JS) or []
    except Exception:
        return []
    return [r for r in rows if isinstance(r, dict)]


class AnimeSearchThread(QThread):
    finished = pyqtSignal(list)
    error = pyqtSignal(str)
    # (link, cover) where cover is a decoded QImage whenever the worker already has
    # one -- the GUI thread must not open and decode poster files itself.
    cover_loaded = pyqtSignal(str, object)

    def __init__(self, query, search_url_template, page=1):
        super().__init__()
        self.query = query
        self.search_url_template = search_url_template
        self.page = page

    def _poll_heuristic(self, driver, timeout, done):
        """Repeatedly run the detector until `done(results)` is true or `timeout`
        elapses; returns the latest results (or an error string). Lets a fast page
        return in a fraction of a second instead of waiting a fixed time."""
        end = time.time() + timeout
        last = []
        while time.time() < end:
            if self.isInterruptionRequested():
                return last
            try:
                r = driver.execute_async_script(_AUTODETECT_JS, self.query)
            except Exception:
                r = []
            if isinstance(r, str):
                return r
            if r:
                last = r
                if done(r):
                    return r
            time.sleep(0.2)
        return last

    def run(self):
        search_url = self.search_url_template.replace("{query}", quote(self.query))
        
        # Inject page parameter
        if self.page > 1:
            if "witanime.site" in search_url:
                search_url += f"&page={self.page}"
            elif "animerco.org" in search_url:
                search_url = search_url.replace("/?s=", f"/page/{self.page}/?s=")
        
        if "witanime.site" in search_url:
            import urllib.request, re, os, hashlib
            from PyQt6.QtGui import QImage
            from PyQt6.QtCore import Qt
            try:
                entries, seen = [], set()
                req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
                try:
                    html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8")
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        html = ""
                    else:
                        raise
                        
                cards = re.findall(r'''<a[^>]+href=['"]([^'"]+/(?:anime|movie)/[^'"]+)['"][^>]*>(.*?)</a>''', html, re.DOTALL | re.IGNORECASE)
                for url, inner in cards:
                    if url in seen: continue
                    img = re.search(r'''<img[^>]+src=['"]([^'"]+)['"][^>]*alt=['"]([^'"]+)['"]''', inner, re.IGNORECASE)
                    if img:
                        cover_url, title = img.groups()
                        # cover starts empty (a path, once fetched); img holds the URL.
                        entries.append({"link": url, "title": title.strip(),
                                        "cover": "", "img": cover_url})
                        seen.add(url)
                
                cache_dir = _covers_cache_dir()
                _prune_cover_cache(cache_dir)

                # Cards first; covers stream in behind them.
                #
                # The manager is built per search, in this worker thread, and that is
                # deliberate. Pooling one across searches was tried: a manager belongs
                # to the thread that created it, so sharing one means moving the whole
                # cover path onto the GUI thread -- and that version crashed natively
                # (0xC0000409) on every run, with and without aborting in-flight
                # replies. The only thing pooling would have saved is a DNS lookup and
                # TLS handshake per page, which measured as noise next to the download
                # itself. Not worth a crash.
                targets = entries[:60]
                self.finished.emit(entries)
                if targets:
                    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest
                    from PyQt6.QtCore import QUrl, QEventLoop

                    manager = QNetworkAccessManager()
                    loop = QEventLoop()
                    pending = [len(targets)]
                    written = []

                    def on_reply(reply, entry, url):
                        if not self.isInterruptionRequested():
                            try:
                                data = reply.readAll()
                                if data:
                                    key = hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]
                                    p = os.path.join(cache_dir, f"{key}.img")
                                    img = QImage()
                                    if img.loadFromData(data) and not img.isNull():
                                        img = img.scaled(164, 200,
                                                         Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                                         Qt.TransformationMode.SmoothTransformation)
                                        img.save(p, "JPEG", 50)
                                        written.append(p)
                                        # Hand over the image we already hold. Sending
                                        # the path instead made the GUI thread reopen
                                        # and decode this very file, while this thread
                                        # was still writing others -- measured freezes
                                        # of 3 to 16 s, every one of them stuck in
                                        # QImageReader.size().
                                        self.cover_loaded.emit(entry["link"], img)
                                    else:
                                        with open(p, "wb") as f:
                                            f.write(data)
                                        loaded = QImage()
                                        if loaded.load(p) and not loaded.isNull():
                                            self.cover_loaded.emit(entry["link"], loaded)
                            except Exception:
                                pass
                        reply.deleteLater()
                        pending[0] -= 1
                        if pending[0] == 0 or self.isInterruptionRequested():
                            loop.quit()

                    # Sort the page into what must be downloaded and what is already
                    # on disk, and start the downloads first: the cached ones used to
                    # be decoded inline here, which held every request behind them.
                    cached, needed = [], []
                    for entry in targets:
                        url = _best_cover_url(entry.get("img", ""))
                        if not url:
                            pending[0] -= 1
                            continue
                        key = hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]
                        p = os.path.join(cache_dir, f"{key}.img")
                        if os.path.exists(p) and os.path.getsize(p) > 100:
                            cached.append((entry, p))
                            pending[0] -= 1
                        else:
                            needed.append((entry, url))

                    for entry, url in needed:
                        if self.isInterruptionRequested():
                            break
                        req = QNetworkRequest(QUrl(url))
                        req.setRawHeader(b"User-Agent", b"Mozilla/5.0")
                        req.setRawHeader(b"Referer", b"https://witanime.site/")
                        # Qt waits forever by default (transferTimeout is 0) and opens
                        # at most 6 connections per host, so one stalled poster holds a
                        # slot and everything behind it waits -- measured as a page
                        # taking 16 s to deliver 6 of 24 covers that elsewhere took 1 s.
                        # A bounded wait hands the slot back.
                        req.setTransferTimeout(8000)
                        reply = manager.get(req)
                        reply.finished.connect(lambda r=reply, e=entry, u=url: on_reply(r, e, u))

                    # Now the cached ones, while those downloads run. The files are
                    # read together because the first read of a cover written by an
                    # earlier search waits on the virus scanner -- one page of 19
                    # measured 10.2 s read one after another, 47 ms once settled.
                    # Decoding stays here, on this thread, so no Qt object is shared.
                    if cached:
                        blobs = _read_cover_files([p for _e, p in cached])
                        for entry, p in cached:
                            if self.isInterruptionRequested():
                                break
                            data = blobs.get(p)
                            cached_img = QImage()
                            ok = (cached_img.loadFromData(data) if data
                                  else cached_img.load(p))
                            if ok and not cached_img.isNull():
                                self.cover_loaded.emit(entry["link"], cached_img)

                    # Leaving a page must release its connections at once. Without
                    # this the loop only ends when a reply arrives, so a page whose
                    # covers are timing out held its manager (and its slots against
                    # the image host) for the full 8 s while the next page opened 24
                    # more -- which is why covers on the following page came back as
                    # placeholders.
                    from PyQt6.QtCore import QTimer as _QTimer
                    guard = _QTimer()
                    guard.setInterval(250)

                    def _bail():
                        if self.isInterruptionRequested():
                            loop.quit()

                    guard.timeout.connect(_bail)
                    guard.start()
                    try:
                        if pending[0] > 0 and not self.isInterruptionRequested():
                            loop.exec()
                    finally:
                        guard.stop()
                        # Read what we just wrote, in the background, so the next
                        # search over this page does not pay for it.
                        _warm_cover_files(written)
            except Exception as e:
                self.error.emit(f"Witanime search failed: {e}")
            return

        try:
            driver = acquire_driver()
        except Exception as e:
            self.error.emit(str(e))
            return
        try:
            # search_url already carries the page (built at the top of run()).
            # Rebuilding it from the template here silently dropped /page/N/, so
            # every animerco page loaded page 1.
            driver.get(search_url)

            # Smart wait: poll for result cards to appear instead of a fixed sleep.
            raw = self._poll_heuristic(driver, timeout=6.0, done=lambda r: len(r) >= 1)
            if isinstance(raw, str):  # JS reported an error string
                raise RuntimeError(raw)
            if not raw:
                self.finished.emit([])
                return

            # Scroll to trigger lazy poster images, then poll (briefly) until they resolve.
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                pass
            raw2 = self._poll_heuristic(driver, timeout=3.5,
                                        done=lambda r: bool(r) and all(x.get("img") for x in r))
            if isinstance(raw2, list) and raw2:
                raw = raw2

            items = []
            for item in raw[:60]:
                title = (item.get("title") or "").strip()
                link = (item.get("link") or "").strip()
                img = (item.get("img") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link, "img": img, "cover": ""})

            # Show the cards now, with no cover; stream the covers in afterwards. The
            # covers were the whole wait on a cold cache -- the detection itself is a
            # few ms -- so emitting first is what makes results appear promptly instead
            # of after every poster has downloaded.
            self.finished.emit([{"title": it["title"], "link": it["link"], "cover": ""}
                                for it in items])

            # Covers are fetched through the browser (same-origin fetch), so this must
            # happen while the driver is still held. download_covers returns paths
            # aligned with the inputs; announce each one as a card update.
            # Each cover is announced as it is decoded, so the grid fills in rather
            # than staying blank until the batch ends. Decoding happens inside
            # download_covers, from the downloaded bytes -- re-reading the files it
            # had just written measured 55.6 s for 19 covers (virus scanner), and
            # sending the path instead would put that same read on the GUI thread.
            def announce(_index, _path, image, _items=items):
                if not self.isInterruptionRequested():
                    self.cover_loaded.emit(_items[_index]["link"], image)

            download_covers(driver, [it["img"] for it in items], on_image=announce)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            release_driver()

class AnimeDetailsThread(QThread):
    """Derive episode-URL template(s) + counts from an anime page.

    Flat sites (e.g. witanime) list every episode on the anime page. Seasons-based
    sites (e.g. animerco) list season pages, each of which lists that season's
    episodes -- so we detect the seasons and derive each one separately.
    """
    finished = pyqtSignal(list)   # [{"label": str, "template": str, "max_ep": int}]
    error = pyqtSignal(str)

    def __init__(self, anime_url):
        super().__init__()
        self.anime_url = anime_url

    def run(self):
        try:
            entries = self.detect_entries(self.anime_url, want_covers=True)
        except Exception as e:
            self.error.emit(str(e))
            return
        if entries:
            self.finished.emit(entries)
        elif site_key(self.anime_url) in broken_sites():
            # Repeated empty results on pages that were full of links. That is the
            # site changing its layout, not this anime lacking episodes -- saying
            # "no episodes" would send the user hunting for a fault on their end.
            self.error.emit(
                f"{site_display_name(self.anime_url)} seems to have changed its layout — "
                "episode detection is failing on every title from this site, not just "
                "this one. It needs an app update to follow the change.")
        else:
            self.error.emit("Could not detect episodes for this title.")

    def detect_entries(self, anime_url, want_covers=True, driver=None, should_cancel=None):
        """Synchronous episode detection for an anime page. Returns a list of
        entries [{"label","template","max_ep","poster","cover"}] (one per season,
        or a single flat entry), or []. Reused by search and the new-episode watcher.

        Pass an explicit `driver` to run on a caller-owned browser (the watcher's
        parallel pool); otherwise the shared search driver is acquired/released.
        `should_cancel` is a predicate polled at loop boundaries so the caller can
        abort a long detection (e.g. on app close); defaults to this thread's own
        interruption flag.
        """
        if should_cancel is None:
            should_cancel = self.isInterruptionRequested
        # _find_season_links derives the base host from self.anime_url, so keep it in
        # sync (the watcher constructs this thread with an empty url and passes it here).
        self.anime_url = anime_url
        
        # Fast path for Witanime movies: they don't have episode lists, just a single watch page.
        if "witanime.site/movie/" in anime_url:
            return [{"template": anime_url.replace("/movie/", "/watch/movie/"), "max_ep": 1}]
            
        # --- FAST PATH FOR WITANIME.SITE (Bypasses Cloudflare headless block) ---
        if "witanime.site" in anime_url:
            try:
                import urllib.request, re
                req = urllib.request.Request(anime_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as response:
                    html = response.read().decode("utf-8")
                hrefs = re.findall(r'''href=['"]([^'"]+)['"]''', html, re.IGNORECASE)
                template, max_ep = self._derive_from_hrefs(hrefs)
                if template:
                    record_detection(anime_url, len(hrefs), 1)
                    return [{"label": "", "template": template, "max_ep": max_ep, "poster": "", "cover": ""}]
                return []
            except Exception:
                return []
        # ------------------------------------------------------------------------

        own = driver is None
        if own:
            driver = acquire_driver()
        try:
            driver.get(anime_url)
            # Every detection loads a page anyway, so this is where a site move shows
            # up first -- for free, with no request of its own.
            try:
                record_landing(anime_url, driver.current_url)
            except Exception:
                pass

            # Seasons-based site FIRST: an anime page often shows a "latest episodes"
            # widget for OTHER shows, which would otherwise be grabbed as this anime's
            # episodes. Real season pages are the correct source. Smart wait: content
            # is server-rendered, so poll until seasons appear OR flat episodes are
            # already derivable, returning in a fraction of a second.
            seasons = []
            anchors = []
            end = time.time() + 5.0
            while time.time() < end:
                if should_cancel():
                    return []
                # Read the page once and let both checks work off it.
                anchors = page_anchors(driver)
                seasons = self._find_season_links(driver, anchors)
                if seasons:
                    break
                t, _ = self._derive_page(driver, anchors)
                if t:      # flat page already lists episodes -> handle below
                    break
                time.sleep(0.2)

            if seasons:
                entries = []
                for label, url, poster in seasons[:25]:
                    if should_cancel():
                        break
                    try:
                        driver.get(url)
                        tmpl, mx = self._derive_ready(driver, timeout=4.0)
                        if tmpl:
                            entries.append({"label": label, "template": tmpl, "max_ep": mx,
                                            "poster": poster, "cover": ""})
                    except Exception:
                        continue
                if entries:
                    if want_covers:
                        # Same origin as the current page, so fetch() works here.
                        try:
                            covers = download_covers(driver, [e["poster"] for e in entries])
                            for e, cov in zip(entries, covers):
                                e["cover"] = cov
                        except Exception:
                            pass
                    record_detection(anime_url, len(anchors), len(entries))
                    return entries

            # Flat site: episodes listed directly on the anime page (e.g. witanime).
            template, max_ep = self._derive_ready(driver, timeout=4.0)
            if template:
                record_detection(anime_url, len(anchors), 1)
                return [{"label": "", "template": template, "max_ep": max_ep,
                         "poster": "", "cover": ""}]
            # A page full of links that yields nothing is a layout change, not an
            # anime with no episodes -- record it so the app can say which it was.
            record_detection(anime_url, len(anchors), 0)
            return []
        finally:
            if own:
                release_driver()

    def _derive_ready(self, driver, timeout):
        """Poll _derive_page until it yields a template or `timeout` elapses.

        Returns as soon as the episode list is present (server-rendered pages
        resolve on the first pass), so it replaces a fixed per-page sleep.
        """
        end = time.time() + timeout
        while time.time() < end:
            try:
                t, m = self._derive_page(driver)
                if t:
                    return t, m
            except Exception:
                pass
            time.sleep(0.2)
        return "", 0

    def _derive_page(self, driver, anchors=None):
        """Derive (template, max_ep) from the currently-loaded page, or ('', 0).

        `anchors` lets a caller that already read the page (see detect_entries) share
        one page_anchors() round-trip across both checks instead of paying for two.
        """
        if anchors is None:
            anchors = page_anchors(driver)
        hrefs = [a["href"] for a in anchors if a.get("href")]
        onclicks = [a["onclick"] for a in anchors if a.get("onclick")]
        # Prefer the openEpisode/onclick signal; fall back to href-number grouping.
        template, max_ep = self._derive_from_onclick(driver, onclicks)
        if not template:
            template, max_ep = self._derive_from_hrefs(hrefs)
        if not template:
            # Last resort: a movie/OVA or a series with a single uploaded episode.
            # Both grouping strategies need >=2 links to infer a pattern, so these
            # would otherwise look like "no episodes at all".
            template, max_ep = self._derive_single(self._onclick_episode_urls(onclicks) + hrefs)
        return (template, max_ep) if template else ("", 0)

    def _find_season_links(self, driver, anchors=None):
        """Same-domain season pages (/seasons/<slug>/) linked from the anime page,
        each with its own label and (lazy-loaded) poster URL.

        Returns a list of (label, url, poster_url). poster_url may be "" if none
        was found; the real image is a data-src attribute (the visible
        background-image is a loading-spinner placeholder until scrolled into view).
        """
        if anchors is None:
            anchors = page_anchors(driver)
        # Match against the page actually loaded, not the URL that was requested.
        # animerco now redirects eta.animerco.org -> det.animerco.org, so comparing
        # with the requested host rejected every season link on the page: each anime
        # fell through to the flat branch and loaded as one fake episode.
        base = ""
        try:
            base = urlparse(driver.current_url).netloc
        except Exception:
            pass
        base = base or urlparse(self.anime_url).netloc
        by_url, order = {}, []
        for a in anchors:
            h = a.get("href") or ""
            txt = (a.get("text") or "").strip()
            title_attr = a.get("title") or ""
            poster = a.get("poster") or ""
            p = urlparse(h)
            if p.netloc != base:
                continue
            segs = [s for s in p.path.split("/") if s]
            # /seasons/<slug>/ only -- skip the /seasons/ index and the
            # /season/<year> seasonal calendar.
            if len(segs) < 2 or segs[0] != "seasons":
                continue
            clean = f"{p.scheme}://{p.netloc}{p.path}"
            # Poster may sit on a descendant <img> instead of the anchor's data-src.
            if not poster:
                poster = a.get("imgPoster") or ""
            if poster.startswith("data:"):
                poster = ""  # placeholder spinner, not a real cover
            rec = by_url.get(clean)
            if rec is None:
                by_url[clean] = {"label": self._season_label(txt or title_attr, clean),
                                 "poster": poster}
                order.append(clean)
            elif poster and not rec["poster"]:
                # A second anchor to the same season carried the poster.
                rec["poster"] = poster
        return [(by_url[u]["label"], u, by_url[u]["poster"]) for u in order]

    # Arabic season ordinals -> number (e.g. "الموسم الأول" == season 1).
    _AR_ORDINAL = [
        ("الأول", 1), ("الاول", 1), ("الثاني", 2), ("الثالث", 3), ("الرابع", 4),
        ("الخامس", 5), ("السادس", 6), ("السابع", 7), ("الثامن", 8),
        ("التاسع", 9), ("العاشر", 10),
    ]

    @staticmethod
    def _season_label(text, url):
        hay = (text or "") + " " + unquote(url)
        # Latin "season N" (in text or slug).
        m = re.search(r'season[-\s_]*(\d+)', hay, re.I)
        if m:
            return f"Season {m.group(1)}"
        # Arabic "الموسم N" with a literal digit (e.g. title attr "Bleach الموسم 1").
        m = re.search(r'الموسم[-\s_]*(\d+)', hay)
        if m:
            return f"Season {m.group(1)}"
        # Arabic ordinal (الموسم الأول / الثاني / ...). Skip teens ("... عشر"),
        # whose words overlap the single ordinals and are rare on these sites.
        if "عشر" not in hay:
            for word, n in AnimeDetailsThread._AR_ORDINAL:
                if word in hay:
                    return f"Season {n}"
        t = (text or "").strip()
        return (t[:40] or "Season")

    # URL markers that identify a real episode link (vs movie/series/related noise).
    _EP_MARKERS = ("/episode", "/watch/", "/ep-", "/ep/", "الحلقة", "حلقة")

    @staticmethod
    def _onclick_episode_urls(onclicks):
        """Decode witanime-style openEpisode('<base64>') handlers into plain URLs."""
        urls = []
        for oc in onclicks:
            m = re.search(r"openEpisode\('([a-zA-Z0-9+/=]+)'\)", oc)
            if not m:
                continue
            try:
                urls.append(base64.b64decode(m.group(1)).decode("utf-8", "ignore"))
            except Exception:
                continue
        return urls

    def _derive_single(self, urls):
        """Handle a page listing exactly ONE entry -- a movie/OVA, or a series whose
        first episode is its only upload.

        Movies have no episode number in the URL at all (e.g. .../episode/فيلم-<slug>-movie/),
        so there is nothing to parameterise: the URL itself is the whole target and the
        profile holds a single "episode 1". A lone numbered episode still gets a {x}
        template so later uploads keep working.
        """
        seen, eps = set(), []
        for u in urls:
            if not u.startswith(("http://", "https://")):
                continue
            u = unquote(u).split("#")[0]
            if not any(mk in u.lower() for mk in self._EP_MARKERS):
                continue
            if u not in seen:
                seen.add(u)
                eps.append(u)
        if len(eps) != 1:
            return "", 0
        url = eps[0]
        # A trailing episode number -> keep it parameterised (but ignore year-like runs).
        m = None
        for m in re.finditer(r'\d+', url):
            pass
        if m:
            n = int(m.group(0))
            if n >= 1 and not (1990 <= n <= 2035):
                return url[:m.start()] + "{x}" + url[m.end():], n
        return url, 1

    def _derive_from_hrefs(self, hrefs):
        """Group episode-looking URLs by replacing their last number with {x};
        the biggest group is the episode list."""
        groups = defaultdict(list)
        decoded_eps = []
        for href in hrefs:
            if not href.startswith(("http://", "https://")):
                continue  # skip javascript:void(0), #, mailto:, etc.
            # Decode first: percent-encoded (e.g. Arabic) URLs otherwise expose
            # spurious digits from the %XX hex, masking the real episode number.
            href = unquote(href)
            # Only consider actual episode links -- excludes /movies/, /animes/,
            # and "related" links that would otherwise form spurious groups.
            low = href.lower()
            if not any(mk in low for mk in self._EP_MARKERS):
                continue
            decoded_eps.append(href)
            # Match the LAST run of digits in the URL (episode index).
            m = None
            for m in re.finditer(r'\d+', href):
                pass
            if not m:
                continue
            template = href[:m.start()] + "{x}" + href[m.end():]
            groups[template].append(int(m.group(0)))
        if not groups:
            return "", 0
        best = max(groups, key=lambda t: len(groups[t]))
        nums = groups[best]
        # Require >=2 links and a real episode count. Reject pure year clusters
        # (e.g. a "2021/2022/2023" recommended block) -- but NOT legit episode
        # groups that happen to start high (e.g. Bleach eps 63-366, where 1-62
        # live under a different URL slug the download engine resolves on 404).
        if len(nums) < 2 or max(nums) < 1:
            return "", 0
        if all(1990 <= n <= 2035 for n in nums):
            return "", 0
        # Extend the count to a finale episode that shares this base but carries a
        # trailing suffix after the number (e.g. animerco's "-والاخيرة" last episode).
        prefix = best.split("{x}")[0]
        all_nums = list(nums)
        for d in decoded_eps:
            if d.startswith(prefix):
                mm = re.match(r'(\d+)', d[len(prefix):])
                if mm:
                    all_nums.append(int(mm.group(1)))
        return best, max(all_nums)

    def _derive_from_onclick(self, driver, onclicks):
        """Fallback for sites that open episodes via onclick=openEpisode('base64')."""
        ids = []
        for oc in onclicks:
            m = re.search(r"openEpisode\('([a-zA-Z0-9+/=]+)'\)", oc)
            if m:
                ids.append(m.group(1))
        if len(ids) < 2:
            return "", 0
        decoded = []
        for enc in ids:
            try:
                url = unquote(base64.b64decode(enc).decode("utf-8", "ignore"))
                m = None
                for m in re.finditer(r'\d+', url):
                    pass
                if m:
                    decoded.append((url, int(m.group(0)), m.start(), m.end()))
            except Exception:
                continue
        nums = [n for _, n, _, _ in decoded]
        if len(decoded) < 2 or max(nums) < 1:
            return "", 0
        if all(1990 <= n <= 2035 for n in nums):
            return "", 0
        # Build the template from the lowest-numbered episode so {x} lands on the
        # episode index (not a season/year number elsewhere in the URL).
        url, _, s, e = min(decoded, key=lambda d: d[1])
        template = url[:s] + "{x}" + url[e:]
        return template, max(nums)


class AnimeResultCard(SimpleCardWidget):
    # The cover travels as `object`: a decoded QImage (what search delivers) or a
    # path. It used to be a str path only, and once search switched to handing over
    # QImages the path stayed "" -- so Follow saved no poster to the Watchlist and
    # season cards lost the anime's poster to fall back on.
    selected = pyqtSignal(str, str, object)   # (title, href, cover)

    follow = pyqtSignal(str, str, object)   # (title, href, cover)

    def __init__(self, title, href, cover_path, parent=None, on_load=None,
                 button_text="Load Anime", followable=False):
        super().__init__(parent)
        from PyQt6.QtGui import QImage
        self.title = title
        self.href = href
        self.cover_image = cover_path if isinstance(cover_path, QImage) else None
        self.cover_path = "" if self.cover_image is not None else (cover_path or "")
        self._on_load = on_load
        self._button_text = button_text
        self.setFixedSize(180, 312)
        # The whole card is clickable, not just the button.
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(title)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        cover = QLabel()
        cover.setFixedSize(164, 200)
        cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self.cover_image is not None:
            from ui.styles import rounded_from_image
            pix = rounded_from_image(self.cover_image, 164, 200, 8)
        else:
            pix = (rounded_pixmap(self.cover_path, 164, 200, 8)
                   if (self.cover_path and os.path.exists(self.cover_path)) else None)
        if pix is not None:
            cover.setStyleSheet("background: transparent;")
            cover.setPixmap(pix)
        else:
            cover.setText("🎞️")
            cover.setStyleSheet("border-radius: 8px; background-color: #1e1e1e; color: #555555; font-size: 34px;")
        self.lbl_cover = cover
        layout.addWidget(cover)

        # Flag movies in the poster's top-left corner. A child of the cover label,
        # so set_cover() swapping the pixmap later leaves it in place.
        self.lbl_movie = None
        if is_movie_link(href):
            from ui.styles import add_movie_badge
            self.lbl_movie = add_movie_badge(cover)

        lbl = QLabel(title)
        lbl.setWordWrap(True)
        lbl.setFont(QFont("Segoe UI Variable", 9))
        lbl.setStyleSheet("color: #ffffff; background: transparent;")
        lbl.setFixedHeight(42)
        lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(lbl)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn = PrimaryPushButton(self._button_text)
        btn.setFixedHeight(34)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._trigger)
        btn_row.addWidget(btn, 1)

        self.btn_follow = None
        if followable:
            self.btn_follow = ToolButton(FIF.HEART)
            self.btn_follow.setFixedSize(34, 34)
            self.btn_follow.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_follow.setToolTip("Watch for new episodes")
            self.btn_follow.clicked.connect(
                lambda: self.follow.emit(self.title, self.href, self.cover()))
            btn_row.addWidget(self.btn_follow)

        layout.addLayout(btn_row)

    def set_cover(self, cover):
        """Show a cover, given either a decoded QImage or a path on disk.

        A QImage is the fast road and the one the search paths use: the worker that
        downloaded the poster already holds it decoded and scaled, so the GUI thread
        only paints. Passing a path means the GUI thread opens and decodes the file,
        which is what froze the window for up to 16 s -- kept only as a fallback for
        callers that genuinely have nothing but a path.
        """
        import os
        from PyQt6.QtGui import QImage
        from ui.styles import rounded_pixmap, rounded_from_image

        if isinstance(cover, QImage):
            self.cover_image = cover
            pix = rounded_from_image(cover, 164, 200, 8)
        else:
            self.cover_image = None
            self.cover_path = cover
            pix = (rounded_pixmap(cover, 164, 200, 8)
                   if (cover and os.path.exists(cover)) else None)
        if pix is not None:
            self.lbl_cover.setStyleSheet("background: transparent;")
            self.lbl_cover.setText("")
            self.lbl_cover.setPixmap(pix)

    def cover(self):
        """The cover as the card holds it: a QImage if it has one, else the path."""
        return self.cover_image if self.cover_image is not None else self.cover_path

    def set_followable(self, followable):
        if self.btn_follow is not None:
            self.btn_follow.setVisible(bool(followable))

    def _trigger(self):
        if self._on_load is not None:
            self._on_load()
        else:
            self.selected.emit(self.title, self.href, self.cover())

    def mouseReleaseEvent(self, event):
        # Click anywhere on the card (outside the button) also loads it.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self._trigger()
        super().mouseReleaseEvent(event)


class AnimeSearchWidget(QWidget):
    profile_created_signal = pyqtSignal(str)   # new profile name -> Downloader selects it
    follow_signal = pyqtSignal(str, str, str, object)  # (title, anime_url, domain, cover: QImage|path) -> Watchlist

    def __init__(self, parent=None):
        super().__init__(parent)
        # Hold strong refs to running QThreads so Python GC can't delete a
        # live thread's C++ object mid-run (which would crash the app).
        self._threads = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        title = QLabel("Anime Search & Discovery")
        title.setFont(QFont("Segoe UI Variable", 20, QFont.Weight.Bold))
        title.setStyleSheet("color: #ffffff; background: transparent;")
        root.addWidget(title)

        sub = QLabel("Search a website and load a show straight into a new download profile.")
        sub.setStyleSheet("color: #aaaaaa; background: transparent;")
        root.addWidget(sub)

        # --- Search bar row (fixed list of supported websites only) ---
        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.combo_domain = ComboBox()
        self.combo_domain.setFixedWidth(220)
        self.combo_domain.setFixedHeight(40)
        self.combo_domain.currentIndexChanged.connect(self._on_domain_changed)
        bar.addWidget(self.combo_domain)

        self.txt_query = LineEdit()
        self.txt_query.setPlaceholderText("Enter anime title to search...")
        self.txt_query.setFixedHeight(40)
        self.txt_query.returnPressed.connect(self.perform_search)
        bar.addWidget(self.txt_query, 1)

        self.btn_search = PrimaryPushButton("Search")
        self.btn_search.setFixedSize(120, 40)
        self.btn_search.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_search.clicked.connect(self.perform_search)
        bar.addWidget(self.btn_search)

        root.addLayout(bar)

        # A small count/instruction line shown above the results grid.
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #aaaaaa; background: transparent; font-size: 12px;")
        root.addWidget(self.lbl_status)

        # --- Results area ---
        self.scroll = SmoothScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        results_container = QWidget()
        results_container.setStyleSheet("background: transparent;")
        rc = QVBoxLayout(results_container)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(0)

        # Centered placeholder for idle / loading / empty / error states, so the user
        # always sees clear feedback in the middle of the area (not a tiny bottom line).
        self.placeholder = QWidget()
        self.placeholder.setStyleSheet("background: transparent;")
        ph = QVBoxLayout(self.placeholder)
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ph.setSpacing(10)
        self.spinner = IndeterminateProgressRing()
        self.spinner.setFixedSize(48, 48)
        ph.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignCenter)
        self.ph_icon = QLabel("")
        self.ph_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ph_icon.setStyleSheet("font-size: 40px; background: transparent;")
        ph.addWidget(self.ph_icon)
        self.ph_title = QLabel("")
        self.ph_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ph_title.setStyleSheet("color: #dddddd; font-size: 15px; font-weight: bold; background: transparent;")
        ph.addWidget(self.ph_title)
        self.ph_sub = QLabel("")
        self.ph_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ph_sub.setWordWrap(True)
        self.ph_sub.setStyleSheet("color: #888888; font-size: 12px; background: transparent;")
        ph.addWidget(self.ph_sub)
        rc.addWidget(self.placeholder, 1)

        self.grid_host = QWidget()
        self.grid_host.setStyleSheet("background: transparent;")
        from qfluentwidgets import FlowLayout
        self.grid = FlowLayout(self.grid_host)
        self.grid.setSpacing(14)
        self.grid.setContentsMargins(0, 0, 0, 0)
        # FlowLayout handles alignment internally based on flow direction
        rc.addWidget(self.grid_host)

        self.scroll.setWidget(results_container)
        root.addWidget(self.scroll, 1)

        # Pagination Navigator
        self.paginator_container = QWidget()
        self.paginator_container.setStyleSheet("background: transparent;")
        pg_layout = QHBoxLayout(self.paginator_container)
        pg_layout.setContentsMargins(0, 10, 0, 0)
        pg_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.btn_prev_page = PrimaryPushButton("‹ Previous")
        self.btn_prev_page.setFixedSize(100, 36)
        self.btn_prev_page.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_prev_page.clicked.connect(self._on_prev_page)

        self.lbl_page = QLabel("Page 1")
        self.lbl_page.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
        self.lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page.setFixedWidth(80)

        self.btn_next_page = PrimaryPushButton("Next ›")
        self.btn_next_page.setFixedSize(100, 36)
        self.btn_next_page.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_next_page.clicked.connect(self._on_next_page)

        # Left to right: Previous, page number, Next. The arrows and the order were
        # both reversed ("Previous >" on the right of "< Next"), which read as the
        # buttons doing the opposite of what they do.
        pg_layout.addWidget(self.btn_prev_page)
        pg_layout.addWidget(self.lbl_page)
        pg_layout.addWidget(self.btn_next_page)
        
        self.paginator_container.hide()
        root.addWidget(self.paginator_container)

        self._current_page = 1

        self.refresh_domains()
        self._show_state("🔍", "Search for an anime",
                         "Pick a website, type a title, and press Enter.")

    def _search_url_for(self, domain):
        return SUPPORTED_SITES.get(domain, f"https://{domain}/?s={{query}}")

    def _track(self, thread):
        """Keep a strong ref so GC can't delete a running thread's C++ object.

        isRunning() goes False as soon as run() returns, but the OS thread may not
        have finished unwinding. Dropping the last reference then lets Python collect
        the QThread while Qt still considers it alive, and the destructor blocks (or
        aborts) -- on the GUI thread, which is where a freeze would be felt. wait()
        on an already-finished thread returns at once, so joining first costs nothing
        and removes that race.
        """
        kept = []
        for t in self._threads:
            if t.isRunning():
                kept.append(t)
            else:
                try:
                    t.wait(100)
                except Exception:
                    pass
        self._threads = kept
        self._threads.append(thread)

    def showEvent(self, event):
        self.refresh_domains(select=self._current_domain() or None)
        super().showEvent(event)

    # ---- domains ----
    def refresh_domains(self, select=None):
        # Only the fixed supported websites are offered -- users can't add sites.
        # Each item shows the site's name and favicon, and carries the real domain as
        # its data: every caller reads the domain back through _current_domain(), so
        # what is displayed can change without touching the search or profile logic.
        self.combo_domain.blockSignals(True)
        self.combo_domain.clear()
        domains = list(SUPPORTED_SITES.keys())
        for domain in domains:
            self.combo_domain.addItem(site_display_name(domain),
                                      icon=site_icon(domain), userData=domain)
        if select and select in domains:
            self.combo_domain.setCurrentIndex(domains.index(select))
        self.combo_domain.blockSignals(False)
        self._sync_combo_icon()

    def _sync_combo_icon(self):
        """Show the selected site's favicon on the closed combo as well.

        ComboBox.setCurrentIndex only pushes the text, so without this the icon
        appears in the open list and vanishes the moment a site is picked.
        """
        self.combo_domain.setIcon(site_icon(self._current_domain()))

    def _current_domain(self):
        # currentData(), not currentText() -- the text is now the display name.
        return (self.combo_domain.currentData() or "").strip()

    def _on_domain_changed(self, _index):
        self._sync_combo_icon()
        # Switching website clears stale results but keeps the typed query, so the
        # user can re-run the same search on the other site.
        self.lbl_status.setText("")
        self._show_state("🔍", "Search for an anime",
                         "Pick a website, type a title, and press Enter.")

    # ---- search ----
    def perform_search(self):
        self._current_page = 1
        self._execute_search()

    def _execute_search(self):
        domain = self._current_domain()
        if not domain:
            InfoBar.warning("Website Required", "Select a website to search on first.",
                            position=InfoBarPosition.TOP, duration=4000, parent=self.window())
            return
        query = self.txt_query.text().strip()
        if not query:
            self._show_state("⌨️", "Type an anime title", "Enter a name above, then press Enter.")
            self.txt_query.setFocus()
            self.paginator_container.hide()
            return

        template = self._search_url_for(domain)

        self.btn_search.setEnabled(False)
        self.btn_prev_page.setEnabled(False)
        self.btn_next_page.setEnabled(False)
        self.lbl_status.setText("")
        self._show_state("", f"Searching {site_display_name(domain)}…",
                         f"Looking for “{query}” (Page {self._current_page}).", busy=True)

        for t in self._threads:
            t.requestInterruption()

        # Only the newest request may touch the grid. Interruption is only a request
        # -- a worker already past its last check still delivers -- so pressing Enter
        # again (it works while the button is disabled) used to show both queries'
        # results mixed in one grid, whichever finished last appended on top.
        gen = self._next_generation()
        th = AnimeSearchThread(query, template, page=self._current_page)
        th.finished.connect(lambda r, g=gen: self._is_current(g) and self.on_search_finished(r))
        th.error.connect(lambda m, g=gen: self._is_current(g) and self.on_search_error(m))
        if hasattr(th, 'cover_loaded'):
            th.cover_loaded.connect(
                lambda link, c, g=gen: self._is_current(g) and self.on_cover_loaded(link, c))
        self._track(th)
        th.start()

    def _next_generation(self):
        """Start a new grid owner; anything tagged with an older number is ignored."""
        self._grid_gen = getattr(self, "_grid_gen", 0) + 1
        self._pending_covers = {}
        return self._grid_gen

    def _is_current(self, gen):
        return gen == getattr(self, "_grid_gen", 0)

    def _on_prev_page(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._execute_search()

    def _on_next_page(self):
        self._current_page += 1
        self._execute_search()

    def on_search_finished(self, results):
        self.btn_search.setEnabled(True)
        self.btn_prev_page.setEnabled(self._current_page > 1)
        self.lbl_page.setText(f"Page {self._current_page}")
        
        if not results:
            self.lbl_status.setText("")
            if self._current_page > 1:
                self._show_state("😕", "No more results", "You've reached the end of the results.")
            else:
                self._show_state("😕", "No results", "Try a different title, or switch website above.")
            self.btn_next_page.setEnabled(False)
            self.paginator_container.show()
            return
            
        domain = self._current_domain()
        max_per_page = 24 if "witanime" in domain else 20
        self.btn_next_page.setEnabled(len(results) >= max_per_page)
        
        self.paginator_container.show()
        
        # Completely reveal the grid UI
        self.spinner.setVisible(False)
        self.placeholder.hide()
        self.grid_host.show()
        
        n = len(results)
        self.lbl_status.setText(f"{n} result{'s' if n != 1 else ''} on this page · click a card to load it.")
        
        # Start from an empty grid, whatever was there. Belt and braces with the
        # generation check: results are only ever appended below.
        pending = getattr(self, "_pending_covers", {})
        self.clear_grid()
        self._pending_covers = pending
        self._cards = {}

        # Add all cards synchronously in one massive layout update.
        # This completely fixes the UI unresponsiveness! FlowLayout recalculates the entire
        # grid position map O(N^2) every time a widget is added. By adding them synchronously
        # during one turn of the event loop, FlowLayout calculates ONCE, rather than calculating
        # 24 separate times spread over 5 seconds across timers!
        self.grid.setEnabled(False)
        for r in results:
            card = AnimeResultCard(clean_title(r["title"]), r["link"], r["cover"], followable=True)
            self._cards[r["link"]] = card
            
            # A cover that arrived before this card was built is waiting for it.
            early = self._pending_covers.pop(r["link"], "")
            if early:
                card.set_cover(early)
                
            card.selected.connect(self.on_result_selected)
            card.follow.connect(self._on_follow)
            self.grid.addWidget(card)
        self.grid.setEnabled(True)

    def on_cover_loaded(self, link, cover):
        """`cover` is a decoded QImage from the worker (or, rarely, a path)."""
        card = getattr(self, "_cards", {}).get(link)
        if card is not None:
            card.set_cover(cover)
        else:
            # Card not rendered yet -- remember it.
            if hasattr(self, "_pending_covers"):
                self._pending_covers[link] = cover

    def on_search_error(self, msg):
        self.btn_search.setEnabled(True)
        self.btn_prev_page.setEnabled(self._current_page > 1)
        self.btn_next_page.setEnabled(False)
        # The failed search has no pages, so leaving the previous one's navigator up
        # (still reading "Page 3") invites paging through results that are not there.
        self.paginator_container.hide()
        self.lbl_status.setText("")
        self._show_state("⚠️", "Search failed",
                         friendly_browser_error(msg, self._current_domain()))

    def _on_follow(self, title, href, cover):
        # Hand off to the Watchlist tab (via the main window), using the current site.
        self.follow_signal.emit(title, href, self._current_domain(), cover)

    # ---- handoff ----
    def on_result_selected(self, title, href, cover_path=""):
        self.lbl_status.setText("")
        self._show_state("", f"Loading “{title}”…", "Detecting episodes and seasons…", busy=True)
        self._pending_title = title
        self._pending_cover = cover_path
        self._pending_href = href     # season cards follow the parent anime page
        # Loading an anime takes over the grid too: a search still running must not
        # drop its cards on top of the season list, nor this load on a new search.
        for t in self._threads:
            t.requestInterruption()
        gen = self._next_generation()
        th = AnimeDetailsThread(href)
        th.finished.connect(lambda e, g=gen: self._is_current(g) and self.on_details_finished(e))
        th.error.connect(lambda m, g=gen: self._is_current(g) and self.on_details_error(m))
        self._track(th)
        th.start()

    def on_details_finished(self, entries):
        title = getattr(self, "_pending_title", "Anime")
        if not entries:
            self.on_details_error("No episodes detected.")
            return
        if len(entries) == 1:
            e = entries[0]
            name = self._create_profile(title, e["template"], e.get("max_ep", 1))
            self._go_to_profile(name, f"'{name}' created with {e.get('max_ep', 0)} episode(s). Head to the Downloader.")
            return
        # Multiple seasons -> show each as its own result card to pick from.
        self._show_season_cards(title, entries)

    def _show_season_cards(self, title, entries):
        self.clear_grid()
        self._show_results()
        self.lbl_status.setText(f"'{title}' has {len(entries)} seasons — pick one to load.")
        default_cover = getattr(self, "_pending_cover", "")
        parent_href = getattr(self, "_pending_href", "")
        
        self.grid.setEnabled(False)
        for i, e in enumerate(entries):
            label = e.get("label") or f"Season {i + 1}"
            mx = e.get("max_ep", 1)
            name = f"{title} - {label}"
            cover = e.get("cover") or default_cover   # season's own poster, else the anime's
            card = AnimeResultCard(f"{label}  ·  {mx} eps", parent_href, cover,
                                   button_text="Load Season", followable=True,
                                   on_load=lambda n=name, tm=e["template"], m=mx: self._load_season(n, tm, m))
            # Following a season still follows the parent anime -- that is the page
            # the watcher re-reads each check -- so pass the show's real title/URL.
            card.follow.connect(lambda _t, _h, _c, t=title, h=parent_href, cv=cover:
                                self._on_follow(t, h, cv))
            self.grid.addWidget(card)
        self.grid.setEnabled(True)

    def _load_season(self, name, template, max_ep):
        created = self._create_profile(name, template, max_ep)
        self._go_to_profile(created, f"'{created}' created with {max_ep} episode(s). Head to the Downloader.")

    def _go_to_profile(self, name, msg):
        with config_lock:
            app_settings["last_profile"] = name
        save_config()
        # Reset the results area so re-opening Search never shows a stuck spinner.
        self.lbl_status.setText("")
        self._show_state("✅", "Profile created",
                         f"'{name}' opened in the Downloader. Search again anytime.")
        InfoBar.success("Profile Created", msg,
                        position=InfoBarPosition.TOP, duration=6000, parent=self.window())
        self.profile_created_signal.emit(name)

    def on_details_error(self, msg):
        friendly = friendly_browser_error(msg, self._current_domain())
        self.lbl_status.setText("")
        self._show_state("⚠️", "Couldn't load this title", friendly)
        InfoBar.error("Detection Failed", friendly,
                      position=InfoBarPosition.TOP, duration=5000, parent=self.window())

    def _create_profile(self, name, url_template, max_ep, domain=None):
        if domain is None:
            domain = self._current_domain()
        # Give the new profile a working download click-flow (inherit the best
        # same-domain profile, else a built-in default).
        inherited_paths, inherited_next = resolve_site_flow(domain)
        with config_lock:
            # Sanitize + de-duplicate the profile name.
            base = re.sub(r'[\\/:*?"<>|]', "", name).strip() or "Anime"
            final = base
            n = 2
            while final in sites_data:
                final = f"{base} ({n})"
                n += 1

            sites_data[final] = {
                "url": url_template,
                "next_btn_xpath": inherited_next,
                "step_paths": inherited_paths,
                "last_episodes": f"1-{max_ep}" if max_ep > 1 else "1",
            }
        save_config()
        return final

    def _show_state(self, icon, title, subtitle="", busy=False):
        """Show the centered placeholder (idle/loading/empty/error); hide the grid."""
        self.clear_grid()
        self.grid_host.hide()
        self.spinner.setVisible(busy)
        self.ph_icon.setText("" if busy else icon)
        self.ph_icon.setVisible(not busy)
        self.ph_title.setText(title)
        self.ph_sub.setText(subtitle)
        self.ph_sub.setVisible(bool(subtitle))
        self.placeholder.show()

    def _show_results(self):
        """Reveal the results grid and hide the placeholder/spinner."""
        self.spinner.setVisible(False)
        self.placeholder.hide()
        self.grid_host.show()

    def clear_grid(self):
        # Drop references so late cover_loaded signals from the previous search can't
        # touch cards that are being torn down.
        self._cards = {}
        self._pending_covers = {}
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item is None:
                break
            # FlowLayout returns QWidget directly, QLayout returns QLayoutItem
            w = item if hasattr(item, "deleteLater") else item.widget()
            if w:
                w.deleteLater()
