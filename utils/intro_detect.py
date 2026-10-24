"""Find each episode's opening and ending from its audio.

An anime's opening and ending songs repeat from one episode to the next, so two
episodes of the same show share a ~90 s stretch of audio near the start (the
opening) and near the end (the ending) -- wherever the episode places it: in
Sousou no Frieren it starts at 0 s, 23 s, 45 s, 48 s or 68 s. This finds those
stretches by comparing every episode with its neighbours in the folder, and
saves the times beside the episodes in a hidden .aed-skip.json (format and
background queue: utils/skip_times.py), which the bundled mpv.net script
(scripts/aed-skip.lua) reads to offer "Skip opening".

How:
  * decode: mpv.net's own libmpv (encode mode) writes 11 kHz mono PCM of the
    first 10 minutes and the last 5 -- about 1 s per episode, nothing to install;
  * fingerprint: every 0.05 s becomes 47 bits -- whether each of 24 frequency
    bands is louder than the next band, and louder than a step before. Bits
    survive re-encoding and volume changes; they don't survive different audio;
  * match: the time offset at which two fingerprints agree most (cross-
    correlation, all offsets at once), then the longest stretch along it where
    they keep agreeing. Music under dialogue still agrees ~0.8 per bit, unrelated
    audio ~0.5;
  * rules: 60-150 s long, and found again with a second neighbour. Anything less
    certain is dropped -- skipping part of the episode is worse than not
    skipping (the same rule core/filler.py follows).

Measured on all 28 episodes of Sousou no Frieren against AniSkip's crowd-
sourced times: 27/27 openings, median error 0.8 s at the start, 0.6 s at the
end; endings median 0.5 s / 1.7 s, plus 7 that AniSkip doesn't have. ~1.5 s
of decoding per episode, ~0.1 s of matching.
"""

import ctypes
import os
import shutil
import tempfile
import threading
import time
import wave

import numpy as np

VERSION = 1

RATE = 11025              # Hz, mono: plenty for band energies up to 5 kHz
HOP = 0.05                # s per fingerprint frame
WINDOW = 4096             # samples (0.37 s) per spectrum: with HOP, an opening that
                          # starts half a frame off between two episodes still agrees
                          # 0.78 under noise (0.60 with 0.1 s / 0.19 s)
OP_WINDOW = 600.0         # an opening is looked for in the first 10 minutes ...
ED_WINDOW = 300.0         # ... an ending in the last 5
MIN_LEN, MAX_LEN = 60.0, 150.0
SMOOTH = 3.0              # s: agreement is averaged over this before thresholding
AGREE = 0.66              # averaged bit agreement that counts as "same audio"
CONFIRM_TOLERANCE = 2.0   # s: two neighbours must place a segment this close
_BANDS = np.geomspace(150, 5000, 25)


# --------------------------------------------------------------------- decode

class DecodeError(RuntimeError):
    pass


class _Event(ctypes.Structure):
    _fields_ = [("event_id", ctypes.c_int), ("error", ctypes.c_int),
                ("reply_userdata", ctypes.c_uint64), ("data", ctypes.c_void_p)]


_MPV_EVENT_SHUTDOWN = 1
_MPV_EVENT_END_FILE = 7
_lib_lock = threading.Lock()
_libs = {}


def find_libmpv():
    """mpv.net's libmpv-2.dll, or "" when mpv.net isn't installed."""
    try:
        from utils.mpvnet import find_mpvnet
        exe = find_mpvnet()[0]
    except Exception:
        exe = None
    if not exe:
        return ""
    dll = os.path.join(os.path.dirname(exe), "libmpv-2.dll")
    return dll if os.path.isfile(dll) else ""


def _load(dll):
    with _lib_lock:
        lib = _libs.get(dll)
        if lib is None:
            lib = ctypes.CDLL(dll)
            lib.mpv_create.restype = ctypes.c_void_p
            lib.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
            lib.mpv_initialize.argtypes = [ctypes.c_void_p]
            lib.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
            lib.mpv_wait_event.argtypes = [ctypes.c_void_p, ctypes.c_double]
            lib.mpv_wait_event.restype = ctypes.POINTER(_Event)
            lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]
            _libs[dll] = lib
        return lib


def decode(path, start, length, dll=None, timeout=120.0):
    """Mono 16-bit PCM at RATE of `length` seconds from `start` (negative: from the
    end), as float32. mpv.net's libmpv does the decoding in encode mode."""
    dll = dll or find_libmpv()
    if not dll:
        raise DecodeError("mpv.net is not installed")
    lib = _load(dll)
    tmp = tempfile.mkdtemp(prefix="aed_skip_")
    try:
        out = os.path.join(tmp, "a.wav")
        h = lib.mpv_create()
        if not h:
            raise DecodeError("libmpv could not start")
        try:
            opts = {"o": out, "of": "wav", "oac": "pcm_s16le", "vid": "no", "sid": "no",
                    "audio-channels": "mono", "audio-samplerate": str(RATE),
                    "start": f"{start:.3f}", "length": f"{length:.3f}",
                    "hwdec": "no", "terminal": "no", "msg-level": "all=no",
                    "idle": "no", "load-scripts": "no", "ytdl": "no", "config": "no"}
            for k, v in opts.items():
                lib.mpv_set_option_string(h, k.encode(), v.encode("utf-8"))
            if lib.mpv_initialize(h) < 0:
                raise DecodeError("libmpv could not initialize")
            args = (ctypes.c_char_p * 3)(b"loadfile", path.encode("utf-8"), None)
            lib.mpv_command(h, args)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ev = lib.mpv_wait_event(h, 1.0).contents
                if ev.event_id in (_MPV_EVENT_END_FILE, _MPV_EVENT_SHUTDOWN):
                    break
            else:
                raise DecodeError("decoding timed out")
        finally:
            lib.mpv_terminate_destroy(h)
        return read_wav(out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_wav(path):
    try:
        with wave.open(path) as w:
            if w.getsampwidth() != 2:
                raise DecodeError("unexpected sample format")
            data = np.frombuffer(w.readframes(w.getnframes()), np.int16)
            if w.getnchannels() > 1:
                data = data.reshape(-1, w.getnchannels()).mean(1)
            return data.astype(np.float32)
    except (OSError, EOFError, wave.Error) as e:
        raise DecodeError(f"no audio decoded: {e}") from e


# ---------------------------------------------------------------- fingerprint

def fingerprint(samples, rate=RATE):
    """frames x 47 bits (bool): band energy rising across bands, and over time."""
    from scipy.signal import stft
    if len(samples) < rate:
        return np.zeros((0, 47), bool)
    hop = int(rate * HOP)
    f, _t, z = stft(np.asarray(samples, np.float32), rate, nperseg=WINDOW,
                    noverlap=WINDOW - hop, boundary=None)
    p = z.real ** 2 + z.imag ** 2
    # Bins -> 24 bands in one matrix product (a mask per band copies the spectrum).
    band = np.digitize(f, _BANDS) - 1
    weights = (band[None, :] == np.arange(len(_BANDS) - 1)[:, None]).astype(p.dtype)
    e = np.log(weights @ p + 1e-6)
    across = e[1:] > e[:-1]
    over_time = np.diff(e, axis=1, prepend=e[:, :1]) > 0
    return np.vstack([across, over_time]).T


# --------------------------------------------------------------------- match

def _best_lags(a, b, count=3):
    """The `count` offsets (frames, b relative to a) where the fingerprints agree
    most, over all offsets at once: per-bit cross-correlation of +-1 signals."""
    n, m = len(a), len(b)
    size = 1 << int(np.ceil(np.log2(n + m)))
    sa = np.where(a, 1.0, -1.0)
    sb = np.where(b, 1.0, -1.0)
    fa = np.fft.rfft(sa, size, axis=0)
    fb = np.fft.rfft(sb[::-1], size, axis=0)
    corr = np.fft.irfft(fa * fb, size, axis=0).sum(1)[: n + m - 1]
    # corr[k] pairs a[i] with b[j], j - i = (m - 1) - k
    order = np.argsort(corr)[::-1]
    lags, seen = [], []
    for k in order:
        lag = (m - 1) - int(k)
        if all(abs(lag - s) > 2.0 / HOP for s in seen):       # 2 s apart
            lags.append(lag)
            seen.append(lag)
            if len(lags) == count:
                break
    return lags


def _run_on_lag(a, b, lag):
    """Longest stretch where a[i] and b[i + lag] keep agreeing: (len_s, start_a_s,
    start_b_s, agreement), or None."""
    i0, j0 = max(-lag, 0), max(lag, 0)
    k = min(len(a) - i0, len(b) - j0)
    if k * HOP < MIN_LEN:
        return None
    sim = (a[i0:i0 + k] == b[j0:j0 + k]).mean(1)
    w = int(SMOOTH / HOP)
    smooth = np.convolve(sim, np.ones(w) / w, "same")
    good = smooth > AGREE
    edges = np.diff(np.concatenate([[0], good.astype(np.int8), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    if not len(starts):
        return None
    r = int(np.argmax(ends - starts))
    s, e = int(starts[r]), int(ends[r])
    # The moving average spreads a run ~half a window past each true edge: pull
    # each edge in to where the raw agreement itself is high.
    edge = int(1.0 / HOP)                                       # 1 s
    raw_good = np.convolve(sim, np.ones(edge) / edge, "same") > AGREE
    while s < e and not raw_good[s]:
        s += 1
    while e > s and not raw_good[e - 1]:
        e -= 1
    length = (e - s) * HOP
    if not (MIN_LEN <= length <= MAX_LEN):
        return None
    return length, (i0 + s) * HOP, (j0 + s) * HOP, float(sim[s:e].mean())


def shared_segment(a, b):
    """The longest 60-150 s stretch two fingerprints share: (len_s, start_a_s,
    start_b_s, agreement) in seconds from each fingerprint's start, or None."""
    if len(a) * HOP < MIN_LEN or len(b) * HOP < MIN_LEN:
        return None
    best = None
    for lag in _best_lags(a, b):
        run = _run_on_lag(a, b, lag)
        if run and (best is None or run[0] > best[0]):
            best = run
    return best


def _confirmed(found):
    """found: [(start, end)] of one episode's segment from different neighbours.
    Confirmed when two neighbours place it within CONFIRM_TOLERANCE: their
    median, else None."""
    for i in range(len(found)):
        close = [f for f in found if abs(f[0] - found[i][0]) <= CONFIRM_TOLERANCE
                 and abs(f[1] - found[i][1]) <= CONFIRM_TOLERANCE]
        if len(close) >= 2:
            return [round(float(np.median([c[0] for c in close])), 1),
                    round(float(np.median([c[1] for c in close])), 1)]
    return None


def detect(episodes, prints):
    """episodes: [(number, path, duration)] sorted by number; prints: {path:
    {"op": bits | None, "ed": bits | None}}. Returns {path: {"op": [s, e] | None,
    "ed": [s, e] | None}} in seconds from the start of each file.

    Each episode is compared with up to two neighbours on each side; a segment
    counts only when two of them agree on where it is."""
    pairs = {}

    def segment(kind, p, q):
        """shared_segment(p, q) as (start in p, start in q, length); each pair once."""
        if (kind, q, p) in pairs:
            hit = pairs[(kind, q, p)]
            return (hit[1], hit[0], hit[2]) if hit else None
        a, b = prints[p].get(kind), prints[q].get(kind)
        seg = shared_segment(a, b) if a is not None and b is not None else None
        pairs[(kind, p, q)] = (seg[1], seg[2], seg[0]) if seg else None
        return pairs[(kind, p, q)]

    out = {}
    for i, (_n, path, duration) in enumerate(episodes):
        res = {}
        for kind in ("op", "ed"):
            offset = 0.0 if kind == "op" else max(0.0, duration - ED_WINDOW)
            found = []
            for j in (i - 1, i + 1, i - 2, i + 2):
                if 0 <= j < len(episodes):
                    seg = segment(kind, path, episodes[j][1])
                    if seg:
                        start, _other, length = seg
                        found.append((offset + start, offset + start + length))
            res[kind] = _confirmed(found)
        out[path] = res
    return out


# ------------------------------------------------------------------ a folder

def video_duration(path):
    from utils.library_scan import video_duration as duration
    return duration(path)


def detect_folder(folder, force=False, dll=None, decoder=None, duration=video_duration,
                  should_stop=lambda: False):
    """Find openings/endings for the episodes in `folder` and update its skip file
    (utils/skip_times.py). Episodes already done for the same file are kept
    unless `force`. Returns how many episodes have an opening, or -1 when nothing
    could run (fewer than 3 episodes, mpv.net missing, stopped)."""
    from utils.skip_times import SKIP_FILE, is_current, read_skip_file, stamp
    from utils.watch_later import episode_files, write_hidden_json
    files = sorted(episode_files(folder).items())
    if len(files) < 3:
        return -1           # two neighbours must agree: three episodes at least
    if decoder is None:
        dll = dll or find_libmpv()
        if not dll:
            return -1       # retried on the next visit, not saved as "none found"
        decoder = lambda p, s, n: decode(p, s, n, dll=dll)  # noqa: E731
    old = read_skip_file(folder).get("episodes") or {}
    todo = {p for _n, p in files if force or not is_current(old.get(os.path.basename(p)), p)}
    if not todo:
        return sum(1 for e in old.values() if isinstance(e, dict) and e.get("op"))
    # Fingerprint what needs it and its neighbours (they're compared with it).
    index = {p: i for i, (_n, p) in enumerate(files)}
    need = {files[j][1] for p in todo
            for j in range(max(0, index[p] - 2), min(len(files), index[p] + 3))}
    episodes, prints, failed = [], {}, set()
    for n, p in files:
        if p not in need:
            continue
        if should_stop():
            return -1
        d = duration(p) or 0.0
        try:
            op = fingerprint(decoder(p, 0.0, OP_WINDOW))
            ed = fingerprint(decoder(p, -ED_WINDOW, ED_WINDOW)) if d > ED_WINDOW else None
        except DecodeError:
            failed.add(p)     # not saved: tried again next time
            continue
        episodes.append((n, p, d))
        prints[p] = {"op": op, "ed": ed}
    found = detect(episodes, prints)
    eps = dict(old)
    for p in todo - failed:
        if p not in found:
            continue
        try:
            entry = stamp(p)
        except OSError:
            continue
        entry.update({"op": found[p]["op"], "ed": found[p]["ed"], "source": "audio"})
        eps[os.path.basename(p)] = entry
    names = {os.path.basename(p) for _n, p in files}
    eps = {k: v for k, v in eps.items() if k in names}       # files that are gone
    write_hidden_json(os.path.join(folder, SKIP_FILE), {"version": VERSION, "episodes": eps})
    return sum(1 for e in eps.values() if isinstance(e, dict) and e.get("op"))

