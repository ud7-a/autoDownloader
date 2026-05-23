import os
import sys
import shutil
import subprocess
import ctypes
import time

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

    # 3. Handle installation using a detached helper VBScript to prevent Windows file locks!
    try:
        launcher_exe = sys.executable
        launcher_dir = os.path.dirname(launcher_exe)
        launcher_name = os.path.basename(launcher_exe)

        # Copy the new files to a temporary extraction folder first
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        shutil.copytree(src_dir, temp_extract_dir)
        
        # Write helper VBScript to User's Temp directory
        temp_dir = os.environ.get("TEMP", os.environ.get("TMP", "C:\\"))
        vbs_path = os.path.join(temp_dir, "install_helper.vbs")
        
        # Create a double-safe VBScript file that runs completely headless using wscript.exe
        vbs_content = """Dim fso, logFile, logPath
Set fso = CreateObject("Scripting.FileSystemObject")
logPath = "C:\\Auto Episodes Downloader\\install_log.txt"

On Error Resume Next
Set logFile = fso.CreateTextFile(logPath, True)
logFile.WriteLine "=== AUTO DOWNLOADER INSTALL HELPER LOG ==="
logFile.WriteLine "Date/Time: " & Now()
logFile.WriteLine "Launcher Path: {launcher_exe}"
logFile.WriteLine "Launcher Dir: {launcher_dir}"
logFile.WriteLine "Launcher Name: {launcher_name}"
logFile.WriteLine "Destination Dir: {dest_dir}"
logFile.WriteLine "Temp Extract Dir: {temp_extract_dir}"

Set WshShell = CreateObject("WScript.Shell")
WScript.Sleep 2000

' 0. Optional: Automatically whitelist installation folder in Windows Defender
' We only request UAC Administrator rights for this if it's the very first manual installation!
If LCase("{is_silent_update}") = "false" Then
    logFile.WriteLine "Step 0: Requesting UAC elevation to add Windows Defender Exclusion..."
    Set shellApp = CreateObject("Shell.Application")
    Dim rootDir, psExclusionCmd, qt
    qt = Chr(34)
    rootDir = fso.GetParentFolderName("{dest_dir}")
    psExclusionCmd = "-NoProfile -WindowStyle Hidden -Command " & qt & "Add-MpPreference -ExclusionPath '" & rootDir & "'" & qt
    ' 0 = Hide window
    Err.Clear
    shellApp.ShellExecute "powershell.exe", psExclusionCmd, "", "runas", 0
    If Err.Number <> 0 Then
        logFile.WriteLine "User denied UAC elevation or execution failed: " & Err.Description
        Err.Clear
    Else
        logFile.WriteLine "UAC elevation requested. Folder whitelisted in Defender."
    End If
    ' Give Windows a moment to process the UAC command asynchronously
    WScript.Sleep 3000
End If

' 1. Force terminate any active app sessions
logFile.WriteLine "Step 1: Killing running AutoDownloader.exe and autoDownload.exe instances..."
WshShell.Run "taskkill /F /IM AutoDownloader.exe", 0, True
WshShell.Run "taskkill /F /IM autoDownload.exe", 0, True
WScript.Sleep 1000

' Legacy update unlock bypass: If installer is running inside the dest folder, move it out to unlock the folder
Dim isLegacyUpdate, targetSetupPath
isLegacyUpdate = False
If (LCase("{launcher_name}") = "autodownloader.exe" Or LCase("{launcher_name}") = "autodownload.exe") And LCase("{launcher_dir}") = LCase("{dest_dir}") Then
    isLegacyUpdate = True
    targetSetupPath = fso.GetParentFolderName("{dest_dir}") & "\\AutoDownloader_Setup.exe"
    logFile.WriteLine "Legacy update detected. Moving running installer to: " & targetSetupPath
    If fso.FileExists(targetSetupPath) Then
        fso.DeleteFile targetSetupPath, True
    End If
    fso.MoveFile "{dest_dir}\\" & "{launcher_name}", targetSetupPath
End If
WScript.Sleep 1000

' 2. Safely wipe the old installation folder if it exists
logFile.WriteLine "Step 2: Wiping destination folder if exists..."
If fso.FolderExists("{dest_dir}") Then
    fso.DeleteFolder "{dest_dir}", True
    logFile.WriteLine "Destination folder wiped."
Else
    logFile.WriteLine "Destination folder did not exist."
End If
WScript.Sleep 1000

' 3. Sweep the fresh update files into the permanent App directory
logFile.WriteLine "Step 3: Copying folder from temp to destination..."
fso.CopyFolder "{temp_extract_dir}", "{dest_dir}", True
If Err.Number <> 0 Then
    logFile.WriteLine "ERROR Copying folder: " & Err.Description
    Err.Clear
Else
    logFile.WriteLine "Folder copied successfully."
End If
WScript.Sleep 1000

' 4. Clean up the temporary directory
logFile.WriteLine "Step 4: Cleaning up temp directory..."
fso.DeleteFolder "{temp_extract_dir}", True
WScript.Sleep 1000

' 5. Re-generate a clean Desktop Shortcut
logFile.WriteLine "Step 5: Creating Desktop Shortcut..."
desktopPath = WshShell.SpecialFolders("Desktop")
Set Shortcut = WshShell.CreateShortcut(desktopPath & "\\AutoDownloader.lnk")
Shortcut.TargetPath = "{dest_dir}\\AutoDownloader.exe"
Shortcut.WorkingDirectory = "{dest_dir}"
Shortcut.IconLocation = "{dest_dir}\\AutoDownloader.exe,0"
Shortcut.Save()
logFile.WriteLine "Shortcut saved to: " & desktopPath & "\\AutoDownloader.lnk"

' 6. Clean up the old launcher's folder (reversing updater's rename and deleting old backup)
logFile.WriteLine "Step 6: Deleting .old files and cleaning up launcher..."

' Location A: Dest Dir (.old sweeps)
If fso.FileExists("{dest_dir}\\AutoDownloader.exe.old") Then
    Err.Clear
    fso.DeleteFile "{dest_dir}\\AutoDownloader.exe.old", True
    If Err.Number <> 0 Then logFile.WriteLine "Error deleting AutoDownloader.exe.old in dest: " & Err.Description Else logFile.WriteLine "Deleted AutoDownloader.exe.old in destination directory."
End If
If fso.FileExists("{dest_dir}\\autoDownload.exe.old") Then
    Err.Clear
    fso.DeleteFile "{dest_dir}\\autoDownload.exe.old", True
    If Err.Number <> 0 Then logFile.WriteLine "Error deleting autoDownload.exe.old in dest: " & Err.Description Else logFile.WriteLine "Deleted autoDownload.exe.old in destination directory."
End If

' Location B: Launcher Dir (.old sweeps)
If fso.FileExists("{launcher_dir}\\AutoDownloader.exe.old") Then
    Err.Clear
    fso.DeleteFile "{launcher_dir}\\AutoDownloader.exe.old", True
    If Err.Number <> 0 Then logFile.WriteLine "Error deleting AutoDownloader.exe.old in launcher: " & Err.Description Else logFile.WriteLine "Deleted AutoDownloader.exe.old in launcher directory."
End If
If fso.FileExists("{launcher_dir}\\autoDownload.exe.old") Then
    Err.Clear
    fso.DeleteFile "{launcher_dir}\\autoDownload.exe.old", True
    If Err.Number <> 0 Then logFile.WriteLine "Error deleting autoDownload.exe.old in launcher: " & Err.Description Else logFile.WriteLine "Deleted autoDownload.exe.old in launcher directory."
End If

' Location C: Parent Dir of Launcher Dir (.old sweeps)
parentDir = fso.GetParentFolderName("{launcher_dir}")
If fso.FileExists(parentDir & "\\AutoDownloader.exe.old") Then
    Err.Clear
    fso.DeleteFile parentDir & "\\AutoDownloader.exe.old", True
    If Err.Number <> 0 Then logFile.WriteLine "Error deleting AutoDownloader.exe.old in parent: " & Err.Description Else logFile.WriteLine "Deleted AutoDownloader.exe.old in parent of launcher directory."
End If
If fso.FileExists(parentDir & "\\autoDownload.exe.old") Then
    Err.Clear
    fso.DeleteFile parentDir & "\\autoDownload.exe.old", True
    If Err.Number <> 0 Then logFile.WriteLine "Error deleting autoDownload.exe.old in parent: " & Err.Description Else logFile.WriteLine "Deleted autoDownload.exe.old in parent of launcher directory."
End If

' Rename launcher outside dest if needed
logFile.WriteLine "Launcher name lower: " & LCase("{launcher_name}")
If Not isLegacyUpdate And (LCase("{launcher_name}") = "autodownloader.exe" Or LCase("{launcher_name}") = "autodownload.exe") Then
    logFile.WriteLine "Launcher was renamed by updater. Performing rename back to AutoDownloader_Setup.exe..."
    If fso.FileExists("{launcher_dir}\\" & "{launcher_name}") Then
        ' MoveFile fails if destination already exists, so delete it first!
        If fso.FileExists("{launcher_dir}\\AutoDownloader_Setup.exe") Then
            fso.DeleteFile "{launcher_dir}\\AutoDownloader_Setup.exe", True
        End If
        Err.Clear
        fso.MoveFile "{launcher_dir}\\" & "{launcher_name}", "{launcher_dir}\\AutoDownloader_Setup.exe"
        If Err.Number <> 0 Then 
            logFile.WriteLine "ERROR renaming launcher: " & Err.Description 
        Else 
            logFile.WriteLine "Successfully renamed launcher to AutoDownloader_Setup.exe"
        End If
    Else
        logFile.WriteLine "Launcher file not found for rename."
    End If
End If

' 7. Force Windows Explorer to refresh the Desktop and folders
logFile.WriteLine "Step 7: Forcing Windows Explorer to refresh Desktop icons..."
WshShell.Run "ie4uinit.exe -show", 0, False
Dim psRefreshCmd, qt
qt = Chr(34)
psRefreshCmd = "powershell -NoProfile -WindowStyle Hidden -Command " & qt & "$code = '[DllImport(\\" & qt & "shell32.dll\\" & qt & ")] public static extern void SHChangeNotify(uint wEventId, uint uFlags, IntPtr dwItem1, IntPtr dwItem2);'; $type = Add-Type -MemberDefinition $code -Name 'Shell' -PassThru; $type::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)" & qt
WshShell.Run psRefreshCmd, 0, True
WScript.Sleep 500

' 8. Boot the fresh application cleanly
logFile.WriteLine "Step 8: Launching new application..."
WshShell.Run Chr(34) & desktopPath & "\\AutoDownloader.lnk" & Chr(34), 1, False

' 9. Close and clean up
logFile.WriteLine "Step 9: Finalizing and deleting helper script..."
logFile.Close
fso.DeleteFile WScript.ScriptFullName, True
"""
        # Cleanly replace all template placeholders safely
        vbs_content = (vbs_content
                       .replace("{is_silent_update}", str(is_silent_update))
                       .replace("{dest_dir}", dest_dir)
                       .replace("{temp_extract_dir}", temp_extract_dir)
                       .replace("{launcher_dir}", launcher_dir)
                       .replace("{launcher_name}", launcher_name)
                       .replace("{launcher_exe}", launcher_exe))

        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_content)
            
        # Launch VBScript headless using Windows scripting host wscript.exe
        # This runs 100% in the background, showing absolutely zero console windows or CMD flashes!
        subprocess.Popen(
            ["wscript.exe", vbs_path],
            close_fds=True
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
