"""Refresh the bundled favicons shown in the Search tab's website dropdown.

Run deliberately (not part of the build), and commit whatever it writes:

    py tools/fetch_site_icons.py
    py tools/fetch_site_icons.py witanime.life      # just one site

The icons are committed rather than downloaded at runtime for two reasons:

  1. Plain HTTP from Python does not work. Both supported sites reset the
     connection on a urllib request ("[WinError 10054] An existing connection was
     forcibly closed"), so only a real browser can fetch them -- and launching
     headless Chrome to draw a 16px icon on a dropdown would be absurd.
  2. The dropdown is built during startup. Anything on that path that touches the
     network delays the window appearing, and would leave the icons missing
     entirely for offline users.

Adding a site to SUPPORTED_SITES therefore means running this once. A site with no
icon file falls back to a generic globe, so forgetting only costs the picture.
"""

import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# Redirect the data dir BEFORE importing anything from the app, so a maintainer run
# can never touch the real config or browser profile.
os.environ.setdefault("AED_APP_DIR", os.path.join(REPO, "build", "iconfetch-appdir"))
os.makedirs(os.environ["AED_APP_DIR"], exist_ok=True)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.search_tab import SUPPORTED_SITES, site_icon_path                # noqa: E402

# Saved larger than it is drawn (menu icons are ~16px) so the same file stays sharp
# on a 200% display without shipping a second asset.
ICON_PX = 64

# Collect every icon the page declares, plus the conventional path as a backstop,
# and hand back each one base64'd. Chrome is already on the origin, so these are
# same-origin fetches -- no CORS, and the site's own anti-bot checks are satisfied.
_FETCH_ICONS_JS = r"""
var callback = arguments[arguments.length - 1];
(async function () {
    var out = [];
    var seen = {};
    var sel = 'link[rel~="icon"], link[rel="shortcut icon"], link[rel="apple-touch-icon"],'
            + ' link[rel="apple-touch-icon-precomposed"], link[rel="mask-icon"]';
    var urls = Array.prototype.map.call(document.querySelectorAll(sel),
                                        function (l) { return l.href; });
    urls.push(new URL('/favicon.ico', location.origin).href);
    for (var i = 0; i < urls.length; i++) {
        var u = urls[i];
        if (!u || seen[u]) continue;
        seen[u] = 1;
        try {
            var r = await fetch(u, { credentials: 'omit' });
            if (!r.ok) continue;
            var b = await r.blob();
            if (!b.size) continue;
            var d = await new Promise(function (res) {
                var fr = new FileReader();
                fr.onload = function () { res(fr.result); };
                fr.onerror = function () { res(''); };
                fr.readAsDataURL(b);
            });
            if (d) out.push([u, d]);
        } catch (e) { /* try the next candidate */ }
    }
    callback(out);
})();
"""


def _decode(data_url):
    """data: URL -> a square RGBA PIL image, or None if it isn't a usable raster."""
    import base64
    from PIL import Image
    if not isinstance(data_url, str) or "," not in data_url:
        return None
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(raw))
        # .ico files hold several sizes; Pillow opens the largest by default only
        # after a load(), and SVG (mask-icon) fails here -- which is what we want.
        img.load()
        return img.convert("RGBA")
    except Exception:
        return None


def fetch_one(driver, domain):
    driver.get(f"https://{domain}/")
    candidates = driver.execute_async_script(_FETCH_ICONS_JS) or []

    best, best_url, best_edge = None, "", 0
    for entry in candidates:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        url, data_url = entry
        img = _decode(data_url)
        if img is None:
            continue
        edge = max(img.size)
        # Biggest wins: a 180px apple-touch-icon downscales to a clean 64px, while a
        # 16px favicon upscaled to 64 is a blurry mess.
        if edge > best_edge:
            best, best_url, best_edge = img, url, edge

    if best is None:
        print(f"  {domain}: no usable icon found ({len(candidates)} candidates)")
        return False

    from PIL import Image
    icon = best.resize((ICON_PX, ICON_PX), Image.LANCZOS)
    dest = site_icon_path(domain, must_exist=False)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    icon.save(dest, "PNG")
    print(f"  {domain}: {best_edge}px from {best_url} -> {os.path.relpath(dest, REPO)}")
    return True


def main():
    wanted = sys.argv[1:] or list(SUPPORTED_SITES)
    unknown = [d for d in wanted if d not in SUPPORTED_SITES]
    if unknown:
        sys.exit(f"not a supported site: {', '.join(unknown)}")

    from ui.search_tab import _make_headless_driver
    driver = _make_headless_driver()
    driver.set_script_timeout(60)
    failed = []
    try:
        for domain in wanted:
            print(f"fetching {domain}")
            try:
                if not fetch_one(driver, domain):
                    failed.append(domain)
            except Exception as e:
                print(f"  {domain}: {type(e).__name__}: {e}")
                failed.append(domain)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if failed:
        sys.exit(f"failed: {', '.join(failed)}")
    print("done")


if __name__ == "__main__":
    main()
