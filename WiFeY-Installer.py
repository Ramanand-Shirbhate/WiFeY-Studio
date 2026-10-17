import sys
import subprocess
import os
import time
import shutil
import importlib.util

# Ensure UTF-8 output encoding across Windows consoles
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except:
        pass

def check_python_version():
    if sys.version_info < (3, 10):
        print("[ERROR] Python 3.10 or higher is required.")
        print(f"Current version: {sys.version}")
        sys.exit(1)

def ensure_dependencies():
    packages = {
        "flask": "flask",
        "pyserial": "serial",
        "boto3": "boto3",
        "python-dotenv": "dotenv",
        "pyngrok": "pyngrok",
        "pywebview": "webview",
        "pyinstaller": "PyInstaller"
    }

    print("==================================================")
    print(">> STEP 1: Verifying Python Dependencies")
    print("==================================================\n")

    missing = [pkg for pkg, mod in packages.items() if not importlib.util.find_spec(mod)]

    if not missing:
        print("[+] All required Python packages are already installed!\n")
        return True

    print(f"[*] Missing packages detected: {', '.join(missing)}")
    print("[*] Installing missing dependencies via pip...")

    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing, "--quiet"])
        print("[+] All dependencies installed successfully!\n")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed installing packages: {e}")
        return False

def ensure_config_files():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(base_dir, ".env")
    example_file = os.path.join(base_dir, ".env.example")
    
    if not os.path.exists(env_file) and os.path.exists(example_file):
        print("[*] Creating default '.env' file from '.env.example'...")
        shutil.copyfile(example_file, env_file)
        print("[+] Created '.env'. Be sure to add your Cloudflare R2 & Ngrok credentials!\n")

def run_build_pipeline():
    check_python_version()
    
    if not ensure_dependencies():
        return

    ensure_config_files()

    print("==================================================")
    print(">> STEP 2: Compiling WiFeY Studio into Standalone EXE")
    print("==================================================\n")

    build_command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", "WiFeY_Studio",
        "app.py"
    ]

    print("[*] Running PyInstaller build...")
    try:
        subprocess.run(build_command, check=True)
        print("\n[+] Compilation successful! Standalone executable generated in 'dist/' folder.\n")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Compilation failed. Error: {e}")
        return

    print("==================================================")
    print(">> STEP 3: Launching WiFeY Studio Desktop")
    print("==================================================\n")

    exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "WiFeY_Studio.exe")

    if os.path.exists(exe_path):
        print(f"[*] Launching {exe_path}...")
        subprocess.Popen([exe_path])
        print("[+] Success! WiFeY Studio is running natively.")
        time.sleep(2)
    else:
        print("[ERROR] Compiled executable not found in 'dist/' directory.")

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    app_py_path = os.path.join(base_dir, "app.py")
    if not os.path.exists(app_py_path):
        print("[ERROR] 'app.py' not found! Please place this script in the same directory as app.py.")
    else:
        run_build_pipeline()