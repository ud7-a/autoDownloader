import os
import re
import time
import base64
import tempfile
import threading
from collections import defaultdict
from urllib.parse import urlparse, quote, unquote

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel)
from qfluentwidgets import (LineEdit, PrimaryPushButton, ComboBox, ToolButton,
                            SimpleCardWidget, SmoothScrollArea, FluentIcon as FIF,
                            InfoBar, InfoBarPosition, IndeterminateProgressRing)

from utils.config import app_settings, sites_data, save_config, config_lock
from ui.styles import rounded_pixmap

# Fixed list of supported websites (domain -> search URL template). The Search tab
# only allows these; users cannot add arbitrary sites.
SUPPORTED_SITES = {
    "witanime.life": "https://witanime.life/?search_param=animes&s={query}",
    "eta.animerco.org": "https://eta.animerco.org/?s={query}",
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


def _make_headless_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from subprocess import CREATE_NO_WINDOW

    options = webdriver.ChromeOptions()
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
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
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
    """Keep the cover cache bounded -- drop the oldest files beyond max_files."""
    try:
        files = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir)]
        files = [f for f in files if os.path.isfile(f)]
        if len(files) <= max_files:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for f in files[:len(files) - max_files]:
            try: os.remove(f)
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
    let words = query.toLowerCase().split(/\s+/).filter(w => w.length > 1);
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
            if (!cover) {
                let b = bgUrl(sc);
                if (b) cover = b;
                else { for (let e of sc.querySelectorAll('*')) { let u = bgUrl(e); if (u) { cover = u; break; } } }
            }
            if (cover && (title || altTitle)) break;
        }
        let finalTitle = title || altTitle;
        if (finalTitle && matches(finalTitle)) {
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
var callback = arguments[1];
Promise.all(srcs.map(function(src) {
    return fetch(src)
        .then(function(r) { return r.blob(); })
        .then(function(b) {
            return new Promise(function(res) {
                var fr = new FileReader();
                fr.onloadend = function() { res(fr.result); };
                fr.onerror = function() { res(''); };
                fr.readAsDataURL(b);
            });
        })
        .catch(function() { return ''; });
})).then(function(results) { callback(results); });
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


def download_covers(driver, img_urls, cache_dir=None):
    """Download image URLs (from the current same-origin page) to local cache files.

    Returns a list of local paths aligned with img_urls ("" for any that failed or
    were empty). Cached by MD5 of the URL, batch-fetched in one round-trip.
    """
    import hashlib
    if cache_dir is None:
        cache_dir = _covers_cache_dir()
    _prune_cover_cache(cache_dir)

    paths = [""] * len(img_urls)
    to_fetch = []  # (index, url, cache_path)
    for i, url in enumerate(img_urls):
        url = _full_res((url or "").strip())
        if not url or url.startswith("data:"):
            continue
        key = hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]
        p = os.path.join(cache_dir, f"{key}.img")
        if os.path.exists(p) and os.path.getsize(p) > 500:
            paths[i] = p
        else:
            to_fetch.append((i, url, p))

    if to_fetch:
        try:
            data_urls = driver.execute_async_script(_FETCH_IMGS_JS, [f[1] for f in to_fetch]) or []
        except Exception:
            data_urls = []
        for j, (i, _url, p) in enumerate(to_fetch):
            du = data_urls[j] if j < len(data_urls) else ""
            if isinstance(du, str) and du.startswith("data:image"):
                try:
                    with open(p, "wb") as f:
                        f.write(base64.b64decode(du.split(",", 1)[1]))
                    paths[i] = p
                except Exception:
                    pass
    return paths


class AnimeSearchThread(QThread):
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, query, search_url_template):
        super().__init__()
        self.query = query
        self.search_url_template = search_url_template

    def _poll_heuristic(self, driver, timeout, done):
        """Repeatedly run the detector until `done(results)` is true or `timeout`
        elapses; returns the latest results (or an error string). Lets a fast page
        return in a fraction of a second instead of waiting a fixed time."""
        end = time.time() + timeout
        last = []
        while time.time() < end:
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
        try:
            driver = acquire_driver()
        except Exception as e:
            self.error.emit(str(e))
            return
        try:
            search_url = self.search_url_template.replace("{query}", quote(self.query))
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

            # Resolve covers: cached file if present, else batch-fetch in parallel.
            covers = download_covers(driver, [it["img"] for it in items])
            for it, cov in zip(items, covers):
                it["cover"] = cov

            self.finished.emit([{"title": it["title"], "link": it["link"], "cover": it["cover"]} for it in items])
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
        else:
            self.error.emit("Could not detect episodes for this title.")

    def detect_entries(self, anime_url, want_covers=True, driver=None):
        """Synchronous episode detection for an anime page. Returns a list of
        entries [{"label","template","max_ep","poster","cover"}] (one per season,
        or a single flat entry), or []. Reused by search and the new-episode watcher.

        Pass an explicit `driver` to run on a caller-owned browser (the watcher's
        parallel pool); otherwise the shared search driver is acquired/released.
        """
        # _find_season_links derives the base host from self.anime_url, so keep it in
        # sync (the watcher constructs this thread with an empty url and passes it here).
        self.anime_url = anime_url
        own = driver is None
        if own:
            driver = acquire_driver()
        try:
            driver.get(anime_url)

            # Seasons-based site FIRST: an anime page often shows a "latest episodes"
            # widget for OTHER shows, which would otherwise be grabbed as this anime's
            # episodes. Real season pages are the correct source. Smart wait: content
            # is server-rendered, so poll until seasons appear OR flat episodes are
            # already derivable, returning in a fraction of a second.
            seasons = []
            end = time.time() + 5.0
            while time.time() < end:
                seasons = self._find_season_links(driver)
                if seasons:
                    break
                t, _ = self._derive_page(driver)
                if t:      # flat page already lists episodes -> handle below
                    break
                time.sleep(0.2)

            if seasons:
                entries = []
                for label, url, poster in seasons[:25]:
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
                    return entries

            # Flat site: episodes listed directly on the anime page (e.g. witanime).
            template, max_ep = self._derive_ready(driver, timeout=4.0)
            if template:
                return [{"label": "", "template": template, "max_ep": max_ep,
                         "poster": "", "cover": ""}]
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

    def _derive_page(self, driver):
        """Derive (template, max_ep) from the currently-loaded page, or ('', 0)."""
        from selenium.webdriver.common.by import By
        hrefs, onclicks = [], []
        for a in driver.find_elements(By.TAG_NAME, "a"):
            try:
                h = a.get_attribute("href")
                if h:
                    hrefs.append(h)
                oc = a.get_attribute("onclick")
                if oc:
                    onclicks.append(oc)
            except Exception:
                continue
        # Prefer the openEpisode/onclick signal; fall back to href-number grouping.
        template, max_ep = self._derive_from_onclick(driver, onclicks)
        if not template:
            template, max_ep = self._derive_from_hrefs(hrefs)
        return (template, max_ep) if template else ("", 0)

    def _find_season_links(self, driver):
        """Same-domain season pages (/seasons/<slug>/) linked from the anime page,
        each with its own label and (lazy-loaded) poster URL.

        Returns a list of (label, url, poster_url). poster_url may be "" if none
        was found; the real image is a data-src attribute (the visible
        background-image is a loading-spinner placeholder until scrolled into view).
        """
        from selenium.webdriver.common.by import By
        base = urlparse(self.anime_url).netloc
        by_url, order = {}, []
        for a in driver.find_elements(By.TAG_NAME, "a"):
            try:
                h = a.get_attribute("href") or ""
                txt = (a.text or "").strip()
                title_attr = a.get_attribute("title") or ""
                poster = a.get_attribute("data-src") or ""
            except Exception:
                continue
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
                try:
                    img = a.find_element(By.TAG_NAME, "img")
                    poster = img.get_attribute("data-src") or img.get_attribute("src") or ""
                except Exception:
                    poster = ""
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
    selected = pyqtSignal(str, str, str)   # (title, href, cover_path)

    follow = pyqtSignal(str, str, str)   # (title, href, cover_path)

    def __init__(self, title, href, cover_path, parent=None, on_load=None,
                 button_text="Load Anime", followable=False):
        super().__init__(parent)
        self.title = title
        self.href = href
        self.cover_path = cover_path
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
        pix = rounded_pixmap(cover_path, 164, 200, 8) if (cover_path and os.path.exists(cover_path)) else None
        if pix is not None:
            cover.setStyleSheet("background: transparent;")
            cover.setPixmap(pix)
        else:
            cover.setText("🎞️")
            cover.setStyleSheet("border-radius: 8px; background-color: #1e1e1e; color: #555555; font-size: 34px;")
        layout.addWidget(cover)

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

        if followable:
            btn_follow = ToolButton(FIF.HEART)
            btn_follow.setFixedSize(34, 34)
            btn_follow.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_follow.setToolTip("Watch for new episodes")
            btn_follow.clicked.connect(lambda: self.follow.emit(self.title, self.href, self.cover_path))
            btn_row.addWidget(btn_follow)

        layout.addLayout(btn_row)

    def _trigger(self):
        if self._on_load is not None:
            self._on_load()
        else:
            self.selected.emit(self.title, self.href, self.cover_path)

    def mouseReleaseEvent(self, event):
        # Click anywhere on the card (outside the button) also loads it.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self._trigger()
        super().mouseReleaseEvent(event)


class AnimeSearchWidget(QWidget):
    profile_created_signal = pyqtSignal(str)   # new profile name -> Downloader selects it
    follow_signal = pyqtSignal(str, str, str, str)  # (title, anime_url, domain, cover) -> Watchlist

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
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(14)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        rc.addWidget(self.grid_host)

        self.scroll.setWidget(results_container)
        root.addWidget(self.scroll, 1)

        self.refresh_domains()
        self._show_state("🔍", "Search for an anime",
                         "Pick a website, type a title, and press Enter.")

    def _search_url_for(self, domain):
        return SUPPORTED_SITES.get(domain, f"https://{domain}/?s={{query}}")

    def _track(self, thread):
        """Keep a strong ref so GC can't delete a running thread's C++ object.
        Prune only fully-finished threads (isRunning() False == run() returned,
        finally-block included), so we never drop a live thread mid-run."""
        self._threads = [t for t in self._threads if t.isRunning()]
        self._threads.append(thread)

    def showEvent(self, event):
        self.refresh_domains(select=self._current_domain() or None)
        super().showEvent(event)

    # ---- domains ----
    def refresh_domains(self, select=None):
        # Only the fixed supported websites are offered -- users can't add sites.
        self.combo_domain.blockSignals(True)
        self.combo_domain.clear()
        domains = list(SUPPORTED_SITES.keys())
        self.combo_domain.addItems(domains)
        if select and select in domains:
            self.combo_domain.setCurrentText(select)
        self.combo_domain.blockSignals(False)

    def _current_domain(self):
        return self.combo_domain.currentText().strip()

    def _on_domain_changed(self, _index):
        # Switching website clears stale results but keeps the typed query, so the
        # user can re-run the same search on the other site.
        self.lbl_status.setText("")
        self._show_state("🔍", "Search for an anime",
                         "Pick a website, type a title, and press Enter.")

    # ---- search ----
    def perform_search(self):
        domain = self._current_domain()
        if not domain:
            InfoBar.warning("Website Required", "Select a website to search on first.",
                            position=InfoBarPosition.TOP, duration=4000, parent=self.window())
            return
        query = self.txt_query.text().strip()
        if not query:
            self._show_state("⌨️", "Type an anime title", "Enter a name above, then press Enter.")
            self.txt_query.setFocus()
            return

        template = self._search_url_for(domain)   # fixed URL for the supported site

        self.btn_search.setEnabled(False)
        self.lbl_status.setText("")
        self._show_state("", f"Searching {domain}…", f"Looking for “{query}”.", busy=True)

        th = AnimeSearchThread(query, template)
        th.finished.connect(self.on_search_finished)
        th.error.connect(self.on_search_error)
        self._track(th)
        th.start()

    def on_search_finished(self, results):
        self.btn_search.setEnabled(True)
        if not results:
            self.lbl_status.setText("")
            self._show_state("😕", "No results",
                             "Try a different title, or switch website above.")
            return
        self._show_results()
        n = len(results)
        self.lbl_status.setText(f"{n} result{'s' if n != 1 else ''} · click a card to load it.")
        cols = 5
        for i, r in enumerate(results):
            card = AnimeResultCard(r["title"], r["link"], r["cover"], followable=True)
            card.selected.connect(self.on_result_selected)
            card.follow.connect(self._on_follow)
            self.grid.addWidget(card, i // cols, i % cols)

    def on_search_error(self, msg):
        self.btn_search.setEnabled(True)
        self.lbl_status.setText("")
        self._show_state("⚠️", "Search failed", msg)

    def _on_follow(self, title, href, cover):
        # Hand off to the Watchlist tab (via the main window), using the current site.
        self.follow_signal.emit(title, href, self._current_domain(), cover)

    # ---- handoff ----
    def on_result_selected(self, title, href, cover_path=""):
        self.lbl_status.setText("")
        self._show_state("", f"Loading “{title}”…", "Detecting episodes and seasons…", busy=True)
        self._pending_title = title
        self._pending_cover = cover_path
        th = AnimeDetailsThread(href)
        th.finished.connect(self.on_details_finished)
        th.error.connect(self.on_details_error)
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
        self._show_results()
        self.lbl_status.setText(f"'{title}' has {len(entries)} seasons — pick one to load.")
        default_cover = getattr(self, "_pending_cover", "")
        cols = 5
        for i, e in enumerate(entries):
            label = e.get("label") or f"Season {i + 1}"
            mx = e.get("max_ep", 1)
            name = f"{title} - {label}"
            cover = e.get("cover") or default_cover   # season's own poster, else the anime's
            card = AnimeResultCard(f"{label}  ·  {mx} eps", "", cover, button_text="Load Season",
                                   on_load=lambda n=name, tm=e["template"], m=mx: self._load_season(n, tm, m))
            self.grid.addWidget(card, i // cols, i % cols)

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
        self.lbl_status.setText("")
        self._show_state("⚠️", "Couldn't load this title", msg)
        InfoBar.error("Detection Failed", msg,
                      position=InfoBarPosition.TOP, duration=5000, parent=self.window())

    def _create_profile(self, name, url_template, max_ep, domain=None):
        if domain is None:
            domain = self._current_domain()
        # Inherit the click-flow from an existing profile on the same domain, if any.
        inherited_paths, inherited_next = {}, ""
        with config_lock:
            for cfg in sites_data.values():
                if extract_domain(cfg.get("url", "")) == domain:
                    inherited_paths = cfg.get("step_paths", {})
                    inherited_next = cfg.get("next_btn_xpath", "")
                    break

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
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
