import os
import re
import sys
import shutil
import io
import subprocess

# Force terminal UTF-8 encoding on Windows to prevent UnicodeEncodeError crashes when printing emojis!
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# --- NEW PROJECT STRUCTURE PATHS ---
MAIN_FILE = "main.py"
CONFIG_FILE = os.path.join("utils", "config.py")
EXE_NAME = "AutoDownloader"

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

INSTALLER_CMD = [
    "pyinstaller",
    "--noconfirm",
    "--onefile",
    "--windowed",
    "--name", f"{EXE_NAME}_Setup",
    "--add-data", f"dist/{EXE_NAME};{EXE_NAME}",
    "installer.py"
]

def run_cmd(cmd, check=True):
    print(f"\n[RUNNING] {' '.join(cmd)}")
    # Enable shell=True on Windows to support CLI shims like gh
    result = subprocess.run(cmd, text=True, shell=(sys.platform == "win32"))
    if check and result.returncode != 0:
        print(f"\n❌ ERROR: Command failed: {' '.join(cmd)}")
        sys.exit(1)

def main():
    is_local = "--local" in sys.argv
    if is_local:
        print("🚀 --- ANIME DOWNLOADER RELEASE AUTOMATION (LOCAL DRY-RUN) --- 🚀\n")
    else:
        print("🚀 --- ANIME DOWNLOADER RELEASE AUTOMATION --- 🚀\n")
    
    # 1. Read the current version from config.py
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    match = re.search(r'APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', content)
    if not match:
        print(f"❌ Could not find APP_VERSION in {CONFIG_FILE}!")
        sys.exit(1)

    major, minor, patch = map(int, match.groups())
    current_version = f"{major}.{minor}.{patch}"
    print(f"Current Version: {current_version}")
    
    if is_local:
        print("💡 Dry-run mode: Keeping version unchanged and skipping Git/GitHub uploads.")
        new_version = current_version
    else:
        CHOOSE = False
        while not CHOOSE:
            choice = input("Choose update type from: major, minor, or patch update? : \n").strip().lower() or 'p'

            if choice in ['major', 'm']:
                major += 1; minor = 0; patch = 0
                CHOOSE = True
            elif choice in ['minor', 'min']:
                minor += 1; patch = 0
                CHOOSE = True
            elif choice in ['patch', 'p']:
                patch += 1
                CHOOSE = True
            else:
                print("Invalid choice. Please choose again\n")

        new_version = f"{major}.{minor}.{patch}"
        print(f"\n✨ Upgrading to version: {new_version} ✨\n")

        # 3. Overwrite the version in config.py
        new_content = re.sub(
            r'APP_VERSION\s*=\s*"\d+\.\d+\.\d+"', 
            f'APP_VERSION = "{new_version}"', 
            content
        )
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)

    # 4. Compile the main application folder via PyInstaller (--onedir)
    print("📦 Compiling .exe with PyInstaller...")
    # Clean up previous build/dist folders to avoid caching issues
    for f_dir in ["build", "dist"]:
        if os.path.exists(f_dir):
            try: shutil.rmtree(f_dir)
            except: pass
            
    run_cmd(PYINSTALLER_CMD)
    
    exe_path = os.path.join("dist", EXE_NAME, f"{EXE_NAME}.exe")
    if not os.path.exists(exe_path):
        print("❌ Build failed! Could not find the .exe inside the dist folder.")
        sys.exit(1)

    # 4.5. Compile the Setup Installer via PyInstaller (--onefile)
    print("\n📦 Packaging application into standalone Installer Setup...")
    run_cmd(INSTALLER_CMD)
    
    setup_path = os.path.join("dist", f"{EXE_NAME}_Setup.exe")
    if not os.path.exists(setup_path):
        print("❌ Installer packaging failed!")
        sys.exit(1)

    # Zip the compiled folder for portable/zip downloaders
    zip_path = "AutoDownloader.zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)
    
    print("\n🤐 Zipping compiled folder for release...")
    try:
        # Simplify archiving paths directly to avoid OS-level directory change lockouts
        shutil.make_archive("AutoDownloader", "zip", os.path.join("dist", EXE_NAME))
    except Exception as e:
        print(f"❌ ERROR: Zipping failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
        
    if not os.path.exists(zip_path):
        print("❌ Zipping failed!")
        sys.exit(1)

    if is_local:
        print("\n🎉 LOCAL DRY-RUN COMPLETE!")
        print(f"Generated build folder: dist/{EXE_NAME}")
        print(f"Generated Installer package: {setup_path}")
        print(f"Generated Portable ZIP package: {zip_path}")
        print("Everything successfully built, packaged, and zipped locally! GitHub was NOT affected.")
        return

    # 5. Git Commit & Push (Adding ALL new folders!)
    print("\n🌐 Pushing code to GitHub...")
    run_cmd(["git", "add", "."]) 
    run_cmd(["git", "commit", "-m", f"🚀 Release version {new_version}"], check=False)
    try:
        run_cmd(["git", "push", "-u", "origin", "main"])
    except SystemExit:
        run_cmd(["git", "push"])

    # 6. Create GitHub Release & Upload BOTH the Setup installer and portable ZIP!
    print("\n☁️ Creating GitHub Release and uploading assets...")
    tag = f"v{new_version}"
    run_cmd(["gh", "release", "create", tag, setup_path, zip_path, "--title", f"Release {tag}", "--notes", f"Automated release for version {new_version}"])
    
    # Cleanup local zip file after uploading
    if os.path.exists(zip_path):
        os.remove(zip_path)
        
    print(f"\n✅ SUCCESS! Version {new_version} is now live and will auto-update for users!")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ A fatal error occurred during build/deploy: {e}")
        import traceback
        traceback.print_exc()
    input("\nPress Enter to close...")