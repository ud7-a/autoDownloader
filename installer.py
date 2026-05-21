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

    # 2. Terminate any running instances of the app
    subprocess.run("taskkill /F /IM AutoDownloader.exe /T", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)

    # 3. Handle installation using a detached helper batch file to prevent Windows file locks!
    try:
        # If the destination doesn't exist, we can copy files directly
        if not os.path.exists(dest_dir):
            os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
            shutil.copytree(src_dir, dest_dir)
            
            # Create Desktop Shortcut
            desktop_path = os.path.join(os.environ["USERPROFILE"], "Desktop")
            shortcut_path = os.path.join(desktop_path, "AutoDownloader.lnk")
            target_exe = os.path.join(dest_dir, "AutoDownloader.exe")
            
            ps_cmd = (
                f'$WshShell = New-Object -ComObject WScript.Shell; '
                f'$Shortcut = $WshShell.CreateShortcut("{shortcut_path}"); '
                f'$Shortcut.TargetPath = "{target_exe}"; '
                f'$Shortcut.WorkingDirectory = "{dest_dir}"; '
                f'$Shortcut.IconLocation = "{target_exe},0"; '
                f'$Shortcut.Save()'
            )
            subprocess.run(["powershell", "-Command", ps_cmd], check=True, creationflags=0x08000000)
            
            if not is_silent_update:
                ctypes.windll.user32.MessageBoxW(
                    0, 
                    "Auto Episodes Downloader has been successfully installed!\n\nA shortcut has been created on your Desktop.", 
                    title, 
                    0 | 64 # MB_OK | MB_ICONINFORMATION
                )
                
            # Launch the new app by double-clicking the newly created Desktop shortcut!
            try:
                os.startfile(shortcut_path)
            except Exception:
                subprocess.Popen([target_exe], cwd=dest_dir, close_fds=True)
            sys.exit(0)
            
        else:
            # If the destination exists (updating), we use our bulletproof helper VBScript to avoid black console popups!
            # copy the new files to a temporary extraction folder first
            if os.path.exists(temp_extract_dir):
                shutil.rmtree(temp_extract_dir)
            shutil.copytree(src_dir, temp_extract_dir)
            
            # Write helper VBScript to User's Temp directory
            temp_dir = os.environ.get("TEMP", os.environ.get("TMP", "C:\\"))
            vbs_path = os.path.join(temp_dir, "install_helper.vbs")
            
            # Create a double-safe VBScript file that runs completely headless using wscript.exe
            vbs_content = f"""Set WshShell = CreateObject("WScript.Shell")
WScript.Sleep 2000

' 1. Force terminate any active app sessions to release file locks
On Error Resume Next
WshShell.Run "taskkill /F /IM AutoDownloader.exe /T", 0, True
WScript.Sleep 1000

' 2. Safely wipe the old installation folder
Set fso = CreateObject("Scripting.FileSystemObject")
If fso.FolderExists("{dest_dir}") Then
    fso.DeleteFolder "{dest_dir}", True
End If
WScript.Sleep 1000

' 3. Sweep the fresh update files into the permanent App directory
fso.CopyFolder "{temp_extract_dir}", "{dest_dir}", True
WScript.Sleep 1000

' 4. Clean up the temporary directory
fso.DeleteFolder "{temp_extract_dir}", True

' 5. Re-generate a clean Desktop Shortcut
desktopPath = WshShell.SpecialFolders("Desktop")
Set Shortcut = WshShell.CreateShortcut(desktopPath & "\\AutoDownloader.lnk")
Shortcut.TargetPath = "{dest_dir}\\AutoDownloader.exe"
Shortcut.WorkingDirectory = "{dest_dir}"
Shortcut.IconLocation = "{dest_dir}\\AutoDownloader.exe,0"
Shortcut.Save()

' 6. Boot the fresh application cleanly by running the newly created Desktop shortcut!
WshShell.Run Chr(34) & desktopPath & "\\AutoDownloader.lnk" & Chr(34), 1, False

' 7. Self-delete this VBScript file cleanly
fso.DeleteFile WScript.ScriptFullName, True
"""
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
                    "Auto Episodes Downloader has been successfully updated!\n\nThe application will restart automatically in a few seconds.", 
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
