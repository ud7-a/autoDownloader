import os
import sys
import shutil
import subprocess

MAIN_FILE = "main.py"
EXE_NAME = "AutoDownloader"

# The exact flags used in the production pipeline
PYINSTALLER_CMD = [
    "pyinstaller", 
    "--noconfirm", 
    "--onedir", 
    "--windowed", 
    "--name", EXE_NAME, 
    "--collect-all", "selenium", 
    "--collect-all", "qfluentwidgets",
    "--collect-all", "numpy",
    "--hidden-import", "PyQt6.QtXml",
    "--hidden-import", "PyQt6.QtSvg",
    "--add-data", "assets;assets",
    MAIN_FILE
]

def clean_build_folders():
    """Removes old build/dist folders to ensure a clean compilation."""
    print(f"[KILL] Killing any running instances of {EXE_NAME}.exe...")
    subprocess.run(f"taskkill /F /IM {EXE_NAME}.exe /T", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    import time
    time.sleep(1) # Give Windows time to release NTFS file locks
    
    print("[CLEAN] Cleaning old build files...")
    for folder in ["build", "dist"]:
        if os.path.exists(folder):
            try:
                shutil.rmtree(folder)
            except Exception as e:
                print(f"[WARNING] Could not delete {folder}. It might be open in another program. ({e})")
            
    # Clean up old .spec files
    spec_file = f"{EXE_NAME}.spec"
    if os.path.exists(spec_file):
        os.remove(spec_file)

def generate_splash_image():
    """Generates a static splash screen image using the PyQt6 draw logic."""
    print("[SPLASH] Generating static splash screen image...")
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import Qt
        
        # We need a QApplication instance to create a QPixmap/QPainter
        app = QApplication.instance()
        if not app:
            app = QApplication([])
        
        # Import the function from main
        import sys
        sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
        from main import create_splash_pixmap
        
        pixmap = create_splash_pixmap()
        
        # Ensure assets folder exists
        os.makedirs("assets", exist_ok=True)
        splash_path = os.path.join("assets", "splash.png")
        
        # Save it
        pixmap.save(splash_path, "PNG")
        print(f"[SUCCESS] Generated splash image at {splash_path}")
    except Exception as e:
        print(f"[WARNING] Could not generate splash image dynamically: {e}")

def run_cmd(cmd):
    print(f"\n[RUNNING] {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if result.returncode != 0:
        print(f"\n[ERROR] Build failed!")
        sys.exit(1)

def deploy_app():
    import shutil
    import subprocess
    dest_dir = r"C:\Auto Episodes Downloader\App"
    print(f"\n🚀 [DEPLOY] Moving compiled folder to: {dest_dir}...")
    
    # 1. Clean up destination directory first
    if os.path.exists(dest_dir):
        try:
            shutil.rmtree(dest_dir)
        except Exception as e:
            print(f"[WARNING] Could not overwrite old installation folder: {e}")
            print("Make sure the app is closed and try again.")
            return

    # 2. Copy compiled folder
    src_dir = os.path.join("dist", EXE_NAME)
    try:
        shutil.copytree(src_dir, dest_dir)
        print(f"[SUCCESS] App successfully moved to: {dest_dir}")
    except Exception as e:
        print(f"[ERROR] Failed to move compiled folder: {e}")
        return

    # 3. Create Desktop Shortcut
    desktop_path = os.path.join(os.environ["USERPROFILE"], "Desktop")
    shortcut_path = os.path.join(desktop_path, f"{EXE_NAME}.lnk")
    target_exe = os.path.join(dest_dir, f"{EXE_NAME}.exe")
    
    print(f"🔗 [SHORTCUT] Generating Desktop shortcut at: {shortcut_path}...")
    
    # PowerShell command to create a shortcut cleanly without external python packages
    ps_cmd = (
        f'$WshShell = New-Object -ComObject WScript.Shell; '
        f'$Shortcut = $WshShell.CreateShortcut("{shortcut_path}"); '
        f'$Shortcut.TargetPath = "{target_exe}"; '
        f'$Shortcut.WorkingDirectory = "{dest_dir}"; '
        f'$Shortcut.IconLocation = "{target_exe},0"; '
        f'$Shortcut.Save()'
    )
    
    try:
        subprocess.run(["powershell", "-Command", ps_cmd], check=True, creationflags=0x08000000) # CREATE_NO_WINDOW
        print("[SUCCESS] Desktop shortcut created successfully!")
    except Exception as e:
        print(f"[WARNING] Failed to generate shortcut: {e}")

def main():
    print(">>> --- TEST BUILD: COMPILING EXE ONLY --- <<<\n")
    
    # 1. Start with a clean slate
    clean_build_folders()

    # 2. Compile the App
    print("\n[BUILD] Compiling .exe with PyInstaller (This may take a minute or two)...")
    run_cmd(PYINSTALLER_CMD)
    
    # 3. Verify & Deploy
    exe_path = os.path.join("dist", EXE_NAME, f"{EXE_NAME}.exe")
    if os.path.exists(exe_path):
        print(f"\n[SUCCESS] Your test executable is ready!")
        deploy_app()
        print("\n🎉 Deployment Complete!")
        print(f"You can now double-click the '{EXE_NAME}' icon on your Desktop to open it instantly!")
    else:
        print("\n[ERROR] Build failed! Could not find the .exe in the dist folder.")

if __name__ == "__main__":
    main()