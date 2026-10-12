"""Global keyboard shortcuts: key combinations that work from anywhere in Windows,
whether the app is running or not.

  * Library           -- opens the app straight on the Library;
  * Continue watching -- continues what you're watching in mpv.net (an episode
                         left part-way resumes, else the next one starts),
                         without opening the app window (utils/quick_play.py).

Windows does the listening. Each action is a Start-menu shortcut with a hotkey
("Auto Episodes Downloader - Library", "... - Continue watching"); Explorer
registers hotkeys of Start-menu shortcuts itself, so nothing of ours has to stay
running. The shortcut starts the app with that action's argument (--library,
--play-next), which main.py handles before even importing Qt.

Continue watching was first called "Play next": its action key, argument and
settings keep that name, so existing shortcuts and settings still work, and the
startup check moves an old "... - Play next" shortcut to the new name.

Keys are named by their virtual-key code, not the character they type, so a
combination works the same on every keyboard layout (on an Arabic layout the L
key types "م", but it is still the L key).

Standard library only: the .lnk is written by Windows' own ShellLink object,
called with ctypes (see _save_shell_link for why it must be that object).
"""
import os
import sys
import time

ACTIONS = {
    "library": {
        "title": "Open the Library",
        "file": "Auto Episodes Downloader - Library.lnk",
        "arg": "--library",
        "setting": "library_hotkey",
        "target_setting": "library_hotkey_target",
        "description": "Open Auto Episodes Downloader on the Library",
        "done": "opens the Library from anywhere.",
    },
    "play_next": {
        "title": "Continue watching",
        "file": "Auto Episodes Downloader - Continue watching.lnk",
        "old_files": ["Auto Episodes Downloader - Play next.lnk"],
        "arg": "--play-next",
        "setting": "play_next_hotkey",
        "target_setting": "play_next_hotkey_target",
        "description": "Continue watching where you left off",
        "done": "continues what you're watching from anywhere.",
    },
}

# The Library shortcut's names, kept for the code that came first.
SHORTCUT_NAME = ACTIONS["library"]["file"]
LIBRARY_ARG = ACTIONS["library"]["arg"]
SETTING = ACTIONS["library"]["setting"]
TARGET_SETTING = ACTIONS["library"]["target_setting"]

_MODS = ("Ctrl", "Alt", "Shift")
_MOD_FLAGS = {"Alt": 0x1, "Ctrl": 0x2, "Shift": 0x4}      # RegisterHotKey's MOD_*
_HOTKEYF = {"Shift": 0x1, "Ctrl": 0x2, "Alt": 0x4}         # a .lnk's HOTKEYF_*
MOD_NOREPEAT = 0x4000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_TEST_HOTKEY_ID = 0xBFFE


# ------------------------------------------------------------------ the keys

def _key_table():
    keys = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        keys[c] = ord(c)
    for n in range(1, 25):
        keys[f"F{n}"] = 0x6F + n
    for n in range(10):
        keys[f"Num{n}"] = 0x60 + n
    keys.update({
        "Space": 0x20, "Enter": 0x0D, "Backspace": 0x08, "Insert": 0x2D, "Delete": 0x2E,
        "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
        "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
        "Pause": 0x13, "PrintScreen": 0x2C, "ScrollLock": 0x91,
        "Num*": 0x6A, "Num+": 0x6B, "Num-": 0x6D, "Num.": 0x6E, "Num/": 0x6F,
        # The punctuation keys of a US layout, by position.
        ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
        "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    })
    return keys


KEYS = _key_table()
_KEY_BY_VK = {vk: name for name, vk in KEYS.items()}
_KEY_BY_LOWER = {name.lower(): name for name in KEYS}
# Keys that type something: with Shift alone they are how capitals and symbols
# are typed.
_TYPING_KEYS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") | {
    "Space", ";", "=", ",", "-", ".", "/", "`", "[", "\\", "]", "'"}

# Combinations Windows (or most PC graphics drivers) already own.
RESERVED = {
    "Ctrl+Alt+Delete": "the Windows security screen",
    "Ctrl+Shift+Escape": "Task Manager",
    "Ctrl+Escape": "the Start menu",
    "Ctrl+Alt+Tab": "the Windows app switcher",
    "Alt+Tab": "the Windows app switcher",
    "Alt+Shift+Tab": "the Windows app switcher",
    "Alt+Escape": "switching windows",
    "Alt+F4": "closing windows",
    "Alt+Space": "the window menu",
    "Ctrl+Alt+Up": "screen rotation (graphics driver)",
    "Ctrl+Alt+Down": "screen rotation (graphics driver)",
    "Ctrl+Alt+Left": "screen rotation (graphics driver)",
    "Ctrl+Alt+Right": "screen rotation (graphics driver)",
    "Alt+Shift+PrintScreen": "Windows High Contrast",
    "Ctrl+Alt+PrintScreen": "screenshot tools",
    "Alt+PrintScreen": "a screenshot of the window",
}

# Overlays that listen for their defaults by reading the keyboard directly, which
# Windows can't report. Flagged only while that program is running.
KNOWN_APPS = {
    "radeonsoftware.exe": ("AMD Radeon Software", {
        "Ctrl+Shift+E": "start/stop recording", "Ctrl+Shift+S": "save instant replay",
        "Ctrl+Shift+I": "screenshot", "Ctrl+Shift+O": "performance overlay"}),
    "nvidia share.exe": ("NVIDIA overlay", {
        "Alt+Shift+F10": "instant replay on/off", "Alt+Z": "the overlay",
        "Alt+F9": "recording", "Alt+F10": "saving instant replay", "Alt+F1": "screenshots"}),
    "nvidia overlay.exe": ("NVIDIA overlay", {
        "Alt+Shift+F10": "instant replay on/off", "Alt+Z": "the overlay",
        "Alt+F9": "recording", "Alt+F10": "saving instant replay", "Alt+F1": "screenshots"}),
}


def normalize(text):
    """'ctrl+alt+l' -> 'Ctrl+Alt+L'. Raises ValueError with a message for the user.

    What a Windows shortcut can hold: one, two or three of Ctrl / Alt / Shift with
    one other key. (One modifier takes that key away from every app -- see
    app_warnings.) The Windows key can't be used by shortcuts."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Press a key combination, like Ctrl+Alt+L.")
    # Split on "+" but keep a trailing "Num+" / "+" key intact.
    parts, cur = [], ""
    for ch in raw:
        if ch == "+" and cur and cur.lower() not in ("num",):
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        parts.append(cur)
    parts = [p.strip() for p in parts if p.strip()]
    names = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift",
             "meta": "Win", "win": "Win", "windows": "Win"}
    aliases = {"esc": "Escape", "escape": "Escape", "return": "Enter", "del": "Delete",
               "ins": "Insert", "pgup": "PageUp", "pgdown": "PageDown", "tab": "Tab",
               "print": "PrintScreen", "sysreq": "PrintScreen"}
    mods, keys = set(), []
    for p in parts:
        name = names.get(p.lower())
        if name == "Win":
            raise ValueError("The Windows key can't be part of a shortcut. Use Ctrl, Alt or Shift.")
        if name:
            mods.add(name)
            continue
        key = _KEY_BY_LOWER.get(p.lower()) or aliases.get(p.lower())
        keys.append(key or p)
    if len(keys) != 1:
        raise ValueError("Use one key together with Ctrl, Alt or Shift.")
    key = keys[0]
    if key in ("Escape", "Tab"):
        canonical = "+".join([m for m in _MODS if m in mods] + [key])
        what = RESERVED.get(canonical)
        raise ValueError(f"{canonical} belongs to Windows" + (f" ({what})." if what else "."))
    if key not in KEYS:
        raise ValueError(f"'{key}' can't be used in a shortcut.")
    if not mods:
        raise ValueError(f"Add Ctrl, Alt or Shift to {key} (for example Ctrl+{key}).")
    return "+".join([m for m in _MODS if m in mods] + [key])


def key_name(vk):
    """A key's name for display, by virtual-key code ("" for an unknown one)."""
    return {0x1B: "Escape", 0x09: "Tab"}.get(vk) or _KEY_BY_VK.get(vk) or (
        f"Key {vk:#04x}" if vk else "")


def from_vk(ctrl, alt, shift, vk):
    """The combination for a key press, by virtual-key code. Raises ValueError."""
    key = _KEY_BY_VK.get(vk)
    if vk == 0x1B:
        key = "Escape"
    elif vk == 0x09:
        key = "Tab"
    if key is None:
        raise ValueError("That key can't be used in a shortcut.")
    mods = [m for m, on in (("Ctrl", ctrl), ("Alt", alt), ("Shift", shift)) if on]
    return normalize("+".join(mods + [key]))


def to_win32(hotkey):
    """(modifier flags, virtual-key code) for RegisterHotKey."""
    parts = _split(normalize(hotkey))
    return sum(_MOD_FLAGS[p] for p in parts[:-1]), KEYS[parts[-1]]


def to_lnk_hotkey(hotkey):
    """The 2-byte HotKey field of a .lnk header: key code low, HOTKEYF_* high."""
    parts = _split(normalize(hotkey))
    return KEYS[parts[-1]] | (sum(_HOTKEYF[p] for p in parts[:-1]) << 8)


def _split(canonical):
    """'Ctrl+Alt+Num+' -> ['Ctrl', 'Alt', 'Num+'] (the key may itself contain '+')."""
    parts = canonical.split("+")
    mods = [p for p in parts if p in _MODS]
    return mods + [canonical[len("+".join(mods)) + (1 if mods else 0):]]


# ------------------------------------------------------------- what else has it

def is_taken(hotkey):
    """Is the combination registered system-wide already (by another program, or
    by a shortcut Explorer has registered)? Tried by registering it for an
    instant and letting it go."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    u.RegisterHotKey.restype = wintypes.BOOL
    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    mods, vk = to_win32(hotkey)
    if u.RegisterHotKey(None, _TEST_HOTKEY_ID, mods | MOD_NOREPEAT, vk):
        u.UnregisterHotKey(None, _TEST_HOTKEY_ID)
        return False
    return ctypes.get_last_error() == ERROR_HOTKEY_ALREADY_REGISTERED


def read_lnk_hotkey(path):
    """The HotKey field of a .lnk file (0 when it has none or isn't a link)."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x4C)
    except OSError:
        return 0
    if len(head) < 0x4C or head[:4] != b"\x4c\x00\x00\x00":
        return 0
    return head[0x40] | (head[0x41] << 8)


def from_lnk_hotkey(value):
    """The combination a .lnk HotKey field holds, or "" (none, or not one we allow)."""
    vk, flags = value & 0xFF, value >> 8
    if not vk:
        return ""
    try:
        return from_vk(bool(flags & _HOTKEYF["Ctrl"]), bool(flags & _HOTKEYF["Alt"]),
                       bool(flags & _HOTKEYF["Shift"]), vk)
    except ValueError:
        return ""


def current_hotkey(action="library"):
    """The combination in effect for `action`: read from its shortcut, which is
    what Windows acts on. The saved setting can lag behind it -- another copy of
    the app closing with older settings in memory writes over it."""
    path = shortcut_path(action)
    if os.path.exists(path):
        return from_lnk_hotkey(read_lnk_hotkey(path))
    return ""


def _shortcut_folders():
    env = os.environ
    if env.get("AED_SHORTCUT_DIR"):                      # tests: never the real ones
        return [env["AED_SHORTCUT_DIR"]]
    return [p for p in (
        os.path.join(env.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu"),
        os.path.join(env.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu"),
        os.path.join(env.get("USERPROFILE", ""), "Desktop"),
        os.path.join(env.get("PUBLIC", ""), "Desktop"),
        os.path.join(env.get("USERPROFILE", ""), "OneDrive", "Desktop"),
    ) if p and os.path.isdir(p)]


def shortcuts_using(hotkey, folders=None, skip=()):
    """Names of Start-menu / desktop shortcuts with the same key combination, other
    than the paths in `skip`. Windows only honours one: the others silently stop
    working."""
    want = to_lnk_hotkey(hotkey)
    skip = {os.path.normcase(p) for p in skip}
    found = []
    for folder in folders if folders is not None else _shortcut_folders():
        for root, _dirs, files in os.walk(folder):
            for name in files:
                if not name.lower().endswith(".lnk"):
                    continue
                path = os.path.join(root, name)
                if os.path.normcase(path) not in skip and read_lnk_hotkey(path) == want:
                    found.append(name[:-4])
    return sorted(set(found))


def _running_programs():
    try:
        import psutil
        return {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
    except Exception:
        return set()


def conflicts(hotkey, current="", action="library"):
    """Certain conflicts, as sentences for the user ([] = free): Windows' own
    combinations, our other shortcut, other shortcuts with the same keys, and any
    program that has registered it. `current` is this action's saved combination:
    its own shortcut holds it, which is not a conflict."""
    hotkey = normalize(hotkey)
    out = []
    if hotkey in RESERVED:
        out.append(f"Windows already uses {hotkey} for {RESERVED[hotkey]}.")
    ours = {}
    for name, info in ACTIONS.items():
        ours[os.path.normcase(shortcut_path(name))] = info["title"]
        for old in info.get("old_files", ()):          # not yet moved by health()
            ours[os.path.normcase(os.path.join(os.path.dirname(shortcut_path(name)), old))] = info["title"]
        if name != action and current_hotkey(name) == hotkey:
            out.append(f"Your “{info['title']}” shortcut already uses {hotkey}.")
    for name in shortcuts_using(hotkey, skip=list(ours)):
        out.append(f"The shortcut “{name}” already uses {hotkey}.")
    own = hotkey == current or hotkey == current_hotkey(action)
    if not own and not out and is_taken(hotkey):
        out.append(f"Another program has already claimed {hotkey}.")
    return out


_SELECTION_KEYS = ("Left", "Right", "Up", "Down", "Home", "End", "PageUp", "PageDown")
# Shortcuts most programs have. A global combination takes the keys away from
# every app, so these work but come with a warning.
COMMON_APP_KEYS = {
    "Ctrl+Shift+T": "reopening a closed tab in browsers",
    "Ctrl+Shift+N": "a private window in browsers and a new folder in File Explorer",
    "Ctrl+Shift+Z": "Redo",
    "Ctrl+Shift+V": "paste as plain text",
    "Ctrl+Shift+Delete": "clearing browsing data",
    "Ctrl+Shift+I": "browser developer tools",
    "Ctrl+Shift+J": "the browser console",
    "Ctrl+Shift+S": "Save As",
    **{f"Ctrl+Shift+{k}": "selecting text" for k in _SELECTION_KEYS},
    **{f"Shift+{k}": "selecting text" for k in _SELECTION_KEYS},
    **{f"Ctrl+{k}": "moving through text" for k in _SELECTION_KEYS},
    "Ctrl+A": "Select all", "Ctrl+C": "Copy", "Ctrl+V": "Paste", "Ctrl+X": "Cut",
    "Ctrl+Z": "Undo", "Ctrl+Y": "Redo", "Ctrl+S": "Save", "Ctrl+O": "Open",
    "Ctrl+N": "New", "Ctrl+P": "Print", "Ctrl+F": "Find", "Ctrl+H": "Replace / history",
    "Ctrl+T": "a new tab", "Ctrl+W": "closing a tab", "Ctrl+R": "reloading a page",
    "Ctrl+L": "the browser address bar", "Ctrl+D": "bookmarks", "Ctrl+J": "downloads",
    "Ctrl+B": "bold", "Ctrl+I": "italic", "Ctrl+U": "underline", "Ctrl+K": "inserting links",
    "Ctrl+E": "search", "Ctrl+G": "Find next", "Ctrl+Q": "quitting apps",
    "Ctrl+Enter": "sending messages", "Ctrl+Backspace": "deleting a word",
    "Ctrl+Delete": "deleting a word", "Ctrl+F4": "closing a tab", "Ctrl+F5": "a full reload",
    "Shift+Delete": "deleting without the Recycle Bin", "Shift+F10": "the right-click menu",
    "Shift+Enter": "a new line in chats", "Alt+Enter": "full screen and Properties",
    "Alt+Left": "Back in browsers", "Alt+Right": "Forward in browsers",
    "Alt+Up": "going up a folder in File Explorer", "Alt+D": "the browser address bar",
}


def app_warnings(hotkey, running=None):
    """Likely conflicts, which still may be used: a running overlay whose default
    keys include `hotkey` (it reads the keyboard directly, so Windows can't
    confirm it, and its keys may have been changed), or keys that apps use
    themselves -- a global shortcut takes them away from every app."""
    hotkey = normalize(hotkey)
    running = _running_programs() if running is None else running
    parts = _split(hotkey)
    mods, key = parts[:-1], parts[-1]
    out = []
    for exe, (app, combos) in KNOWN_APPS.items():
        if exe in running and hotkey in combos:
            out.append(f"{app} uses {hotkey} for {combos[hotkey]} by default.")
    if mods == ["Shift"] and key in _TYPING_KEYS:
        shown = key if len(key) > 1 or not key.isalpha() else key.upper()
        out.append(f"Shift+{key} is how {shown if key != 'Space' else 'a space'} is typed "
                   f"with Shift: you couldn't type it anywhere while this is your shortcut.")
    elif hotkey in COMMON_APP_KEYS:
        out.append(f"Apps use {hotkey} for {COMMON_APP_KEYS[hotkey]}; it would stop working "
                   "in them.")
    elif mods == ["Alt"] and len(key) == 1 and key.isalpha():
        out.append(f"Many apps open a menu with {hotkey}; it would stop working in them.")
    elif len(mods) == 1 and not (key.startswith("F") and key[1:].isdigit() and int(key[1:]) > 12):
        out.append(f"Some apps use {hotkey} themselves; it would stop working in them.")
    return list(dict.fromkeys(out))


# --------------------------------------------------------------- the .lnk file

def shortcut_path(action="library"):
    """Where `action`'s shortcut lives: the Start menu (Explorer registers hotkeys
    there). AED_SHORTCUT_DIR moves it -- the test suite always sets it, so no test
    can read or change the user's real shortcuts."""
    folder = os.environ.get("AED_SHORTCUT_DIR")
    if not folder:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")
    return os.path.join(folder, ACTIONS[action]["file"])


def launch_target(action="library"):
    """(program, arguments, working folder, icon) the shortcut starts."""
    arg = ACTIONS[action]["arg"]
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, arg, os.path.dirname(exe), f"{exe},0"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    icon = os.path.join(root, "assets", "app_icon.ico")
    return pythonw, f'"{os.path.join(root, "main.py")}" {arg}', root, f"{icon},0"


def target_signature(action="library"):
    """What the shortcut must launch now; stored so a move or reinstall is noticed."""
    program, args, _cwd, _icon = launch_target(action)
    return f"{program}|{args}"


def _save_shell_link(path, program, args, cwd, icon, hotkey,
                     description="Open Auto Episodes Downloader on the Library"):
    """Write the .lnk through Windows' own ShellLink object (IShellLinkW +
    IPersistFile), called directly with ctypes.

    It has to be this object, saving in place: Explorer registers a shortcut's
    hotkey when the shell itself saves the link. A finished file written or moved
    into the Start menu any other way -- even byte-identical -- is listed but its
    hotkey is never registered (measured: 0 of 7 combinations). And SetHotkey
    takes any key, where WScript.Shell's Hotkey property only knows a few names.
    """
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                    ("d4", ctypes.c_ubyte * 8)]

    ole32 = ctypes.OleDLL("ole32")

    def guid(text):
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    def method(obj, index, *argtypes):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])

    def release(obj):
        vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)

    initialized = ctypes.windll.ole32.CoInitializeEx(None, 0x2) in (0, 1)  # STA; S_OK/S_FALSE
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(guid("{00021401-0000-0000-C000-000000000046}")),
                               None, 1,                                  # CLSCTX_INPROC_SERVER
                               ctypes.byref(guid("{000214F9-0000-0000-C000-000000000046}")),
                               ctypes.byref(link))
        icon_path, _, icon_index = icon.rpartition(",")
        method(link, 20, wintypes.LPCWSTR)(link, program)                 # SetPath
        method(link, 11, wintypes.LPCWSTR)(link, args)                    # SetArguments
        method(link, 9, wintypes.LPCWSTR)(link, cwd)                      # SetWorkingDirectory
        method(link, 7, wintypes.LPCWSTR)(link, description)              # SetDescription
        method(link, 17, wintypes.LPCWSTR, ctypes.c_int)(                 # SetIconLocation
            link, icon_path or icon, int(icon_index or 0))
        method(link, 13, wintypes.WORD)(link, to_lnk_hotkey(hotkey))      # SetHotkey
        method(link, 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(   # QueryInterface
            link, ctypes.byref(guid("{0000010B-0000-0000-C000-000000000046}")),
            ctypes.byref(persist))
        method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(persist, path, True)   # Save
    finally:
        for obj in (persist, link):
            if obj:
                release(obj)
        if initialized:
            ctypes.windll.ole32.CoUninitialize()


def _notify_shell(path):
    """Tell Explorer the shortcut changed, so it (re)registers the hotkey now."""
    try:
        import ctypes
        SHCNE_CREATE, SHCNE_DELETE, SHCNF_PATHW = 0x2, 0x4, 0x5
        event = SHCNE_CREATE if os.path.exists(path) else SHCNE_DELETE
        ctypes.windll.shell32.SHChangeNotify(event, SHCNF_PATHW, ctypes.c_wchar_p(path), None)
    except Exception:
        pass


def _wait(condition, seconds):
    end = time.time() + seconds
    while True:
        if condition():
            return True
        if time.time() >= end:
            return False
        time.sleep(0.15)


def install(hotkey, wait_for_explorer=3.0, action="library"):
    """Write `action`'s Start-menu shortcut with `hotkey`. Returns (ok, message).

    An existing shortcut is deleted first, not saved over: Explorer keeps the
    old key of a shortcut whose key changes in place registered until sign-out
    (measured), so both keys would stay taken. Deleting it releases the old one."""
    try:
        hotkey = normalize(hotkey)
    except ValueError as e:
        return False, str(e)
    if sys.platform != "win32":
        return False, "Shortcuts are only available on Windows."
    info = ACTIONS[action]
    path = shortcut_path(action)
    program, args, cwd, icon = launch_target(action)
    old = from_lnk_hotkey(read_lnk_hotkey(path)) if os.path.exists(path) else ""
    # Taken by someone else? (This shortcut holding these keys doesn't count.)
    # Checked first: once ours is saved, "taken" can't tell Explorer from them.
    if hotkey != old and is_taken(hotkey):
        return False, f"Another program has already claimed {hotkey}. Pick another one."
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            return False, f"Couldn't replace the old shortcut: {e}"
        _notify_shell(path)
        if old:
            _wait(lambda: not is_taken(old), 2.0)        # Explorer lets go of the old key
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        _save_shell_link(path, program, args, cwd, icon, hotkey, info["description"])
    except OSError as e:
        return False, f"Couldn't create the shortcut: {e}"
    if read_lnk_hotkey(path) != to_lnk_hotkey(hotkey):
        return False, "Couldn't save the key combination in the shortcut."
    # Explorer holds the combination once it has registered the shortcut.
    if wait_for_explorer <= 0 or _wait(lambda: is_taken(hotkey), wait_for_explorer):
        return True, f"{hotkey} {info['done']}"
    return True, (f"Saved. If {hotkey} doesn't respond yet, sign out of Windows and back in "
                  "once -- Windows picks up new shortcut keys at sign-in.")


def remove(action="library"):
    """Delete `action`'s shortcut (and with it the hotkey). True when none is left."""
    path = shortcut_path(action)
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    _notify_shell(path)
    return True


def refresh(settings):
    """At startup: for each action with a hotkey set whose shortcut is missing or
    launches a different program (the app was moved or reinstalled elsewhere),
    rewrite it. Returns True when it wrote one. Meant for a background thread."""
    wrote = False
    for action, info in ACTIONS.items():
        hotkey = settings.get(info["setting"])
        if not hotkey:
            continue
        path = shortcut_path(action)
        if os.path.exists(path) and settings.get(info["target_setting"]) == target_signature(action):
            continue
        ok, _msg = install(hotkey, wait_for_explorer=0, action=action)
        if ok:
            settings[info["target_setting"]] = target_signature(action)
            wrote = True
    return wrote


def _retire_old_names(action, info, settings, wait):
    """A shortcut still saved under an earlier name ("... - Play next.lnk"):
    delete it and carry its key over, so health() writes it under the current
    name. Not renamed in place -- a moved .lnk's hotkey is never registered."""
    changed = False
    folder = os.path.dirname(shortcut_path(action))
    for name in info.get("old_files", ()):
        old = os.path.join(folder, name)
        if not os.path.exists(old):
            continue
        hotkey = from_lnk_hotkey(read_lnk_hotkey(old))
        try:
            os.remove(old)
        except OSError:
            continue
        _notify_shell(old)
        changed = True
        if hotkey:
            _wait(lambda: not is_taken(hotkey), wait)    # Explorer lets go of it first
            if not os.path.exists(shortcut_path(action)):
                settings[info["setting"]] = hotkey
            settings.pop(info["target_setting"], None)  # written again below
    return changed


def health(settings, wait=3.0):
    """The startup check of every shortcut. Returns (settings changed, warnings).

      * the shortcut file is what Windows acts on: a setting that lags behind it
        is brought in line;
      * a shortcut that is missing, or launches a moved / reinstalled copy of the
        app, is written again;
      * one whose key Windows isn't holding (it slipped at sign-in, or the file
        was replaced by hand) is written again -- delete, then create -- so
        Explorer registers it; if Windows still doesn't take it, the user is told.

    A program that took the key afterwards by reading the keyboard directly
    can't be seen from here; the shortcut dialog's live test covers that."""
    changed, warnings = False, []
    if sys.platform != "win32":
        return changed, warnings
    for action, info in ACTIONS.items():
        changed |= _retire_old_names(action, info, settings, wait)
        path = shortcut_path(action)
        on_file = current_hotkey(action) if os.path.exists(path) else ""
        if on_file and on_file != settings.get(info["setting"]):
            settings[info["setting"]] = on_file
            changed = True
        hotkey = settings.get(info["setting"])
        if not hotkey:
            continue
        stale = (not os.path.exists(path)
                 or settings.get(info["target_setting"]) != target_signature(action))
        if not stale and is_taken(hotkey):
            continue                                  # in place and held: healthy
        ok, message = install(hotkey, wait_for_explorer=wait, action=action)
        if ok:
            settings[info["target_setting"]] = target_signature(action)
            changed = True
            if not is_taken(hotkey):
                warnings.append(f"Windows isn't responding to your “{info['title']}” shortcut "
                                f"({hotkey}). Sign out of Windows and back in once to fix it.")
        else:
            warnings.append(f"Your “{info['title']}” shortcut ({hotkey}) stopped working: "
                            f"{message}")
    return changed, warnings
