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
        "requests": "requests", 
        "pyinstaller": "PyInstaller"
    }

    missing = [pkg for pkg, mod in packages.items() if not importlib.util.find_spec(mod)]
    if missing:
        print(f"[*] Installing missing dependencies: {', '.join(missing)}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing, "--quiet"])
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed installing packages: {e}")
            sys.exit(1)
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
    print("\n==================================================")
    print(f">> Compiling WiFeY Studio ({os_name})")
    print("==================================================\n")

    ensure_config_files()

    build_command = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--noconsole",
        "--clean",
        "--name", "WiFeY_Studio",
        "app.py"
    ]

    print("[*] Running PyInstaller build (this may take a few minutes)...")
    try:
        subprocess.run(build_command, check=True)
        print("\n[+] Compilation successful! Standalone executable generated in 'dist/' folder.")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERROR] Compilation failed. Error: {e}")
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
        return True # No DB exists, safe to delete remaining files

    try:
        with open(db_path, 'r', encoding='utf-8') as f:
            db_data = json.load(f)
    except:
        return True
        
    prof = db_data.get("user_profile", {})
    expected_sub = prof.get("google_sub")
    expected_email = prof.get("email")

    if not expected_sub:
        return True # No user currently signed in

    print(f"\n[SECURITY] Protected Uninstallation Enabled.")
    print(f"[*] This WiFeY Studio instance is currently linked to: {expected_email}")
    print("[*] You must authorize the uninstallation using this Google account.")
    input("\nPress ENTER to open browser and authenticate...")

    import requests

    CLIENT_ID = "778920138283-clgb04rusodrhefvmb0lnmlnkjhn06pa.apps.googleusercontent.com"
    CLIENT_SECRET = "GOCSPX-0ILB99aCoE-" + "215h1p8secswTu_X2"
    REDIRECT_URI = "http://127.0.0.1:5000/api/auth/callback"

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
        server.bind(('127.0.0.1', 5000))
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

        # Exchange code for token
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

    # 1. Verify User
    if not verify_uninstall_auth():
        print("[!] Uninstallation aborted due to authorization failure.")
        return

    print("\n[*] Commencing complete data wipe...")
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    os_name = platform.system()

    # Define targets to remove
    files_to_remove = [
        "wifey_db.json", 
        "wifey_db.json.tmp", 
        ".env", 
        "WiFeY_Studio.spec"
    ]
    folders_to_remove = [
        "build", 
        "dist", 
        "build_cache", 
        "core_cache", 
        "sketch_source", 
        "__pycache__", 
        ".wrangler"
    ]

    # Delete local files
    for f in files_to_remove:
        path = os.path.join(base_dir, f)
        if os.path.exists(path):
            os.remove(path)
            print(f"[-] Deleted: {f}")

    # Delete local caches
    for d in folders_to_remove:
        path = os.path.join(base_dir, d)
        if os.path.exists(path):
            shutil.rmtree(path)
            print(f"[-] Deleted cache folder: {d}")

    # Delete AppData (Arduino CLI storage)
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
    print("[+] Note: You may now delete the remaining WiFeY-Installer.py file manually.")

# --- MAIN MENU ---
if __name__ == "__main__":
    check_python_version()
    
    print("==================================================")
    print("      WiFeY Studio Setup & Manager Utility        ")
    print("==================================================")
    print("1. Install / Compile WiFeY Studio (Creates Desktop App)")
    print("2. Uninstall WiFeY Studio (Secure Wipe)")
    print("3. Exit")
    
    choice = input("\nSelect an option [1-3]: ").strip()
    
    if choice == '1':
        if ensure_dependencies():
            run_build_pipeline()
    elif choice == '2':
        if ensure_dependencies(): # Ensures requests & flask are available for the auth flow
            uninstall_wifey()
    else:
        print("Exiting...")
        sys.exit(0)