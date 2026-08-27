"""Ad and popup suppression for the automation browser -- without an extension.

Why not an extension: the engine drives ad-heavy pages, where overlay ads and
popunders cause mis-clicks and junk tabs, and an ad blocker was previously installed
for that reason. Chrome no longer allows an app to install one unattended:

* `--load-extension` and Selenium's `add_extension(.crx)` are both ignored on
  current Chrome (measured on 151 -- neither puts anything in the profile);
* the old fallback opened the Chrome Web Store and fired blind pyautogui keystrokes
  at the native confirm dialog. pyautogui is not even bundled in the packaged app,
  so for a new user that path raised ImportError and installed nothing.

Blocking the requests instead needs no install, no store, no clicks and no user
interaction, works headless, and cannot be broken by extension policy changes.
Chrome enforces it natively via CDP (Network.setBlockedURLs).

Only dedicated ad/tracker/popunder networks are listed. The anime sites and every
file host the downloader uses (Google Drive, MediaFire, Workupload, mega, ...) are
deliberately absent, so downloads are unaffected.
"""

AD_URL_PATTERNS = [
    # Mainstream ad exchanges
    "*doubleclick.net*", "*googlesyndication.com*", "*googleadservices.com*",
    "*adservice.google*", "*adnxs.com*", "*rubiconproject.com*",
    "*pubmatic.com*", "*openx.net*", "*criteo.*", "*casalemedia.com*",
    # Popunder / aggressive networks common on streaming sites
    "*popads.net*", "*popcash.net*", "*poptm.com*", "*propellerads.com*",
    "*onclickads.net*", "*exoclick.com*", "*exosrv.com*", "*juicyads.com*",
    "*adsterra.com*", "*hilltopads.net*", "*clickadu.com*", "*adcash.com*",
    "*mgid.com*", "*adskeeper.com*", "*revcontent.com*", "*trafficjunky.*",
    # Content-recommendation widgets
    "*taboola.com*", "*outbrain.com*", "*zergnet.com*",
    # Trackers and analytics beacons
    "*google-analytics.com*", "*googletagmanager.com*", "*histats.com*",
    "*statcounter.com*", "*hotjar.com*", "*scorecardresearch.com*",
    # Interstitial landing pages a download link can be redirected to instead of the
    # file. These are not ad servers, so an ad blocker's filter lists do not carry
    # them, but they hijack the download tab and are what the user actually sees.
    # None of them is a file host the downloader uses.
    "*fast.io*",
]


def apply(driver, patterns=None):
    """Block ad/tracker requests on a live driver. Returns True if it took effect.

    Safe to call on any driver; a failure here must never stop a download, so the
    caller is not expected to handle errors.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs",
                               {"urls": list(patterns or AD_URL_PATTERNS)})
        return True
    except Exception:
        return False
