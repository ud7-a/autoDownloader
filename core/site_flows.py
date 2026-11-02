"""Built-in download flows per site, shared by Search (new profiles), the engine's
callers, the config migration and the tests -- one copy, so a fix here reaches
every user of it. Pure data and helpers: no Qt, no Selenium, safe to import early.
"""
import hashlib
import json


def site_of(host):
    """The registrable part of a host name: a3.mp4upload.com -> mp4upload.com."""
    return ".".join(host.split(".")[-2:]) if host else ""


DEFAULT_SITE_FLOWS = {
    # Taken from a hand-tuned working profile (tests/fixtures/witanime_template.json,
    # exported from the app). Order is the fallback order: Google Drive first, then
    # Mediafire, witanime's own wtsrv mirror, Workupload, mp4upload, gofile, wahmi.
    # Existing profiles still on an earlier shipped version of this flow are moved
    # to it once (utils/config._update_witanime_template, RETIRED_FLOWS below).
    "witanime.site": {
        "next_btn_xpath": "الحلقة التالية",
        "step_paths": {
            "FHD - Google": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 2.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'google')]", "delay": 3.0},
                {"xpath": "Download anyway", "delay": 2.0},
            ],
            "FHD - Mediafire": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mediafire')]", "delay": 5.0},
                {"xpath": '//*[@id="downloadButton"]', "delay": 2.0},
            ],
            # An episode can offer wtsrv twice. The page is right-to-left and XPath
            # counts in source order, so [last()] is the button furthest LEFT on
            # screen -- checked on Yuusha Party ep 1: wtsrv, mp4upload, wtsrv laid
            # out right to left, and [last()] picked the left one. With a single
            # wtsrv button [last()] is simply that button.
            "FHD - wtsrv": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "(//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'wtsrv')])[last()]", "delay": 5.0},
                {"xpath": '//*[@id="downloadButton"]', "delay": 2.0},
            ],
            "FHD - Workupload": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 2.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'workupload')]", "delay": 5.0},
                {"xpath": '//*[@id="file"]/div[3]/div/a', "delay": 2.0},
            ],
            "FHD - mp4upload": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 2.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'mp4upload')]", "delay": 5.0},
                {"script": "if (window.location.href.includes('mp4upload.com') && !window.location.href.includes('embed-')) { window.location.href = window.location.href.replace(/mp4upload\\.com\\/([a-zA-Z0-9]+)/, 'mp4upload.com/embed-$1.html'); } return null;", "delay": 3.0},
                {"script": "let src = null; let v = document.querySelector('video'); if (v && v.src && !v.src.startsWith('blob:')) return v.src; let scripts = document.querySelectorAll('script'); for (let s of scripts) { let match = s.innerHTML.match(/eval\\((function\\(p,a,c,k,e,d\\)[\\s\\S]*?split\\('\\|'\\).*?)\\)/); if (match) { try { let unpacked = eval(match[1]); let m = unpacked.match(/player\\.src\\(\\\"([^\\\"]+)\\\"\\)/) || unpacked.match(/src:\\\"([^\\\"]+)\\\"/); if (m) src = m[1]; } catch(e) {} } } return src;", "delay": 2.0}
            ],
            # gofile opens in the same tab (no popup) on a folder page holding the
            # episode; its Download button is the only [data-action=download] there.
            "FHD - gofile": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'gofile')]", "delay": 5.0},
                {"xpath": "//button[@data-action='download']", "delay": 2.0},
            ],
            # wahmi has a 60s client-side cooldown countdown on its page, but its
            # backend API (/download/create) does not enforce the wait: an immediate
            # POST with the page's CSRF token returns the direct .mp4 download URL
            # instantly, completely bypassing the 60s cooldown in <1 second.
            "FHD - wahmi": [
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//button[contains(., 'FHD')]", "delay": 1.0},
                {"xpath": "//h2[contains(text(), 'تحميل')]/following-sibling::div//div[contains(@class, 'rounded-xl') and .//button[contains(., 'FHD')]]//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'wahmi')]", "delay": 4.0},
                {"script": "let csrf = document.querySelector('meta[name=\"csrf-token\"]')?.getAttribute('content'); let dlId = (typeof downloadId !== 'undefined' ? downloadId : null) || window.location.pathname.split('/')[1]; let base = (typeof config !== 'undefined' && config.baseURL ? config.baseURL : window.location.origin); let url = base + '/' + dlId + '/download/create'; return (async () => { try { let resp = await fetch(url, { method: 'POST', headers: { 'X-CSRF-TOKEN': csrf, 'Content-Type': 'application/json', 'Accept': 'application/json' } }); let data = await resp.json(); if (data && data.download_link) return data.download_link; } catch(e) {} let a = document.querySelector('a.download-link'); if (a && a.href) return a.href; return null; })();", "delay": 2.0},
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


def _clean(value):
    """A flow as compared: strings without stray spaces (a trailing space typed into
    an xpath field), numbers as floats (2 and 2.0 are one delay)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k).strip(): _clean(v) for k, v in value.items()}
    return value


def flow_fingerprint(step_paths):
    """Identifies a list of download paths: the same paths, in the same order (the
    order is the fallback order, so a user's reordering is an edit), with the same
    steps give the same fingerprint, whatever stray spaces or 2 vs 2.0."""
    pairs = [[str(name).strip(), _clean(steps)] for name, steps in step_paths.items()]
    text = json.dumps(pairs, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# Fingerprints of every witanime flow the app has shipped before the current one,
# each also with the "FHD - mp4upload" and "FHD - wahmi" paths that one-time
# migrations appended to existing profiles (at the end, in that order). A profile
# matching one of these was
# never edited by its user, so it is safe to move to the current flow; any other
# profile was edited and is left alone. Worked out from the flows in git history;
# when the flow changes again, the one it replaces is added here.
RETIRED_FLOWS = {
    "witanime.site": frozenset({
        "5172d12e8549e2fe",   # 2026-10-10 (ba06e51): Mediafire first, "FHD - Google Drive"
        "3303fb8c5c5e7e6d",   # 2026-09-28 (cb5273b)
        "85b2682491204221",   # 2026-09-28 (cb5273b) + mp4upload
        "2bd4231a9d0df5f0",   # 2026-09-28 (cb5273b) + wahmi
        "bfe9feaa7bec1678",   # 2026-09-28 (cb5273b) + mp4upload + wahmi
        "f07b524e46429600",   # 2026-09-23 (04f048d)
        "30d2e40bee75388d",   # 2026-09-23 (04f048d) + mp4upload
        "0d0c4c8f94664e7c",   # 2026-09-23 (04f048d) + wahmi
        "86f16e8655a971e1",   # 2026-09-23 (04f048d) + mp4upload + wahmi
        "558a85ed4ec212ba",   # 2026-09-04 (88f84a6): the old page, lowercase path names
        "861fe28138cad0e8",   # 2026-09-04 (88f84a6) + mp4upload
        "4017011fafcd8910",   # 2026-09-04 (88f84a6) + wahmi
        "0d74d601936d72a9",   # 2026-09-04 (88f84a6) + mp4upload + wahmi
        "e34e2074ab83aadc",   # 2026-08-18 (59734b0)
        "e2b79b41058f7ba0",   # 2026-08-18 (59734b0) + mp4upload
        "50a58f04dcbcc18d",   # 2026-08-18 (59734b0) + wahmi
        "4d558908b378e083",   # 2026-08-18 (59734b0) + mp4upload + wahmi
        "fd1d4eb673e88d8b",   # 2026-07-25 (6a2eaa7)
        "3082ac87d8205e6f",   # 2026-07-25 (6a2eaa7) + mp4upload
        "d0057348ae7b45a7",   # 2026-07-25 (6a2eaa7) + wahmi
        "2ee184794ee2a948",   # 2026-07-25 (6a2eaa7) + mp4upload + wahmi
    }),
}

