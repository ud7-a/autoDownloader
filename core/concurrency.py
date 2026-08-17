"""Smart concurrency: pick how many episodes download at once, from measured speed.

The user shouldn't have to guess a number. Bandwidth is shared between downloads, so
with N running at once each episode gets roughly B/N and takes

    T = size * N / bandwidth

The aim is to keep each episode landing inside a target time band. Finishing well
under it means the pipe is under-used (a single download rarely saturates a line --
hosts cap per-connection speed), so another stream can run. Running over it means we
have over-subscribed and every episode is crawling.

Note this deliberately does not try to make an individual episode faster: on a
saturated line more parallelism makes each one slower. The band is a fair-share
target, chosen so more of the connection gets used without starving any one download.

Pure logic -- no Qt, no network, no sleeping (the clock is injectable), so it can be
unit-tested directly.
"""

import time


class ConcurrencyController:
    MIN_LIMIT = 1
    MAX_LIMIT = 6

    TARGET_LOW = 60.0        # seconds; below this we have headroom
    TARGET_HIGH = 90.0       # seconds; above this we are over-subscribed
    WINDOW = 15.0            # seconds between decisions
    SETTLE_WINDOWS = 2       # windows to wait after a change before deciding again
    FAILURE_COOLDOWN = 60.0  # seconds to stay off the gas after host pushback

    def __init__(self, start=3, enabled=True, clock=time.time):
        self._clock = clock
        self.enabled = bool(enabled)
        self.limit = self._clamp(int(start or 1))
        self.manual_limit = self.limit
        self.last_projection = None      # median seconds/episode from the last window
        self.last_reason = "start"
        self._samples = {}               # ep -> projected seconds
        self._next_eval = self._clock() + self.WINDOW
        self._settle = 0
        self._blocked_until = 0.0

    # ---- inputs -------------------------------------------------------------
    def record_progress(self, ep, total_bytes, speed_bytes):
        """Feed one aria2c progress line. Speed of 0 (stalled/starting) is ignored."""
        if not self.enabled or not speed_bytes or not total_bytes:
            return
        try:
            self._samples[ep] = float(total_bytes) / float(speed_bytes)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    def record_failure(self, reason=""):
        """Host pushback (block page, non-zero exit, 429/403).

        Back off immediately and hard: a rate-limit ban costs minutes to recover
        from, which is far more expensive than briefly running too few downloads.
        """
        if not self.enabled:
            return
        now = self._clock()
        self._blocked_until = now + self.FAILURE_COOLDOWN
        before = self.limit
        self.limit = self._clamp(self.limit // 2)
        self._samples.clear()
        self._next_eval = now + self.WINDOW
        self._settle = self.SETTLE_WINDOWS
        if self.limit != before:
            self.last_reason = f"backed off after {reason or 'a failure'}"
        return self.limit

    # ---- decision -----------------------------------------------------------
    def evaluate(self, force=False):
        """Called often (the throttle loop polls once a second); acts once a window."""
        if not self.enabled:
            return self.limit
        now = self._clock()
        if not force and now < self._next_eval:
            return self.limit
        self._next_eval = now + self.WINDOW

        projection = self._median(self._samples.values())
        self._samples.clear()
        self.last_projection = projection

        # Let a previous change take effect before judging it.
        if self._settle > 0:
            self._settle -= 1
            return self.limit
        if projection is None:
            return self.limit

        if projection < self.TARGET_LOW and now >= self._blocked_until:
            if self.limit < self.MAX_LIMIT:
                self.limit += 1
                self._settle = self.SETTLE_WINDOWS
                self.last_reason = f"episodes finishing in ~{projection:.0f}s -- room for one more"
            else:
                self.last_reason = "at the maximum"
        elif projection > self.TARGET_HIGH:
            if self.limit > self.MIN_LIMIT:
                self.limit -= 1
                self._settle = self.SETTLE_WINDOWS
                self.last_reason = f"episodes taking ~{projection:.0f}s -- easing off"
            else:
                # Already at one download; the connection simply is not fast enough.
                self.last_reason = "connection is the limit, not the settings"
        else:
            self.last_reason = f"~{projection:.0f}s per episode -- on target"
        return self.limit

    # ---- helpers ------------------------------------------------------------
    def describe(self):
        """Short human-readable state for the progress screen."""
        if not self.enabled:
            return f"Concurrent: {self.limit} (manual)"
        if self.last_projection:
            return f"Concurrent: {self.limit} (auto · ~{self.last_projection:.0f}s/episode)"
        return f"Concurrent: {self.limit} (auto)"

    def _clamp(self, value):
        return max(self.MIN_LIMIT, min(self.MAX_LIMIT, int(value)))

    @staticmethod
    def _median(values):
        vals = sorted(v for v in values if v and v > 0)
        if not vals:
            return None
        mid = len(vals) // 2
        if len(vals) % 2:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2.0
