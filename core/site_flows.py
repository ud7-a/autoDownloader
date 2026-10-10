"""Built-in download flows per site, shared by Search (new profiles), the engine's
callers, the config migration and the tests -- one copy, so a fix here reaches
every user of it. Pure data and helpers: no Qt, no Selenium, safe to import early.
"""


def site_of(host):
    """The registrable part of a host name: a3.mp4upload.com -> mp4upload.com."""
    return ".".join(host.split(".")[-2:]) if host else ""


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

