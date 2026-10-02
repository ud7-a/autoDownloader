"""mpv.net support: find it, install it with winget, and apply the bundled
quality-shader config (assets/mpvnet) to its config folder.

Stdlib only, so the frozen app needs nothing extra.
"""
import os
import re
import shutil
import subprocess
import sys
import time

WINGET_ID = "mpv.net"
EXE_NAME = "mpvnet.exe"

# What "Apply quality shaders" writes. Folders are merged file by file, so shaders
# or scripts the user added themselves stay where they are.
BUNDLE_FILES = ("mpv.conf", "input.conf", "mpvnet.conf")
BUNDLE_DIRS = ("Shaders", "scripts")
EMPTY_DIRS = ("script-opts",)
# The folder name the bundled [anime] profile-cond looks for.
ANIME_FOLDER = "animes"

# winget's "no newer version available" -- the package is already installed.
WINGET_ALREADY_INSTALLED = 0x8A15002B
CREATE_NO_WINDOW = 0x08000000


def bundle_dir():
    """assets/mpvnet: the frozen app's extract dir, or the repo when run from source."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", "mpvnet")


# ---------------------------------------------------------------- detection

def _uninstall_entries():
    """(DisplayName, InstallLocation, DisplayVersion, DisplayIcon) for every
    installed program, per-user and machine-wide."""
    try:
        import winreg
    except ImportError:
        return
    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, path in roots:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                        values = []
                        for name in ("DisplayName", "InstallLocation", "DisplayVersion", "DisplayIcon"):
                            try:
                                values.append(str(winreg.QueryValueEx(key, name)[0] or ""))
                            except OSError:
                                values.append("")
                        yield tuple(values)
                except OSError:
                    continue


def _app_paths_exe():
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{EXE_NAME}") as key:
                value = winreg.QueryValueEx(key, "")[0]
        except OSError:
            continue
        if value:
            return value.strip('"')
    return None


def find_mpvnet():
    """Return (exe_path, version) for an installed mpv.net, or (None, None).

    Looks at the uninstall entries first (that is where winget's installer
    registers it and the only place with a version), then App Paths, PATH and the
    usual install folders.
    """
    for name, location, version, icon in _uninstall_entries():
        if not name.lower().startswith("mpv.net"):
            continue
        for candidate in (os.path.join(location, EXE_NAME) if location else "",
                          icon.split(",")[0].strip('"')):
            if candidate and candidate.lower().endswith(EXE_NAME) and os.path.isfile(candidate):
                return candidate, version or None

    candidates = [_app_paths_exe(), shutil.which("mpvnet")]
    for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            sub = os.path.join("Programs", "mpv.net") if env == "LOCALAPPDATA" else "mpv.net"
            candidates.append(os.path.join(base, sub, EXE_NAME))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate, None
    return None, None


def config_dir(exe_path=None):
    """mpv.net reads portable_config next to the exe when that folder exists,
    otherwise %APPDATA%\\mpv.net."""
    if exe_path:
        portable = os.path.join(os.path.dirname(exe_path), "portable_config")
        if os.path.isdir(portable):
            return portable
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "mpv.net")


def bundled_shaders():
    folder = os.path.join(bundle_dir(), "Shaders")
    try:
        return sorted(f for f in os.listdir(folder) if f.lower().endswith(".glsl"))
    except OSError:
        return []


def bundled_profiles():
    """[(profile, [shader file stems in load order])] from the bundled mpv.conf,
    so the tab always shows what Apply actually installs."""
    try:
        with open(os.path.join(bundle_dir(), "mpv.conf"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    profiles, current = [], None
    for line in lines:
        line = line.strip()
        header = re.fullmatch(r"\[([^\]]+)\]", line)
        if header:
            current = header.group(1)
        elif current and line.startswith("glsl-shaders="):
            value = line.split("=", 1)[1].strip().strip('"')
            stems = [os.path.splitext(os.path.basename(p))[0] for p in value.split(";") if p]
            profiles.append((current, stems))
    return profiles


def shaders_applied(cfg_dir):
    """Every bundled shader is in place and mpv.conf actually loads them."""
    shaders = bundled_shaders()
    if not shaders:
        return False
    if not all(os.path.isfile(os.path.join(cfg_dir, "Shaders", s)) for s in shaders):
        return False
    try:
        with open(os.path.join(cfg_dir, "mpv.conf"), encoding="utf-8", errors="replace") as f:
            conf = f.read()
    except OSError:
        return False
    return all(os.path.splitext(s)[0] in conf for s in shaders)


# ------------------------------------------------------------------ install

def winget_path():
    found = shutil.which("winget")
    if found:
        return found
    alias = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "winget.exe")
    return alias if os.path.isfile(alias) else None


def winget_install_command(winget):
    return [winget, "install", "--id", WINGET_ID, "--exact", "--source", "winget",
            "--silent", "--accept-package-agreements", "--accept-source-agreements"]


_SPINNER = set("-\\|/ ")


def clean_winget_line(raw):
    """winget redraws its spinner and progress bar with \\r and \\b; keep only the
    last frame of a line, or "" when that frame is just a spinner."""
    text = raw.replace("\b", "\r").split("\r")
    frames = [t.strip() for t in text if t.strip()]
    if not frames:
        return ""
    last = frames[-1]
    return "" if set(last) <= _SPINNER else last


def run_winget_install(on_line=None):
    """Blocking. Streams cleaned output lines to on_line; returns winget's exit code
    (-1 when winget is missing)."""
    winget = winget_path()
    if not winget:
        return -1
    proc = subprocess.Popen(
        winget_install_command(winget),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    buf = b""
    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
        if not chunk:
            break
        buf += chunk
        # Progress frames end in \r, real lines in \n -- report both as they arrive.
        while True:
            cut = max(buf.rfind(b"\n"), buf.rfind(b"\r"))
            if cut < 0:
                break
            piece, buf = buf[:cut + 1], buf[cut + 1:]
            for line in piece.decode("utf-8", errors="replace").split("\n"):
                cleaned = clean_winget_line(line)
                if cleaned and on_line:
                    on_line(cleaned)
    if buf and on_line:
        cleaned = clean_winget_line(buf.decode("utf-8", errors="replace"))
        if cleaned:
            on_line(cleaned)
    code = proc.wait()
    # Exit codes are HRESULTs; Windows hands them back as unsigned.
    return code & 0xFFFFFFFF if code < 0 else code


# -------------------------------------------------------------- the config

def _lua_pattern_escape(text):
    return re.sub(r"([\^\$\(\)%\.\[\]\*\+\-\?])", r"%\1", text)


def anime_folder_name(download_dir):
    """The folder the [anime] profile should match: the app's download folder.

    Lowercased the way Lua's string.lower does it (ASCII only), since the
    profile-cond lowercases the path before matching.
    """
    name = os.path.basename(os.path.normpath(download_dir or "")) if download_dir else ""
    if not name or name.endswith(":"):
        return ANIME_FOLDER
    return "".join(c.lower() if c.isascii() else c for c in name)


def render_mpv_conf(text, download_dir):
    """Point the anime/series profile-conds at the user's download folder."""
    folder = anime_folder_name(download_dir)
    if folder == ANIME_FOLDER:
        return text
    old = f"[\\\\/]{ANIME_FOLDER}[\\\\/]"
    new = f"[\\\\/]{_lua_pattern_escape(folder)}[\\\\/]"
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("profile-cond="):
            line = line.replace(old, new)
        elif line.startswith("#") and f'"{ANIME_FOLDER}"' in line:
            line = line.replace(f'"{ANIME_FOLDER}"', f'"{folder}"')
        lines.append(line)
    return "".join(lines)


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def apply_config(cfg_dir, download_dir=None):
    """Copy the bundled config into cfg_dir.

    Every file that would be overwritten with different content is first copied
    to cfg_dir/backup/<timestamp>/ (same relative path). Returns that backup
    folder, or None when nothing needed backing up.
    """
    src_root = bundle_dir()
    plan = []   # (relative path, bytes to write)
    for name in BUNDLE_FILES:
        data = _read_bytes(os.path.join(src_root, name))
        if data is None:
            continue
        if name == "mpv.conf":
            data = render_mpv_conf(data.decode("utf-8"), download_dir).encode("utf-8")
        plan.append((name, data))
    for folder in BUNDLE_DIRS:
        base = os.path.join(src_root, folder)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            data = _read_bytes(os.path.join(base, name))
            if data is not None:
                plan.append((os.path.join(folder, name), data))
    if not plan:
        raise FileNotFoundError(f"The bundled mpv.net config is missing: {src_root}")

    backup = os.path.join(cfg_dir, "backup", time.strftime("%Y-%m-%d_%H%M%S"))
    backed_up = False
    for rel, data in plan:
        target = os.path.join(cfg_dir, rel)
        current = _read_bytes(target)
        if current is not None and current != data:
            dest = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(target, dest)
            backed_up = True

    for rel, data in plan:
        target = os.path.join(cfg_dir, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
    for folder in EMPTY_DIRS:
        os.makedirs(os.path.join(cfg_dir, folder), exist_ok=True)
    return backup if backed_up else None
