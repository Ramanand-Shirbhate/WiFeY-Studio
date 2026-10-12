import sys
import subprocess
import os
import time

def run_build_pipeline():
    # Step 1: List all dependencies, making sure PyInstaller is included
    required_packages = [
        "flask",
        "pyserial",
        "boto3",
        "python-dotenv",
        "pyngrok",
        "pywebview",
        "pyinstaller"
    ]

    print("==================================================")
    print("🚀 STEP 1: Installing Required Libraries")
    print("==================================================\n")
    
    for package in required_packages:
        try:
            print(f"[*] Installing {package}...")
            # sys.executable ensures pip installs to the correct Python environment
            subprocess.check_call([sys.executable, "-m", "pip", "install", package, "--quiet"])
        except subprocess.CalledProcessError:
            print(f"[❌] Failed to install {package}. Exiting.")
            return

    print("\n[✓] All libraries installed successfully!\n")
    
    print("==================================================")
    print("🚀 STEP 2: Compiling app.py into an EXE")
    print("==================================================\n")
    
    # We use 'sys.executable -m PyInstaller' to avoid Windows PATH errors
    build_command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", "WiFeY_Studio",
        "app.py"
    ]
    
    try:
        # Run PyInstaller and wait for it to finish
        subprocess.run(build_command, check=True)
        print("\n[✓] Compilation successful! The .exe is in the 'dist' folder.\n")
    except subprocess.CalledProcessError as e:
        print(f"\n[❌] Compilation failed. Error: {e}")
        return

    print("==================================================")
    print("🚀 STEP 3: Launching WiFeY_Studio.exe")
    print("==================================================\n")
    
    # Locate the newly built .exe file
    exe_path = os.path.join(os.getcwd(), "dist", "WiFeY_Studio.exe")
    
    if os.path.exists(exe_path):
        print(f"[*] Starting {exe_path}...")
        
        # Use Popen instead of run() so this builder script can close itself
        # while leaving the WiFeY Studio UI running in the background.
        subprocess.Popen([exe_path])
        
        print("🎉 Done! WiFeY Studio is now running natively.")
        time.sleep(2) # Give the user a moment to read the success message
    else:
        print("[❌] Error: Could not find the compiled EXE. Something went wrong during the build.")

if __name__ == "__main__":
    if not os.path.exists("app.py"):
        print("[❌] Error: 'app.py' not found! Please place this script in the same folder as app.py.")
    else:
        run_build_pipeline()