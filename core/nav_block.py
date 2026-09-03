"""Cancel navigations to download interstitials before they happen.

The download buttons on the anime sites are wrapped in redirect chains. One of those
chains sometimes ends at fast.io's "Google Drive alternatives" page instead of the
file -- reported by users as the click being intercepted, and it kills the episode:
the next step's button never appears, the interception window expires, and the
download fails even though other mirrors on the page would have worked.

core.adblock cannot stop this. Network.setBlockedURLs refuses subresources only; a
top-level navigation to a blocked host still loads (measured). The Fetch domain is
the one that can: it pauses a request BEFORE it commits, and Fetch.failRequest
cancels it outright, so the interstitial never loads in any tab.

Two things make this harder than calling execute_cdp_cmd:

  Fetch pauses arrive as EVENTS, and Selenium's execute_cdp_cmd is request/response
  only -- it cannot receive them. So this talks to the browser-level DevTools socket
  directly (the same socket core.extensions uses to install the ad blocker) and runs
  a reader thread.

  The interstitial usually opens in a NEW tab, which would navigate before anything
  could be enabled on it. Target.setAutoAttach with waitForDebuggerOnStart holds each
  new target still until Fetch is enabled on it, which closes that race.

Safety, because a mechanism that pauses requests can hang a browser:

  * Only Document requests matching the patterns are paused, so a failure can never
    stall an image, a script, or the file transfer itself.
  * Anything paused that does not match is continued rather than failed, so a
    surprise in the pattern semantics degrades to "does nothing" instead of
    blocking every page.
  * A target held for the debugger is ALWAYS released, even if enabling Fetch on it
    raised, and Chrome resumes held targets by itself if this socket dies.
  * Every entry point is best-effort. Blocking ads is an improvement, not a
    requirement, and must never stop a download.
"""

import json
import re
import threading

# Only true interstitials belong here. This cancels page loads, so a wrong entry
# breaks a download outright -- unlike core.adblock's list, which only drops
# subresources and can afford to be broad.
BLOCKED_NAV_PATTERNS = ("*fast.io*",)


def _to_regex(pattern):
    """CDP-style '*' wildcard pattern -> compiled regex."""
    return re.compile(".*".join(re.escape(p) for p in pattern.split("*")), re.I)


class NavBlocker:
    """Cancels matching top-level navigations for one driver. Start it once."""

    def __init__(self, driver, patterns=None):
        self._driver = driver
        self._patterns = tuple(patterns or BLOCKED_NAV_PATTERNS)
        self._matchers = [_to_regex(p) for p in self._patterns]
        self._ws = None
        self._thread = None
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._stopping = False
        self.blocked = []          # URLs actually cancelled, for logging and tests

    # ---------------------------------------------------------------- protocol

    def _send(self, method, params=None, session_id=None):
        with self._send_lock:
            self._next_id += 1
            message = {"id": self._next_id, "method": method, "params": params or {}}
            if session_id:
                message["sessionId"] = session_id
            self._ws.send(json.dumps(message))
            return self._next_id

    def matches(self, url):
        return any(m.search(url or "") for m in self._matchers)

    def _arm_session(self, session_id):
        """Enable Fetch on a freshly attached target, then let it run.

        The release is unconditional: a target held for the debugger that never gets
        released stays frozen forever, which would break every download rather than
        just the ad.
        """
        try:
            self._send("Fetch.enable", {
                "patterns": [{"urlPattern": p, "requestStage": "Request",
                              "resourceType": "Document"} for p in self._patterns],
            }, session_id)
        except Exception:
            pass
        finally:
            try:
                self._send("Runtime.runIfWaitingForDebugger", {}, session_id)
            except Exception:
                pass

    def _on_paused(self, session_id, params):
        request_id = params.get("requestId")
        url = (params.get("request") or {}).get("url", "")
        if not request_id:
            return
        try:
            if self.matches(url):
                self._send("Fetch.failRequest",
                           {"requestId": request_id, "errorReason": "BlockedByClient"},
                           session_id)
                self.blocked.append(url)
            else:
                # Never reached with the patterns above -- but if the matching
                # semantics ever surprise us, letting it through is the safe error.
                self._send("Fetch.continueRequest", {"requestId": request_id}, session_id)
        except Exception:
            pass

    def _reader(self):
        while not self._stopping:
            try:
                message = json.loads(self._ws.recv())
            except Exception:
                return                      # socket closed or driver gone
            try:
                method = message.get("method")
                if method == "Fetch.requestPaused":
                    self._on_paused(message.get("sessionId"), message.get("params") or {})
                elif method == "Target.attachedToTarget":
                    params = message.get("params") or {}
                    if (params.get("targetInfo") or {}).get("type") in ("page", "iframe"):
                        self._arm_session(params.get("sessionId"))
                    else:
                        # Not a page, but it may still be waiting on us.
                        try:
                            self._send("Runtime.runIfWaitingForDebugger", {},
                                       params.get("sessionId"))
                        except Exception:
                            pass
            except Exception:
                continue                    # one bad message must not kill the loop

    # ---------------------------------------------------------------- lifecycle

    def start(self, timeout=10):
        """Attach and begin cancelling. Returns True if it took effect."""
        try:
            from core.extensions import _browser_websocket_url
            ws_url = _browser_websocket_url(self._driver)
            if not ws_url:
                return False
            import websocket        # websocket-client, bundled with the app
            # suppress_origin for the same reason as core.extensions: it avoids
            # --remote-allow-origins=*, which would let any local process attach.
            self._ws = websocket.create_connection(ws_url, timeout=timeout,
                                                   suppress_origin=True)
            self._ws.settimeout(None)       # the reader blocks until told to stop
            self._thread = threading.Thread(target=self._reader, daemon=True,
                                            name="nav-blocker")
            self._thread.start()
            # Covers tabs opened from here on, holding each one still until Fetch is
            # enabled -- the interstitial arrives in a new tab and would otherwise
            # navigate first.
            self._send("Target.setAutoAttach", {"autoAttach": True,
                                                "waitForDebuggerOnStart": True,
                                                "flatten": True})
            return True
        except Exception:
            self.stop()
            return False

    def stop(self):
        self._stopping = True
        try:
            if self._ws is not None:
                # Stop holding new targets before dropping the socket.
                try:
                    self._send("Target.setAutoAttach", {"autoAttach": False,
                                                        "waitForDebuggerOnStart": False,
                                                        "flatten": True})
                except Exception:
                    pass
                self._ws.close()
        except Exception:
            pass
        self._ws = None


def apply(driver, patterns=None):
    """Start cancelling interstitial navigations on `driver`.

    Returns the NavBlocker (so the caller can stop it or read what it blocked), or
    None if it could not attach. Never raises.
    """
    try:
        blocker = NavBlocker(driver, patterns)
        return blocker if blocker.start() else None
    except Exception:
        return None
