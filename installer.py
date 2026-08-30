import os
import sys
import shutil
import subprocess
import ctypes

def main():
    # 1. Determine where PyInstaller extracted the bundled directory
    if not getattr(sys, 'frozen', False):
        print("This installer must be compiled to run.")
        return

    src_dir = os.path.join(sys._MEIPASS, "AutoDownloader")
    dest_dir = r"C:\Auto Episodes Downloader\App"
    temp_extract_dir = r"C:\Auto Episodes Downloader\App_temp"

    # Check if running as an automatic/background update
    # If the installer is launched directly from the App directory (renamed as AutoDownloader.exe by the updater),
    # we run in 100% silent update mode.
    is_silent_update = "Auto Episodes Downloader" in sys.executable or "--silent" in sys.argv

    title = "Auto Episodes Downloader Setup"

    if not is_silent_update:
        # Prompt user to confirm fresh installation
        message = "This will install (or update) Auto Episodes Downloader on your system.\n\nDo you want to proceed?"
        res = ctypes.windll.user32.MessageBoxW(0, message, title, 4 | 64) # MB_YESNO | MB_ICONINFORMATION
        if res != 6: # 6 is IDYES
            sys.exit(0)

    # 3. Handle installation using a detached helper PowerShell script.
    #    PowerShell is used instead of VBScript because VBScript is deprecated and
    #    absent on modern Windows (Win11 24H2+, Windows Sandbox, hardened/enterprise
    #    machines with WSH disabled) -- a .vbs helper silently fails to run there,
    #    leaving the app un-deployed. PowerShell ships with every supported Windows.
    #    Detaching the deploy into a separate process avoids file locks on the
    #    running installer/app during the swap.
    try:
        launcher_exe = sys.executable
        launcher_dir = os.path.dirname(launcher_exe)
        launcher_name = os.path.basename(launcher_exe)

        # Copy the new files to a temporary extraction folder first
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        shutil.copytree(src_dir, temp_extract_dir)

        # Write helper PowerShell script to the user's Temp directory
        temp_dir = os.environ.get("TEMP", os.environ.get("TMP", "C:\\"))
        ps1_path = os.path.join(temp_dir, "install_helper.ps1")

        ps_content = r"""$ErrorActionPreference = 'SilentlyContinue'

$destDir        = '{dest_dir}'
$tempExtractDir = '{temp_extract_dir}'
$launcherExe    = '{launcher_exe}'
$launcherDir    = '{launcher_dir}'
$launcherName   = '{launcher_name}'
$isSilentUpdate = '{is_silent_update}'
$rootDir        = Split-Path -Parent $destDir
$stageDir       = $destDir + '_new'
$logPath        = Join-Path $rootDir 'install_log.txt'
$ln             = $launcherName.ToLower()

function Log($m) { try { Add-Content -LiteralPath $logPath -Value $m } catch {} }

try { New-Item -ItemType Directory -Force -Path $rootDir | Out-Null } catch {}
try { Set-Content -LiteralPath $logPath -Value "=== AUTO DOWNLOADER INSTALL HELPER LOG (PowerShell) ===" } catch {}
Log ("Date/Time: " + (Get-Date))
Log ("Launcher Path: " + $launcherExe)
Log ("Launcher Dir: " + $launcherDir)
Log ("Launcher Name: " + $launcherName)
Log ("Destination Dir: " + $destDir)
Log ("Temp Extract Dir: " + $tempExtractDir)

Start-Sleep -Seconds 2

# 0. Optionally whitelist the install folder in Windows Defender (fresh install only; needs admin).
if ($isSilentUpdate -eq 'False') {
    Log "Step 0: Requesting UAC elevation to add Windows Defender Exclusion..."
    try {
        $ex = "Add-MpPreference -ExclusionPath '" + $rootDir + "'"
        Start-Process powershell.exe -Verb RunAs -WindowStyle Hidden -ArgumentList @('-NoProfile','-WindowStyle','Hidden','-Command', $ex) -ErrorAction Stop
        Log "UAC elevation requested. Folder whitelisted in Defender."
    } catch {
        Log ("User denied UAC elevation or execution failed: " + $_.Exception.Message)
    }
    Start-Sleep -Seconds 3
}

# 1. Force terminate any active app sessions
Log "Step 1: Killing running AutoDownloader.exe and autoDownload.exe instances..."
try { taskkill /F /IM AutoDownloader.exe 2>$null | Out-Null } catch {}
try { taskkill /F /IM autoDownload.exe 2>$null | Out-Null } catch {}
Start-Sleep -Seconds 1

# Legacy update unlock: if the installer is running from inside the dest folder, move it out to unlock the folder.
$isLegacyUpdate = $false
if (($ln -eq 'autodownloader.exe' -or $ln -eq 'autodownload.exe') -and ($launcherDir.ToLower() -eq $destDir.ToLower())) {
    $isLegacyUpdate = $true
    $targetSetupPath = Join-Path $rootDir 'AutoDownloader_Setup.exe'
    Log ("Legacy update detected. Moving running installer to: " + $targetSetupPath)
    if (Test-Path -LiteralPath $targetSetupPath) { Remove-Item -LiteralPath $targetSetupPath -Force }
    try { Move-Item -LiteralPath (Join-Path $destDir $launcherName) -Destination $targetSetupPath -Force -ErrorAction Stop }
    catch { Log ("Move launcher out failed: " + $_.Exception.Message) }
}
Start-Sleep -Seconds 1

# 2. Stage the fresh files into a side folder with retries, so a transient file lock
#    (AV scan / Explorer / running instance) can never leave the destination wiped-but-empty.
Log ("Step 2: Staging fresh files into: " + $stageDir)
if (Test-Path -LiteralPath $stageDir) { Remove-Item -LiteralPath $stageDir -Recurse -Force }

$copyOk = $false
for ($attempt = 1; $attempt -le 5; $attempt++) {
    try {
        New-Item -ItemType Directory -Force -Path $stageDir | Out-Null
        Copy-Item -Path (Join-Path $tempExtractDir '*') -Destination $stageDir -Recurse -Force -ErrorAction Stop
    } catch {
        Log ("Staging copy attempt " + $attempt + " threw: " + $_.Exception.Message)
    }
    # Verify the copy actually landed the app core, not just returned without error
    if (Test-Path -LiteralPath (Join-Path $stageDir 'AutoDownloader.exe')) {
        $copyOk = $true
        Log ("Staging copy succeeded on attempt " + $attempt + ".")
        break
    }
    Log ("Staging copy attempt " + $attempt + " failed (AutoDownloader.exe not present).")
    if (Test-Path -LiteralPath $stageDir) { Remove-Item -LiteralPath $stageDir -Recurse -Force }
    Start-Sleep -Seconds 2
}

# 3. Only swap into place if staging is verified good. Otherwise keep the existing install intact.
if ($copyOk) {
    Log "Step 3: Swapping staged files into destination..."
    if (Test-Path -LiteralPath $destDir) {
        try { Remove-Item -LiteralPath $destDir -Recurse -Force -ErrorAction Stop }
        catch { Log ("Warning wiping old dest: " + $_.Exception.Message) }
    }
    Start-Sleep -Milliseconds 500
    try {
        # Move-Item into a directory that still exists puts the source INSIDE it,
        # producing App\App_new -- a full second copy of the app that nothing ever
        # cleans up. The wipe above can fail (a locked directory handle is enough, an
        # open Explorer window will do it), so refuse to move unless it really went.
        if (Test-Path -LiteralPath $destDir) { throw "destination still present; copying instead of moving" }
        Move-Item -LiteralPath $stageDir -Destination $destDir -Force -ErrorAction Stop
        Log "Swap succeeded."
    } catch {
        Log ("MoveFolder failed (" + $_.Exception.Message + "). Falling back to CopyFolder...")
        try {
            New-Item -ItemType Directory -Force -Path $destDir | Out-Null
            Copy-Item -Path (Join-Path $stageDir '*') -Destination $destDir -Recurse -Force -ErrorAction Stop
            Log "Fallback copy succeeded."
            if (Test-Path -LiteralPath $stageDir) { Remove-Item -LiteralPath $stageDir -Recurse -Force }
        } catch {
            Log ("ERROR: Fallback copy failed: " + $_.Exception.Message)
        }
    }
} else {
    Log "FATAL: Could not stage new files after 5 attempts. Keeping existing installation intact."
}
Start-Sleep -Seconds 1

# 4. Clean up the temporary extraction directory
Log "Step 4: Cleaning up temp directory..."
if (Test-Path -LiteralPath $tempExtractDir) { Remove-Item -LiteralPath $tempExtractDir -Recurse -Force }

# 4b. Remove leftovers from this update or an older one: the staging folder, the
# nested copy an earlier build could create inside the app folder, and any App_prev_*
# backups. Left alone these quietly double the install size -- 579 MB of orphans was
# seen on one machine against a 215 MB app.
#
# Only these exact shapes are touched. The same directory holds the live user data:
# sites_config.json, download_history.db, SeleniumProfile and watchlist_covers.
foreach ($leftover in @($stageDir, (Join-Path $destDir 'App_new'))) {
    if (Test-Path -LiteralPath $leftover) {
        try {
            Remove-Item -LiteralPath $leftover -Recurse -Force -ErrorAction Stop
            Log ("Removed leftover: " + $leftover)
        } catch { Log ("Could not remove " + $leftover + ": " + $_.Exception.Message) }
    }
}
Get-ChildItem -LiteralPath $rootDir -Directory -Filter 'App_prev_*' -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop
        Log ("Removed old backup: " + $_.Name)
    } catch { Log ("Could not remove " + $_.Name + ": " + $_.Exception.Message) }
}
Start-Sleep -Seconds 1

# 5. Re-generate a clean Desktop Shortcut
Log "Step 5: Creating Desktop Shortcut..."
$desktopPath = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktopPath 'AutoDownloader.lnk'
try {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnkPath)
    $sc.TargetPath = (Join-Path $destDir 'AutoDownloader.exe')
    $sc.WorkingDirectory = $destDir
    $sc.IconLocation = ((Join-Path $destDir 'AutoDownloader.exe') + ',0')
    $sc.Save()
    Log ("Shortcut saved to: " + $lnkPath)
} catch {
    Log ("Shortcut creation failed: " + $_.Exception.Message)
}

# 6. Clean up the old launcher's folder (reversing updater's rename and deleting old backups)
Log "Step 6: Deleting .old files and cleaning up launcher..."
$parentOfLauncher = Split-Path -Parent $launcherDir
$oldTargets = @(
    (Join-Path $destDir 'AutoDownloader.exe.old'),
    (Join-Path $destDir 'autoDownload.exe.old'),
    (Join-Path $launcherDir 'AutoDownloader.exe.old'),
    (Join-Path $launcherDir 'autoDownload.exe.old'),
    (Join-Path $parentOfLauncher 'AutoDownloader.exe.old'),
    (Join-Path $parentOfLauncher 'autoDownload.exe.old')
)
foreach ($t in $oldTargets) {
    if (Test-Path -LiteralPath $t) {
        try { Remove-Item -LiteralPath $t -Force -ErrorAction Stop; Log ("Deleted " + $t) }
        catch { Log ("Error deleting " + $t + ": " + $_.Exception.Message) }
    }
}

# Reverse the updater's rename: if we were launched as AutoDownloader.exe, rename back to the setup name.
if ((-not $isLegacyUpdate) -and ($ln -eq 'autodownloader.exe' -or $ln -eq 'autodownload.exe')) {
    Log "Launcher was renamed by updater. Renaming back to AutoDownloader_Setup.exe..."
    $srcLauncher = Join-Path $launcherDir $launcherName
    $dstLauncher = Join-Path $launcherDir 'AutoDownloader_Setup.exe'
    if (Test-Path -LiteralPath $srcLauncher) {
        if (Test-Path -LiteralPath $dstLauncher) { Remove-Item -LiteralPath $dstLauncher -Force }
        try { Move-Item -LiteralPath $srcLauncher -Destination $dstLauncher -Force -ErrorAction Stop; Log "Successfully renamed launcher to AutoDownloader_Setup.exe" }
        catch { Log ("ERROR renaming launcher: " + $_.Exception.Message) }
    } else {
        Log "Launcher file not found for rename."
    }
}

# 7. Force Windows Explorer to refresh the Desktop and folders
Log "Step 7: Forcing Windows Explorer to refresh Desktop icons..."
try { Start-Process 'ie4uinit.exe' -ArgumentList '-show' -WindowStyle Hidden } catch {}
try {
    $sig = '[DllImport("shell32.dll")] public static extern void SHChangeNotify(uint wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);'
    $shell = Add-Type -MemberDefinition $sig -Name 'ShellNotify' -Namespace 'Win32InstallHelper' -PassThru
    $shell::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
} catch {}
Start-Sleep -Milliseconds 500

# 8. Boot the fresh application cleanly
Log "Step 8: Launching new application..."
$appExe = Join-Path $destDir 'AutoDownloader.exe'
try {
    if (Test-Path -LiteralPath $appExe) {
        Start-Process -FilePath $appExe -WorkingDirectory $destDir
    } elseif (Test-Path -LiteralPath $lnkPath) {
        Start-Process -FilePath $lnkPath
    }
} catch {
    Log ("Launch failed: " + $_.Exception.Message)
}

# 9. Close and clean up
Log "Step 9: Finalizing and deleting helper script..."
try { Remove-Item -LiteralPath $PSCommandPath -Force } catch {}
"""
        # Cleanly replace all template placeholders safely
        ps_content = (ps_content
                      .replace("{is_silent_update}", str(is_silent_update))
                      .replace("{dest_dir}", dest_dir)
                      .replace("{temp_extract_dir}", temp_extract_dir)
                      .replace("{launcher_dir}", launcher_dir)
                      .replace("{launcher_name}", launcher_name)
                      .replace("{launcher_exe}", launcher_exe))

        with open(ps1_path, "w", encoding="utf-8") as f:
            f.write(ps_content)

        # Launch the helper detached and headless via PowerShell. -ExecutionPolicy Bypass
        # neutralizes a Restricted machine policy; CREATE_NO_WINDOW hides the console flash.
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", ps1_path],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )

        if not is_silent_update:
            ctypes.windll.user32.MessageBoxW(
                0,
                "Auto Episodes Downloader has been successfully installed!\n\nA shortcut has been created on your Desktop.",
                title,
                0 | 64 # MB_OK | MB_ICONINFORMATION
            )
        sys.exit(0)

    except Exception as e:
        if not is_silent_update:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"Installation Failed!\n\nCould not install files: {e}\n\nPlease close the app and try again.",
                title,
                0 | 16 # MB_OK | MB_ICONERROR
            )
        sys.exit(1)

if __name__ == "__main__":
    main()
