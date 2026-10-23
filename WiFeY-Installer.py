import sys
import subprocess
import os
import time
import shutil
import importlib.util
import platform
import json
import socket
import webbrowser
import urllib.parse
import threading
import itertools

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

# --- ANIMATED LOADER ---
spinner_stop = False
def spinner_task(message):
    spinner = itertools.cycle(['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'])
    while not spinner_stop:
        sys.stdout.write(f'\r[*] {message} {next(spinner)}')
        sys.stdout.flush()
        time.sleep(0.1)
    sys.stdout.write('\r' + ' ' * (len(message) + 6) + '\r') # Clear line

def run_with_spinner(message, command):
    global spinner_stop
    spinner_stop = False
    thread = threading.Thread(target=spinner_task, args=(message,))
    thread.start()
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        spinner_stop = True
        thread.join()
        return True, result.stdout
    except subprocess.CalledProcessError as e:
        spinner_stop = True
        thread.join()
        return False, e.stderr

# --- LINUX SYSTEM DEPENDENCY BOOTSTRAPPER ---
def ensure_linux_system_deps():
    if platform.system() != "Linux":
        return
        
    if shutil.which("apt"):
        print("\n[*] Linux detected. Auto-installing required system UI and Venv libraries...")
        print("[*] (You may be prompted for your sudo password to install these packages)")
        
        # Silently update apt
        subprocess.run(["sudo", "apt-get", "update"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Install Venv, Pip, and core GTK requirements for PyWebview
        core_deps = ["python3-venv", "python3-pip", "python3-gi", "python3-gi-cairo", "gir1.2-gtk-3.0"]
        subprocess.run(["sudo", "apt-get", "install", "-y"] + core_deps)
        
        # WebKit versioning varies between older/newer Ubuntu (22.04+ uses 4.1, older uses 4.0)
        res = subprocess.run(["sudo", "apt-get", "install", "-y", "gir1.2-webkit2-4.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if res.returncode != 0:
            subprocess.run(["sudo", "apt-get", "install", "-y", "gir1.2-webkit2-4.0"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
    elif shutil.which("dnf"):
        print("\n[*] Fedora/RHEL Linux detected. Auto-installing required system UI libraries...")
        subprocess.run(["sudo", "dnf", "install", "-y", "python3-pip", "python3-gobject", "gtk3", "webkit2gtk3"])
        
    elif shutil.which("pacman"):
        print("\n[*] Arch Linux detected. Auto-installing required system UI libraries...")
        subprocess.run(["sudo", "pacman", "-S", "--noconfirm", "python-pip", "python-gobject", "gtk3", "webkit2gtk"])

# --- DEPENDENCY & VENV MANAGEMENT ---
def ensure_dependencies():
    packages = {
        "flask": "flask",
        "pyserial": "serial",
        "boto3": "boto3",
        "python-dotenv": "dotenv",
        "pyngrok": "pyngrok",
        "pywebview": "webview",
        "requests": "requests", 
        "pyinstaller": "PyInstaller"
    }

    base_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir = os.path.join(base_dir, "venv")
    
    # 1. Check if running inside virtual environment
    in_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)

    # 2. If not in venv, initialize environment and relaunch script BEFORE displaying the menu
    if not in_venv:
        ensure_linux_system_deps()
        
        python_executable = os.path.join(venv_dir, "bin" if os.name != "nt" else "Scripts", "python" + (".exe" if os.name == "nt" else ""))
        
        if not os.path.exists(python_executable):
            if os.path.exists(venv_dir):
                shutil.rmtree(venv_dir)
            print("\n[*] Initializing isolated Python environment (venv)...")
            success, err = run_with_spinner("Creating virtual environment", [sys.executable, "-m", "venv", "--system-site-packages", venv_dir])
            
            if not success:
                print(f"\n[ERROR] Failed to create virtual environment.\nError details: {err}")
                sys.exit(1)

        print("[*] Entering secure virtual environment...\n")
        script_path = os.path.abspath(sys.argv[0])
        result = subprocess.run([python_executable, script_path] + sys.argv[1:])
        sys.exit(result.returncode)

    # 3. Inside venv: Ensure pip is bootstrapped properly
    if importlib.util.find_spec("pip") is None:
        run_with_spinner("Bootstrapping pip", [sys.executable, "-m", "ensurepip", "--upgrade"])

    # 4. Install missing packages
    missing = [pkg for pkg, mod in packages.items() if not importlib.util.find_spec(mod)]
    if missing:
        success, err = run_with_spinner(
            f"Installing {len(missing)} packages ({', '.join(missing)})", 
            [sys.executable, "-m", "pip", "install", *missing, "--quiet"]
        )
        if not success:
            print(f"\n[ERROR] Failed installing packages: {err}")
            sys.exit(1)
        print("[+] All dependencies installed successfully!\n")
    return True

def ensure_config_files():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(base_dir, ".env")
    example_file = os.path.join(base_dir, ".env.example")
    
    if not os.path.exists(env_file) and os.path.exists(example_file):
        print("[*] Creating default '.env' file from '.env.example'...")
        shutil.copyfile(example_file, env_file)

def create_desktop_shortcut(exe_path, os_name):
    desktop_dir = os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        if os_name == "Windows":
            vbs_script = f"""
            Set oWS = WScript.CreateObject("WScript.Shell")
            sDesktop = oWS.SpecialFolders("Desktop")
            Set oLink = oWS.CreateShortcut(sDesktop & "\\WiFeY Studio.lnk")
            oLink.TargetPath = "{exe_path}"
            oLink.WorkingDirectory = "{os.path.dirname(exe_path)}"
            oLink.Description = "WiFeY Studio OTA Compiler"
            oLink.Save
            """
            vbs_path = os.path.join(os.environ.get('TEMP', os.getcwd()), 'create_shortcut.vbs')
            with open(vbs_path, 'w', encoding='utf-8') as f:
                f.write(vbs_script)
            subprocess.run(['cscript.exe', '//Nologo', vbs_path], capture_output=True)
            os.remove(vbs_path)
            print("[+] Windows shortcut added to Desktop.")
            
        elif os_name == "Darwin":
            app_bundle = exe_path + ".app"
            if os.path.exists(app_bundle):
                shortcut_path = os.path.join(desktop_dir, "WiFeY Studio.app")
                if os.path.exists(shortcut_path):
                    if os.path.isdir(shortcut_path) and not os.path.islink(shortcut_path):
                        shutil.rmtree(shortcut_path)
                    else:
                        os.remove(shortcut_path)
                os.symlink(app_bundle, shortcut_path)
                print("[+] macOS shortcut added to Desktop.")
                return app_bundle
                
        elif os_name == "Linux":
            desktop_file = os.path.join(desktop_dir, "WiFeY_Studio.desktop")
            content = f"""[Desktop Entry]
Name=WiFeY Studio
Comment=OTA Compiler
Exec="{exe_path}"
Terminal=false
Type=Application
Categories=Development;
"""
            with open(desktop_file, 'w') as f:
                f.write(content.strip())
            os.chmod(desktop_file, 0o755)
            print("[+] Linux shortcut added to Desktop.")
    except Exception as e:
        print(f"[!] Could not create shortcut: {e}")
    return exe_path

# --- INSTALLATION PIPELINE ---
def run_build_pipeline():
    os_name = platform.system()
    print("==================================================")
    print(f">> Compiling WiFeY Studio ({os_name})")
    print("==================================================\n")

    ensure_config_files()

    build_command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", "WiFeY_Studio",
        "--collect-data", "botocore",
        "--copy-metadata", "botocore",
        "--hidden-import", "serial.tools.list_ports",
        "app.py"
    ]

    success, err = run_with_spinner("Running PyInstaller build (this may take a few minutes)", build_command)
    
    if success:
        print("[+] Compilation successful! Standalone executable generated in 'dist/' folder.")
    else:
        print(f"\n[ERROR] Compilation failed. Error details:\n{err}")
        return

    base_exe = "WiFeY_Studio.exe" if os_name == "Windows" else "WiFeY_Studio"
    exe_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", base_exe)
    launch_target = exe_path

    if os.path.exists(exe_path) or (os_name == "Darwin" and os.path.exists(exe_path + ".app")):
        launch_target = create_desktop_shortcut(exe_path, os_name)
        print(f"\n[*] Launching {os.path.basename(launch_target)}...")
        if os_name == "Darwin":
            subprocess.Popen(["open", launch_target])
        else:
            subprocess.Popen([launch_target])
        print("[+] Success! WiFeY Studio is running natively.\n")
    else:
        print(f"[ERROR] Compiled executable not found at {exe_path}")

# --- UNINSTALLATION / AUTHENTICATION PIPELINE ---
def verify_uninstall_auth():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(base_dir, "wifey_db.json")
    
    if not os.path.exists(db_path):
        return True

    try:
        with open(db_path, 'r', encoding='utf-8') as f:
            db_data = json.load(f)
    except:
        return True
        
    prof = db_data.get("user_profile", {})
    expected_sub = prof.get("google_sub")
    expected_email = prof.get("email")

    if not expected_sub:
        return True

    print(f"\n[SECURITY] Protected Uninstallation Enabled.")
    print(f"[*] This WiFeY Studio instance is currently linked to: {expected_email}")
    print("[*] You must authorize the uninstallation using this Google account.")
    input("\nPress ENTER to open browser and authenticate...")

    import requests

    # Check .env for configured credentials, with fallback defaults
    env_path = os.path.join(base_dir, ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as ef:
            for eline in ef:
                eline = eline.strip()
                if eline and not eline.startswith("#") and "=" in eline:
                    ek, ev = eline.split("=", 1)
                    env_vars[ek.strip()] = ev.strip()

    CLIENT_ID = env_vars.get("GOOGLE_CLIENT_ID") or "778920138283-clgb04rusodrhefvmb0lnmlnkjhn06pa.apps.googleusercontent.com"
    CLIENT_SECRET = env_vars.get("GOOGLE_CLIENT_SECRET") or "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"
    REDIRECT_URI = "http://127.0.0.1:5050/api/auth/callback"

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account"
    }
    
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"
    webbrowser.open(auth_url)

    print("\n[*] Waiting for Google authentication response...")
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 5050))
        server.listen(1)
        client, _ = server.accept()
        request = client.recv(2048).decode('utf-8')
        
        code = None
        for line in request.split('\n'):
            if line.startswith('GET'):
                if 'code=' in line:
                    code = line.split('code=')[1].split('&')[0].split(' ')[0]
                break

        response_html = "HTTP/1.0 200 OK\n\n<html><body style='background:#121316; color:#00b4b6; font-family:sans-serif; text-align:center; padding-top:50px;'><h2>Verification Received!</h2><p>You can close this tab and return to the terminal.</p></body></html>"
        client.sendall(response_html.encode('utf-8'))
        client.close()
        server.close()

        if not code:
            print("[ERROR] Authorization code missing. Uninstallation aborted.")
            return False

        token_resp = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code"
        }).json()
        
        access_token = token_resp.get("access_token")
        if not access_token:
            print("[ERROR] Failed to obtain access token from Google.")
            return False
            
        user_info = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"}
        ).json()
        
        authorized_sub = user_info.get("sub")
        authorized_email = user_info.get("email")

        if authorized_sub == expected_sub:
            print(f"\n[SUCCESS] Authorized by {authorized_email}. Proceeding with uninstallation.")
            return True
        else:
            print(f"\n[DENIED] Authentication failed. Logged in as {authorized_email}, but {expected_email} is required.")
            return False

    except Exception as e:
        print(f"\n[ERROR] Authentication process failed: {e}")
        try:
            server.close()
        except:
            pass
        return False

def uninstall_wifey():
    print("\n==================================================")
    print(">> Uninstalling WiFeY Studio")
    print("==================================================")

    # Require explicit user confirmation
    confirm = input("\n[WARNING] This will completely wipe all databases, configurations, and caches.\nType 'DELETE' to confirm uninstallation: ").strip()
    if confirm != "DELETE":
        print("\n[!] Uninstallation cancelled by user.")
        return

    if not verify_uninstall_auth():
        print("[!] Uninstallation aborted due to authorization failure.")
        return

    print("\n[*] Commencing complete data wipe...")
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os_name = platform.system()

    files_to_remove = ["wifey_db.json", "wifey_db.json.tmp", "wifey_prefs.json", "wifey_prefs.json.tmp", ".env", "WiFeY_Studio.spec"]
    folders_to_remove = ["build", "dist", "build_cache", "core_cache", "sketch_source", "__pycache__", ".wrangler", "cloud_vault", "webview_data", "venv"]

    for f in files_to_remove:
        path = os.path.join(base_dir, f)
        if os.path.exists(path):
            os.remove(path)
            print(f"[-] Deleted: {f}")

    for d in folders_to_remove:
        path = os.path.join(base_dir, d)
        if os.path.exists(path):
            if d == "venv" and os.name == "nt":
                # Schedule active venv folder deletion 2 seconds after Python exits
                subprocess.Popen(f'cmd /c timeout /t 2 /nobreak > NUL & rmdir /s /q "{path}"', shell=True)
                print(f"[-] Scheduled cache folder for deletion upon exit: {d}")
            else:
                shutil.rmtree(path)
                print(f"[-] Deleted cache folder: {d}")

    if os_name == "Windows":
        appdata_path = os.path.join(os.environ.get('APPDATA', ''), 'WiFeY_Studio')
        desktop_lnk = os.path.join(os.path.expanduser("~"), "Desktop", "WiFeY Studio.lnk")
        programs_lnk = os.path.join(os.environ.get('APPDATA', ''), "Microsoft", "Windows", "Start Menu", "Programs", "WiFeY Studio.lnk")
        
        if os.path.exists(appdata_path): shutil.rmtree(appdata_path)
        if os.path.exists(desktop_lnk): os.remove(desktop_lnk)
        if os.path.exists(programs_lnk): os.remove(programs_lnk)
        
    elif os_name == "Darwin":
        desktop_app = os.path.join(os.path.expanduser("~"), "Desktop", "WiFeY Studio.app")
        if os.path.exists(desktop_app):
            if os.path.islink(desktop_app): os.remove(desktop_app)
            else: shutil.rmtree(desktop_app)

    elif os_name == "Linux":
        desktop_lnk = os.path.join(os.path.expanduser("~"), "Desktop", "WiFeY_Studio.desktop")
        if os.path.exists(desktop_lnk): os.remove(desktop_lnk)

    print("\n[+] Uninstallation Complete. All configurations, databases, and caches have been removed.")

# --- MAIN MENU ---
if __name__ == "__main__":
    check_python_version()
    
    # Bootstrap environment & packages BEFORE presenting options to the user
    ensure_dependencies()
    
    print("==================================================")
    print("      WiFeY Studio Setup & Manager Utility        ")
    print("==================================================")
    print("1. Install / Compile WiFeY Studio (Creates Desktop App)")
    print("2. Uninstall WiFeY Studio (Secure Wipe)")
    print("3. Exit")
    
    choice = input("\nSelect an option [1-3]: ").strip()
    
    if choice == '1':
        run_build_pipeline()
    elif choice == '2':
        uninstall_wifey()
    else:
        print("Exiting...")
        sys.exit(0)