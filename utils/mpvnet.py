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


# ----------------------------------------------------------- default player
#
# Windows protects the "default app" choice (the UserChoice key carries a hash
# only Windows itself can write), so no program may set it silently. What an app
# can do -- and what this does -- is register itself as a video player and set
# Windows' default-associations policy (see below), which Windows applies itself.

# What the app downloads; these decide "is mpv.net the default player".
DEFAULT_CHECK_EXTS = (".mp4", ".mkv")
ERROR_CANCELLED = 1223              # the UAC prompt was declined


def _user_choice_progid(ext):
    try:
        import winreg
    except ImportError:
        return ""
    base = rf"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\{ext}"
    # Windows 11 24H2 moved the live choice to UserChoiceLatest; older builds use UserChoice.
    for sub in ("UserChoiceLatest", "UserChoice"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"{base}\{sub}") as key:
                value = winreg.QueryValueEx(key, "ProgId")[0]
        except OSError:
            continue
        if value:
            return str(value)
    return ""


def is_mpvnet_progid(progid):
    p = (progid or "").lower()
    return "mpvnet" in p or "mpv.net" in p


def default_player_status():
    """{".mp4": True/False, ...}: which of the app's file types open in mpv.net."""
    return {ext: is_mpvnet_progid(_user_choice_progid(ext)) for ext in DEFAULT_CHECK_EXTS}


def _registered_command():
    """The command Windows runs for mpv.net's registration, or ""."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                            rf"Applications\{EXE_NAME}\shell\open\command") as key:
            return str(winreg.QueryValueEx(key, "")[0] or "")
    except (ImportError, OSError):
        return ""


def is_registered(exe=None):
    """mpv.net is registered as a media player FOR the app's file types, pointing
    at the mpv.net that is installed now.

    Being listed under RegisteredApplications is not enough: a registration made
    without an extension list (see register_video_associations) leaves Settings
    with a page that offers nothing to set, and one left over from an earlier
    install (Program Files -> AppData, say) opens a player that no longer exists.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Clients\Media\mpv.net\Capabilities\FileAssociations") as key:
            for ext in DEFAULT_CHECK_EXTS:
                winreg.QueryValueEx(key, ext)
    except (ImportError, OSError):
        return False
    if exe:
        command = _registered_command().lower()
        if os.path.normcase(exe).lower() not in command:
            return False
    return True


# ------------------------------------------------------------ play files
#
# The app's own "Start Watching" and History don't need the Windows default at
# all: with mpv.net installed they launch it directly. That part is fully
# automatic; the Windows default only matters for double-clicks in Explorer.

PLAY_SETTING = "play_in_mpvnet"


def play_in_mpvnet_enabled():
    from utils.config import app_settings, config_lock
    with config_lock:
        return bool(app_settings.get(PLAY_SETTING, True))


def open_videos(files):
    """Play `files` (first one starts, the rest are queued) in mpv.net when it is
    installed and the setting is on; otherwise hand the first to Windows.
    Returns "mpvnet" or "default"."""
    files = [f for f in files if f]
    if not files:
        raise FileNotFoundError("nothing to play")
    exe = find_mpvnet()[0] if play_in_mpvnet_enabled() else None
    if exe and all(os.path.isfile(f) for f in files):
        subprocess.Popen([exe, *files], close_fds=True)
        return "mpvnet"
    os.startfile(files[0])
    return "default"


def run_elevated(exe, args, timeout_ms=180000, show=1):
    """Run exe with admin rights (Windows shows the UAC prompt) and wait for it.

    Returns the process exit code, or ERROR_CANCELLED when the prompt is declined.
    """
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = args
    info.nShow = show
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return ctypes.get_last_error() or ERROR_CANCELLED
    if not info.hProcess:
        return 0
    try:
        kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel32.CloseHandle(info.hProcess)


# mpv.net's default `video-exts` (its own menu registers these).
VIDEO_EXTS = ("3g2", "3gp", "avi", "flv", "m2ts", "m4v", "mj2", "mkv", "mov", "mp4",
              "mpeg", "mpg", "ogv", "rmvb", "ts", "webm", "wmv", "y4m")


def register_video_args():
    # mpv.net reads `<type> <ext> <ext> ...` and registers every word after the flag
    # as an extension -- the type included. Passing the type alone registered a
    # single bogus ".video" type and left Settings with nothing to set as default.
    return "--register-file-associations video " + " ".join(VIDEO_EXTS)


def register_video_associations(exe):
    """mpv.net's own registration (same as its menu: Settings > Setup), elevated."""
    return run_elevated(exe, register_video_args())


# ---- automatic: Windows' default-associations policy ----------------------
#
# What Windows 11 still allows an app to do (checked on build 26200):
#   * set the default itself           -> "go to Settings" message
#   * open the Open With picker         -> only "Just once", no "Always"
#   * Settings > mpv.net > .mp4         -> works, but it is the user's click
#   * this policy                       -> works, applied by Windows at sign-in
# An admin points the "default associations configuration file" policy at an
# XML file, and Windows itself writes the choice at each sign-in. Nothing here
# touches the protected UserChoice key or its hash.

POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Windows\System"
POLICY_VALUE = "DefaultAssociationsConfiguration"


def policy_xml_path():
    base = os.environ.get("ProgramData", r"C:\ProgramData")
    return os.path.join(base, "Auto Episodes Downloader", "mpvnet-default-associations.xml")


def build_policy_xml(exts=DEFAULT_CHECK_EXTS):
    rows = "\n".join(
        f'  <Association Identifier="{ext}" ProgId="mpvnet{ext}" ApplicationName="mpv.net" />'
        for ext in exts)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<DefaultAssociations>\n{rows}\n</DefaultAssociations>\n'


def policy_active():
    """The policy points at our XML, and the XML is there."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY) as key:
            value = str(winreg.QueryValueEx(key, POLICY_VALUE)[0] or "")
    except (ImportError, OSError):
        return False
    return (os.path.normcase(value) == os.path.normcase(policy_xml_path())
            and os.path.isfile(value))


def _ps(text):
    return "'" + str(text).replace("'", "''") + "'"


def build_enable_script(exe, register):
    """PowerShell run once, elevated: (re)register mpv.net if needed, write the
    XML, point the policy at it. Exit 0 on success."""
    xml_path = policy_xml_path()
    lines = ["$ErrorActionPreference = 'Stop'"]
    if register:
        args = register_video_args().split(" ")
        lines.append(f"Start-Process -FilePath {_ps(exe)} -ArgumentList "
                     f"{', '.join(_ps(a) for a in args)} -Wait")
    lines += [
        f"New-Item -ItemType Directory -Force -Path {_ps(os.path.dirname(xml_path))} | Out-Null",
        f"Set-Content -LiteralPath {_ps(xml_path)} -Encoding UTF8 -Value {_ps(build_policy_xml())}",
        # Only create the key if it is missing -- New-Item -Force on an existing
        # key would wipe the other policies stored there.
        f"$key = {_ps('HKLM:\\' + POLICY_KEY)}",
        "if (-not (Test-Path $key)) { New-Item -Path $key | Out-Null }",
        f"Set-ItemProperty -Path $key -Name {_ps(POLICY_VALUE)} -Value {_ps(xml_path)}",
        "exit 0",
    ]
    return "\n".join(lines)


def build_disable_script():
    xml_path = policy_xml_path()
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$key = {_ps('HKLM:\\' + POLICY_KEY)}",
        f"$v = (Get-ItemProperty -Path $key -ErrorAction SilentlyContinue).{POLICY_VALUE}",
        # Only remove the policy if it is ours; an admin may have set their own.
        f"if ($v -and ($v -eq {_ps(xml_path)})) {{ Remove-ItemProperty -Path $key -Name {_ps(POLICY_VALUE)} }}",
        f"if (Test-Path -LiteralPath {_ps(xml_path)}) {{ Remove-Item -LiteralPath {_ps(xml_path)} }}",
        "exit 0",
    ])


def _run_elevated_powershell(script):
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return run_elevated(ps, f"-NoProfile -NonInteractive -WindowStyle Hidden "
                            f"-EncodedCommand {encoded}", show=0)


def set_default_player_instant():
    """Apply the default player instantly without relying solely on Group Policy.
    This runs non-elevated to correctly target the current user's UserChoice keys."""
    sfta_path = os.path.join(bundle_dir(), "SFTA.ps1")
    if not os.path.isfile(sfta_path):
        return -1
    script = f"""
    $ErrorActionPreference = 'Stop'
    . {_ps(sfta_path)}
    Set-FTA -Extension '.mp4' -ProgId 'Applications\\{EXE_NAME}'
    Set-FTA -Extension '.mkv' -ProgId 'Applications\\{EXE_NAME}'
    exit 0
    """
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return subprocess.call([ps, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded], creationflags=CREATE_NO_WINDOW)


def enable_default_policy(exe):
    """One admin prompt: registration (if missing or stale) + the policy.
    Then, it instantly applies the associations without a reboot."""
    code = _run_elevated_powershell(build_enable_script(exe, not is_registered(exe)))
    if code == 0:
        set_default_player_instant()
    return code


def disable_default_policy():
    return _run_elevated_powershell(build_disable_script())


def default_apps_uri():
    """Settings > Default apps, opened on mpv.net's page: click .mp4 / .mkv there
    and "Set default" -- the immediate, manual alternative to the policy."""
    return "ms-settings:defaultapps?registeredAppMachine=mpv.net"


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
