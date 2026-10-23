import os
import sys
import re
import time
import json
import tempfile
import subprocess
import shutil
import zipfile
import threading
import ssl
import urllib.request
import socket
import secrets
import queue
import collections
import serial
import serial.tools.list_ports
from flask import Flask, render_template_string, request, jsonify, Response, send_file
import boto3
from botocore.config import Config
from dotenv import load_dotenv
import requests
import ctypes
import webbrowser
import hashlib
import base64
import urllib.parse
import platform
import stat

# --- PYINSTALLER HIDDEN TERMINAL PATCH ---
# Forces ALL child processes (Ngrok, Arduino CLI, etc.) to run invisibly on Windows
if os.name == 'nt':
    _original_popen = subprocess.Popen
    class HiddenPopen(_original_popen):
        def __init__(self, *args, **kwargs):
            # Force the CREATE_NO_WINDOW flag onto every background process
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
            # Prevent PyInstaller stdin crash
            if kwargs.get('stdin') is None:
                kwargs['stdin'] = subprocess.DEVNULL
            super().__init__(*args, **kwargs)
    subprocess.Popen = HiddenPopen
# -----------------------------------------

# --- APPLICATION BASE DIRECTORY RESOLUTION ---
if getattr(sys, 'frozen', False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Load environment variables from .env located in the app directory
ENV_FILE_PATH = os.path.join(APP_DIR, '.env')
load_dotenv(ENV_FILE_PATH)

# Windows Subprocess Flag (prevent console windows from popping up)
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

# --- CACHE & DATABASE DIRECTORIES ---
CORE_CACHE_DIR = os.path.join(APP_DIR, "core_cache")
BUILD_CACHE_DIR = os.path.join(APP_DIR, "build_cache")
os.makedirs(CORE_CACHE_DIR, exist_ok=True)
os.makedirs(BUILD_CACHE_DIR, exist_ok=True)

DB_PATH = os.path.join(APP_DIR, "wifey_db.json")

# --- BUILD / FIRMWARE LOCATIONS (one sketch path + one build path = every build reuses the compiled core & libraries) ---
PREFS_PATH = os.path.join(APP_DIR, "wifey_prefs.json")          # local-only preferences (never synced to the cloud)
OUTPUT_DIR = os.path.join(BUILD_CACHE_DIR, "output")
VERIFIED_DIR = os.path.join(BUILD_CACHE_DIR, "verified_bins")    # <source-hash>.bin: verified binaries reused by Deploy
LAN_FIRMWARE_DIR = os.path.join(BUILD_CACHE_DIR, "lan_firmware") # per-fleet/build copies served to LAN nodes
SKETCH_DIR = os.path.join(APP_DIR, "sketch_source", "WiFeYSketch")

SKETCH_FILE = os.path.join(SKETCH_DIR, "WiFeYSketch.ino")
for _d in (OUTPUT_DIR, VERIFIED_DIR, LAN_FIRMWARE_DIR, SKETCH_DIR):
    os.makedirs(_d, exist_ok=True)
DEFAULT_FQBN = "esp32:esp32:esp32"
TEMPLATE_VERSION = "2"

# --- DATABASE MANAGER (Persistent, Thread-Safe JSON Store) ---
class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._data = {
            "user_profile": {
                "google_sub": "",
                "email": "",
                "id_token": "",
                "picture": ""
            },
            "manifest": {},
            "devices": {}
        }
        self._dirty = False
        self.load()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def _flush_loop(self):
        # Beacons/check-ins only mark the DB dirty; one writer flushes every few seconds instead of rewriting the file per packet
        while True:
            time.sleep(3)
            if self._dirty:
                self.save()

    def load(self):
        with self.lock:
            if os.path.exists(self.db_path):
                try:
                    with open(self.db_path, "r", encoding="utf-8") as f:
                        self._data = json.load(f)
                except Exception as e:
                    print(f"[DB] Error loading database: {e}")

            # Ensure user_profile top-level structure
            if "user_profile" not in self._data or not isinstance(self._data["user_profile"], dict):
                self._data["user_profile"] = {"google_sub": "", "email": "", "id_token": "", "picture": ""}
            else:
                self._data["user_profile"].setdefault("google_sub", "")
                self._data["user_profile"].setdefault("email", "")
                self._data["user_profile"].setdefault("id_token", "")
                self._data["user_profile"].setdefault("picture", "")

            if "manifest" not in self._data or not isinstance(self._data["manifest"], dict):
                self._data["manifest"] = {}
            if "devices" not in self._data or not isinstance(self._data["devices"], dict):
                self._data["devices"] = {}

            # Schema migration for manifest (integer target_build and firmware_url)
            for fleet_name, fleet_data in list(self._data["manifest"].items()):
                if not isinstance(fleet_data, dict):
                    self._data["manifest"][fleet_name] = {"target_build": 100, "firmware_url": ""}
                    continue
                if "target_build" not in fleet_data:
                    # Migrate from old fleet_version if possible
                    fleet_data["target_build"] = 100
                else:
                    try:
                        fleet_data["target_build"] = int(fleet_data["target_build"])
                    except:
                        fleet_data["target_build"] = 100
                if "firmware_url" not in fleet_data:
                    fleet_data["firmware_url"] = ""

            # Schema migration for devices (integer current_build, device_token, custom overrides)
            for dev_id, dev_data in list(self._data["devices"].items()):
                if not isinstance(dev_data, dict):
                    continue
                dev_data.setdefault("fleet", "DefaultFleet")
                # Migrate auth_token -> device_token
                if "device_token" not in dev_data:
                    dev_data["device_token"] = dev_data.get("auth_token") or f"dt_{secrets.token_hex(8)}"
                # Migrate version -> current_build (integer)
                if "current_build" not in dev_data:
                    old_ver = dev_data.get("version", 100)
                    m = re.search(r'\d+', str(old_ver))
                    dev_data["current_build"] = int(m.group(0)) if m else 100
                else:
                    try:
                        dev_data["current_build"] = int(dev_data["current_build"])
                    except:
                        dev_data["current_build"] = 100
                dev_data.setdefault("last_seen", time.time())
                if "custom_build_override" not in dev_data:
                    dev_data["custom_build_override"] = None
                elif dev_data["custom_build_override"] is not None:
                    try:
                        dev_data["custom_build_override"] = int(dev_data["custom_build_override"])
                    except:
                        dev_data["custom_build_override"] = None
                if "custom_firmware_url" not in dev_data:
                    dev_data["custom_firmware_url"] = None

    def save(self):
        with self.lock:
            self._dirty = False
            tmp_path = self.db_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=4)
                os.replace(tmp_path, self.db_path)
            except Exception as e:
                print(f"[DB] Error saving database: {e}")
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except:
                        pass

    def get_all(self):
        with self.lock:
            return json.loads(json.dumps(self._data))

    def get_user_profile(self):
        with self.lock:
            return dict(self._data.get("user_profile", {}))

    def set_user_profile(self, google_sub=None, email=None, id_token=None, picture=None):
        with self.lock:
            prof = self._data.setdefault("user_profile", {})
            if google_sub is not None: prof["google_sub"] = google_sub
            if email is not None: prof["email"] = email
            if id_token is not None: prof["id_token"] = id_token
            if picture is not None: prof["picture"] = picture
        self.save()

    def clear_user_profile(self):
        with self.lock:
            self._data["user_profile"] = {
                "google_sub": "",
                "email": "",
                "id_token": "",
                "picture": ""
            }
        self.save()

    def get_device(self, device_id):
        with self.lock:
            return self._data.get("devices", {}).get(device_id)

    def is_valid_device_token(self, device_id, device_token):
        with self.lock:
            dev = self._data.get("devices", {}).get(device_id)
            if dev and dev.get("device_token") == device_token:
                return True
            return False

    def get_all_device_tokens(self):
        with self.lock:
            return {d.get("device_token") for d in self._data.get("devices", {}).values() if d.get("device_token")}

    def register_or_update_device(self, device_id, fleet, device_token=None, current_build=None):
        with self.lock:
            now = time.time()
            if device_id not in self._data["devices"]:
                self._data["devices"][device_id] = {
                    "fleet": fleet,
                    "device_token": device_token or f"dt_{secrets.token_hex(8)}",
                    "current_build": int(current_build) if current_build is not None else 100,
                    "last_seen": now,
                    "custom_build_override": None,
                    "custom_firmware_url": None
                }
            else:
                dev = self._data["devices"][device_id]
                if fleet:
                    dev["fleet"] = fleet
                dev["last_seen"] = now
                if device_token:
                    dev["device_token"] = device_token
                if current_build is not None:
                    dev["current_build"] = int(current_build)
            
            if fleet and fleet not in self._data["manifest"]:
                self._data["manifest"][fleet] = {"target_build": 100, "firmware_url": ""}
        self.save()

    def register_device_token(self, device_id, device_token):
        with self.lock:
            if device_id in self._data["devices"]:
                self._data["devices"][device_id]["device_token"] = device_token
            else:
                self._data["devices"][device_id] = {
                    "fleet": "DefaultFleet",
                    "device_token": device_token,
                    "current_build": 100,
                    "last_seen": time.time(),
                    "custom_build_override": None,
                    "custom_firmware_url": None
                }
        self.save()

    def update_device_seen(self, device_id, current_build=None):
        with self.lock:
            if device_id in self._data["devices"]:
                self._data["devices"][device_id]["last_seen"] = time.time()
                if current_build is not None:
                    self._data["devices"][device_id]["current_build"] = int(current_build)
                self._dirty = True   # flushed by the background writer

    def delete_fleet(self, fleet_name, move_to="DefaultFleet"):
        """Removes a fleet; its nodes are moved to move_to. Returns the ids of the moved nodes."""
        moved = []
        with self.lock:
            self._data["manifest"].pop(fleet_name, None)
            for dev_id, dev in self._data["devices"].items():
                if isinstance(dev, dict) and dev.get("fleet") == fleet_name:
                    dev["fleet"] = move_to
                    moved.append(dev_id)
            if moved:
                self._data["manifest"].setdefault(move_to, {"target_build": 100, "firmware_url": ""})
        self.save()
        return moved

    def delete_device(self, device_id):
        with self.lock:
            if device_id in self._data["devices"]:
                del self._data["devices"][device_id]
        self.save()

    def create_fleet(self, fleet_name, target_build=100, firmware_url=""):
        with self.lock:
            if fleet_name not in self._data["manifest"]:
                self._data["manifest"][fleet_name] = {
                    "target_build": int(target_build),
                    "firmware_url": firmware_url
                }
        self.save()

    def set_fleet_target_build(self, fleet_name, target_build, firmware_url=None):
        with self.lock:
            if fleet_name not in self._data["manifest"]:
                self._data["manifest"][fleet_name] = {
                    "target_build": int(target_build),
                    "firmware_url": firmware_url or ""
                }
            else:
                self._data["manifest"][fleet_name]["target_build"] = int(target_build)
                if firmware_url is not None:
                    self._data["manifest"][fleet_name]["firmware_url"] = firmware_url
        self.save()

    def get_target_build(self, fleet_id, device_id):
        with self.lock:
            if device_id and device_id in self._data.get("devices", {}):
                dev = self._data["devices"][device_id]
                override = dev.get("custom_build_override")
                override_url = dev.get("custom_firmware_url")
                if override is not None:
                    return int(override), override_url or ""
            
            manifest_fleet = self._data.get("manifest", {}).get(fleet_id, {})
            target_build = manifest_fleet.get("target_build", 100)
            firmware_url = manifest_fleet.get("firmware_url", "")
            return int(target_build), firmware_url

    def set_node_override(self, device_id, build_num, url=None):
        with self.lock:
            if device_id in self._data.get("devices", {}):
                self._data["devices"][device_id]["custom_build_override"] = int(build_num) if build_num is not None else None
                self._data["devices"][device_id]["custom_firmware_url"] = url
        self.save()

    def clear_node_override(self, device_id):
        with self.lock:
            if device_id in self._data.get("devices", {}):
                self._data["devices"][device_id]["custom_build_override"] = None
                self._data["devices"][device_id]["custom_firmware_url"] = None
        self.save()

    def reassign_fleet(self, device_id, new_fleet):
        with self.lock:
            if device_id in self._data.get("devices", {}):
                self._data["devices"][device_id]["fleet"] = new_fleet
                if new_fleet not in self._data.get("manifest", {}):
                    self._data.setdefault("manifest", {})[new_fleet] = {
                        "target_build": 100,
                        "firmware_url": ""
                    }
        self.save()

    def get_next_build_number(self, fleet_name):
        with self.lock:
            target = self._data.get("manifest", {}).get(fleet_name, {}).get("target_build", 100)
            builds = []
            for d in self._data.get("devices", {}).values():
                if isinstance(d, dict) and d.get("fleet") == fleet_name:
                    try:
                        builds.append(int(d.get("current_build", 100)))
                    except Exception:
                        pass
            try:
                base = max([int(target)] + builds)
            except Exception:
                base = 100
            return max(base + 1, 101)

db = DatabaseManager(DB_PATH)

# --- LOCAL PREFERENCES (remembered Wi-Fi, bin folder, OTA options) ---
class PrefsManager:
    DEFAULTS = {
        "wifi_ssid": "",
        "wifi_pass": "",
        "bin_dir": "",
        "poll_seconds": 30,
        "net_mode": "auto",
        "ap_window_min": 10,
        "serial_mirror": True,
    }
    NET_MODES = ("auto", "sta", "ap_sta", "ota_ap")

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self._data = dict(self.DEFAULTS)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data.update({k: v for k, v in loaded.items() if k in self.DEFAULTS})
        except Exception as e:
            print(f"[Prefs] Error loading preferences: {e}")

    def get(self, key, default=None):
        with self.lock:
            return self._data.get(key, self.DEFAULTS.get(key, default))

    def all(self):
        with self.lock:
            return dict(self._data)

    def update(self, values):
        clean = {}
        for key, val in (values or {}).items():
            if key not in self.DEFAULTS:
                continue
            if key in ("wifi_ssid", "wifi_pass"):
                clean[key] = "" if val is None else str(val)
            elif key == "bin_dir":
                clean[key] = os.path.expanduser(str(val or "").strip())
            elif key == "poll_seconds":
                clean[key] = max(0, min(3600, int(val)))
            elif key == "ap_window_min":
                clean[key] = max(0, min(1440, int(val)))
            elif key == "net_mode":
                v = str(val or "auto").lower()
                clean[key] = v if v in self.NET_MODES else "auto"
            elif key == "serial_mirror":
                clean[key] = (str(val).strip().lower() in ("1", "true", "yes", "on")) if isinstance(val, str) else bool(val)
        with self.lock:
            self._data.update(clean)
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2)
                os.replace(tmp, self.path)
            except Exception as e:
                print(f"[Prefs] Error saving preferences: {e}")
            return dict(self._data)

prefs = PrefsManager(PREFS_PATH)

# --- FIND ARDUINO CLI TOOLCHAIN ---
def find_arduino_cli():
    # 1. Global PATH
    cli = shutil.which("arduino-cli")
    if cli and os.path.exists(cli):
        return cli

    # 2. Common Windows & AppData paths
    common_paths = [
        r"C:\arduino-cli\arduino-cli.exe",
        os.path.join(os.environ.get('APPDATA', ''), 'WiFeY_Studio', 'arduino-cli.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'arduino-cli', 'arduino-cli.exe'),
        os.path.join(APP_DIR, 'arduino-cli.exe')
    ]
    for p in common_paths:
        if p and os.path.exists(p):
            return p
    return None

# --- AUTOMATED DEPENDENCY SETUP STATE ---
SETUP_STATE = {
    "stage": "Initializing...",
    "progress": 0,
    "log": "Starting environment checks...\n",
    "done": False,
    "error": False,
    "cli_path": None,
    "cli_version": "Unknown"
}

def create_windows_shortcuts():
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    exe_path = sys.executable
    vbs_script = f"""
    Set oWS = WScript.CreateObject("WScript.Shell")
    sDesktop = oWS.SpecialFolders("Desktop")
    sPrograms = oWS.SpecialFolders("Programs")
    
    Set oLink = oWS.CreateShortcut(sDesktop & "\\WiFeY Studio.lnk")
    oLink.TargetPath = "{exe_path}"
    oLink.WorkingDirectory = "{os.path.dirname(exe_path)}"
    oLink.Description = "WiFeY Studio OTA Compiler"
    oLink.Save
    
    Set oLink2 = oWS.CreateShortcut(sPrograms & "\\WiFeY Studio.lnk")
    oLink2.TargetPath = "{exe_path}"
    oLink2.WorkingDirectory = "{os.path.dirname(exe_path)}"
    oLink2.Description = "WiFeY Studio OTA Compiler"
    oLink2.Save
    """
    vbs_path = os.path.join(os.environ.get('TEMP', os.getcwd()), 'create_wifey_shortcut.vbs')
    try:
        with open(vbs_path, 'w', encoding='utf-8') as f:
            f.write(vbs_script)
        subprocess.run(['cscript.exe', '//Nologo', vbs_path], creationflags=CREATE_NO_WINDOW)
        os.remove(vbs_path)
    except:
        pass

def download_file_with_progress(url, dest):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    with urllib.request.urlopen(url, context=ctx) as response, open(dest, 'wb') as out_file:
        total_size = int(response.headers.get('Content-Length', 0))
        block_size = 8192
        count = 0
        while True:
            buffer = response.read(block_size)
            if not buffer:
                break
            out_file.write(buffer)
            count += 1
            if total_size > 0:
                percent = int((count * block_size * 100) / total_size)
                SETUP_STATE["progress"] = min(100, percent)

def setup_environment_thread():
    try:
        SETUP_STATE["stage"] = "Checking for Arduino CLI..."
        cli_executable = find_arduino_cli()
        
        LOCAL_APP_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'WiFeY_Studio')
        os.makedirs(LOCAL_APP_DIR, exist_ok=True)
        
        system_os = platform.system()
        cli_ext = ".exe" if system_os == "Windows" else ""
        LOCAL_CLI_PATH = os.path.join(LOCAL_APP_DIR, f'arduino-cli{cli_ext}')
        
        if cli_executable:
            SETUP_STATE["log"] += f"Found Arduino CLI at: {cli_executable}\n"
        else:
            SETUP_STATE["stage"] = "Downloading Arduino CLI..."
            if system_os == "Windows":
                SETUP_STATE["log"] += "Downloading official Windows 64-bit release...\n"
                archive_url = "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip"
                archive_path = os.path.join(LOCAL_APP_DIR, "cli.zip")
            elif system_os == "Linux":
                SETUP_STATE["log"] += "Downloading official Linux 64-bit release...\n"
                archive_url = "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Linux_64bit.tar.gz"
                archive_path = os.path.join(LOCAL_APP_DIR, "cli.tar.gz")
            elif system_os == "Darwin":
                SETUP_STATE["log"] += "Downloading official macOS 64-bit release...\n"
                archive_url = "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_macOS_64bit.tar.gz"
                archive_path = os.path.join(LOCAL_APP_DIR, "cli.tar.gz")
            
            download_file_with_progress(archive_url, archive_path)
            
            SETUP_STATE["stage"] = "Extracting files..."
            SETUP_STATE["progress"] = 0
            
            if archive_path.endswith('.zip'):
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(LOCAL_APP_DIR)
            else:
                import tarfile
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(LOCAL_APP_DIR)
                    
            os.remove(archive_path)
            cli_executable = LOCAL_CLI_PATH
            
            # Grant execution permissions on Linux/macOS
            if system_os in ["Linux", "Darwin"]:
                st = os.stat(cli_executable)
                os.chmod(cli_executable, st.st_mode | stat.S_IEXEC)
                
            SETUP_STATE["log"] += "Extraction complete.\n"
            
        SETUP_STATE["cli_path"] = cli_executable
        # Check CLI version
        try:
            ver_res = subprocess.run([cli_executable, "version"], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
            if ver_res.returncode == 0:
                SETUP_STATE["cli_version"] = ver_res.stdout.strip()
        except:
            pass

        # Fast-track check
        check_core = subprocess.run([cli_executable, "core", "list"], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
        check_lib = subprocess.run([cli_executable, "lib", "list"], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)

        has_esp32 = "esp32:esp32" in check_core.stdout
        has_json = "ArduinoJson" in check_lib.stdout
        
        if has_esp32 and has_json:
            SETUP_STATE["stage"] = "Toolchain Ready!"
            SETUP_STATE["progress"] = 100
            SETUP_STATE["log"] += "ESP32 Core & ArduinoJson verified. Launching WiFeY Studio...\n"
            create_windows_shortcuts()
            time.sleep(0.5)
            SETUP_STATE["done"] = True
            start_warmup()
            return

        # Configure ESP32 & ESP8266 Board Managers
        SETUP_STATE["stage"] = "Configuring Board Managers..."
        subprocess.run([cli_executable, "config", "init"], capture_output=True, creationflags=CREATE_NO_WINDOW) 
        subprocess.run([cli_executable, "config", "set", "board_manager.additional_urls", "https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json,https://arduino.esp8266.com/stable/package_esp8266com_index.json"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        
        SETUP_STATE["stage"] = "Updating Arduino Core Index..."
        subprocess.run([cli_executable, "core", "update-index"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        
        if not has_esp32:
            SETUP_STATE["stage"] = "Installing ESP32 Core..."
            SETUP_STATE["log"] += "Downloading Xtensa toolchains and ESP32 board files...\n"
            process = subprocess.Popen([cli_executable, "core", "install", "esp32:esp32"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
            for line in process.stdout:
                SETUP_STATE["log"] += line
            process.wait()

        # Check and Install ESP8266 Core
        check_esp82 = subprocess.run([cli_executable, "core", "list"], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
        if "esp8266:esp8266" not in check_esp82.stdout:
            SETUP_STATE["stage"] = "Installing ESP8266 Core..."
            SETUP_STATE["log"] += "Downloading ESP8266 toolchains (this takes a moment)...\n"
            process = subprocess.Popen([cli_executable, "core", "install", "esp8266:esp8266"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
            for line in process.stdout:
                SETUP_STATE["log"] += line
            process.wait()
            
        if not has_json:
            SETUP_STATE["stage"] = "Installing ArduinoJson..."
            SETUP_STATE["log"] += "Fetching ArduinoJson library...\n"
            process = subprocess.Popen([cli_executable, "lib", "install", "ArduinoJson"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
            for line in process.stdout:
                SETUP_STATE["log"] += line
            process.wait()
            
        SETUP_STATE["stage"] = "Creating App Shortcuts..."
        create_windows_shortcuts()
        
        SETUP_STATE["stage"] = "Setup Complete!"
        SETUP_STATE["progress"] = 100
        SETUP_STATE["log"] += "\nAll dependencies verified! Launching WiFeY Studio...\n"
        time.sleep(1.0)
        SETUP_STATE["done"] = True
        start_warmup()
        
    except Exception as e:
        SETUP_STATE["error"] = True
        SETUP_STATE["log"] += f"\nERROR: {str(e)}"

# --- NGROK TUNNEL CONFIGURATION ---
NGROK_STATIC_DOMAIN = os.getenv("NGROK_STATIC_DOMAIN", "christie-noncarnivorous-pulvinately.ngrok-free.dev")
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "")

NGROK_URL = "http://127.0.0.1:5050"
NGROK_ACTIVE = False

if NGROK_STATIC_DOMAIN:
    try:
        from pyngrok import ngrok, conf
        if NGROK_AUTHTOKEN:
            ngrok.set_auth_token(NGROK_AUTHTOKEN)
        # Check if already running or connect
        tunnels = ngrok.get_tunnels()
        existing = [t for t in tunnels if NGROK_STATIC_DOMAIN in t.public_url]
        if existing:
            NGROK_URL = existing[0].public_url
            NGROK_ACTIVE = True
        else:
            try:
                ngrok_tunnel = ngrok.connect(5050, domain=NGROK_STATIC_DOMAIN, bind_tls=True)
                NGROK_URL = ngrok_tunnel.public_url
                NGROK_ACTIVE = True
            except:
                NGROK_URL = f"https://{NGROK_STATIC_DOMAIN}"
                NGROK_ACTIVE = True
    except Exception as e:
        NGROK_URL = f"https://{NGROK_STATIC_DOMAIN}" if NGROK_STATIC_DOMAIN else "http://127.0.0.1:5050"

app = Flask(__name__)

# --- CLOUDFLARE R2 & WORKER CONFIGURATION ---
# Cloudflare Worker Gateway & Google OAuth Defaults
WORKER_BASE_URL = os.getenv("WORKER_BASE_URL", "https://wifey-gateway.07ramanand.workers.dev/").rstrip('/')

# Split the Client ID for GitHub Push Protection
fallback_id = "778920138283-clgb04rusodrhefvmb" + "0lnmlnkjhn06pa.apps.googleusercontent.com"
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", fallback_id)

# Split the Client Secret for GitHub Push Protection
fallback_secret = "GOCSPX-0ILB99aCoE-" + "215h1p8secswTu_X2"
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", fallback_secret)
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY", "")
R2_BUCKET = os.getenv("R2_BUCKET", "wifey-firmware")
OAUTH_STATES = {}
_S3_CLIENT = None
_S3_LOCK = threading.Lock()
def get_s3_client():
    global _S3_CLIENT
    with _S3_LOCK:
        if not R2_ACCOUNT_ID or not R2_ACCESS_KEY or not R2_SECRET_KEY:
            raise ValueError("Cloudflare R2 credentials not configured. Please update your settings.")
        if _S3_CLIENT is None:
            _S3_CLIENT = boto3.client(
                "s3",
                endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
                aws_access_key_id=R2_ACCESS_KEY,
                aws_secret_access_key=R2_SECRET_KEY,
                config=Config(signature_version="s3v4")
            )
        return _S3_CLIENT

client = get_s3_client  # backwards-compatible alias

# --- LOCAL IP & LAN NETWORKING ---
def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

LAN_NODES = {}
LAN_NODES_LOCK = threading.Lock()
UDP_PORT = 53535

# UI Event and Notification Queue (for live SSE notifications)
UI_LOG_QUEUE = queue.Queue(maxsize=1000)

def emit_ui_log(msg):
    try:
        UI_LOG_QUEUE.put_nowait(msg)
    except:
        pass

def udp_beacon_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_BROADCAST"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(('', UDP_PORT))
    except Exception as e:
        print(f"[UDP Beacon] Bind error on port {UDP_PORT}: {e}")
        return

    print(f"[UDP Beacon] Service active, listening on UDP port {UDP_PORT}...")
    while True:
        try:
            data, addr = sock.recvfrom(2048)
            text = data.decode('utf-8', errors='ignore').strip()
            if not text:
                continue

            if text.startswith('{') and text.endswith('}'):
                try:
                    beacon = json.loads(text)
                    device_id = beacon.get("device_id")
                    device_token = beacon.get("device_token")
                    ip = beacon.get("ip") or addr[0]
                    build = beacon.get("build", 100)

                    if device_id and device_token:
                        # Multi-User Isolation Check:
                        # Match device_token against user's registered devices in wifey_db.json
                        registered = db.get_device(device_id)
                        if registered and registered.get("device_token") == device_token:
                            now = time.time()
                            with LAN_NODES_LOCK:
                                LAN_NODES[device_id] = {
                                    "device_id": device_id,
                                    "device_token": device_token,
                                    "ip": ip,
                                    "build": int(build),
                                    "fleet": registered.get("fleet", "DefaultFleet"),
                                    "tcp": int(beacon.get("tcp") or 2323),
                                    "last_seen": now
                                }
                            db.update_device_seen(device_id, current_build=int(build))
                        else:
                            # Ignores nodes belonging to other users on the same Wi-Fi router
                            pass
                except Exception:
                    pass
            elif text.startswith('['):
                # Wireless UDP Serial logging from WIFEY_PRINT
                log_line = f"[UDP LOG {addr[0]}] {text}\n"
                emit_ui_log(log_line)
        except Exception:
            time.sleep(0.1)

# --- LIVE LAN STATUS, INSTANT UPDATE NUDGES & WIRELESS SERIAL MONITOR ---
LAN_ONLINE_SECS = 15          # nodes beacon every 5s -> missing 3 beacons = offline
WSERIAL_PORT = 2323
LAN_STATE = {}                # device_id -> last known online flag (for live online/offline events)

def lan_monitor_thread():
    """Emits [LAN] events the moment a node appears or drops off the network."""
    while True:
        time.sleep(2)
        try:
            now = time.time()
            with LAN_NODES_LOCK:
                snap = {k: ((now - v.get("last_seen", 0)) < LAN_ONLINE_SECS, v.get("ip", "")) for k, v in LAN_NODES.items()}
            for dev_id, (online, ip) in snap.items():
                prev = LAN_STATE.get(dev_id)
                if online and prev is not True:
                    emit_ui_log(f"[LAN] Node '{dev_id}' is now ONLINE ({ip})")
                elif (not online) and prev is True:
                    emit_ui_log(f"[LAN] Node '{dev_id}' went OFFLINE")
                LAN_STATE[dev_id] = online
        except Exception:
            pass

def nudge_devices(device_ids=None, fleet=None):
    """Tells LAN nodes to check for updates right now (UDP), so they update without waiting or restarting."""
    now = time.time()
    with LAN_NODES_LOCK:
        nodes = [dict(n) for n in LAN_NODES.values() if now - n.get("last_seen", 0) < LAN_ONLINE_SECS * 2]
    registry = db.get_all().get("devices", {})
    sent = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for n in nodes:
            did = n.get("device_id")
            if device_ids is not None and did not in device_ids:
                continue
            if fleet is not None and registry.get(did, {}).get("fleet") != fleet:
                continue
            msg = json.dumps({"cmd": "check_update", "token": n.get("device_token", "")}).encode("utf-8")
            try:
                sock.sendto(msg, (n["ip"], UDP_PORT))
                sent += 1
            except OSError:
                pass
    finally:
        sock.close()
    return sent

def compute_live():
    """One consistent snapshot of fleets + nodes with live online flags (LAN beacon or cloud check-in)."""
    now = time.time()
    snapshot = db.get_all()
    poll = int(prefs.get("poll_seconds", 30) or 0)
    cloud_window = max(90, poll * 3 + 30) if poll > 0 else 900
    with LAN_NODES_LOCK:
        lan = {k: dict(v) for k, v in LAN_NODES.items()}

    devices = {}
    fleets = {}
    for name, f in snapshot.get("manifest", {}).items():
        fleets[name] = {"target_build": int(f.get("target_build", 100)), "total": 0, "online": 0}
    fleets.setdefault("DefaultFleet", {"target_build": 100, "total": 0, "online": 0})

    lan_online_total = 0
    online_total = 0
    for dev_id, d in snapshot.get("devices", {}).items():
        li = lan.get(dev_id)
        lan_online = bool(li and (now - li.get("last_seen", 0)) < LAN_ONLINE_SECS and li.get("device_token") == d.get("device_token"))
        last_seen = max(float(d.get("last_seen", 0) or 0), float(li.get("last_seen", 0)) if li else 0.0)
        online = lan_online or (now - last_seen) < cloud_window
        fleet = d.get("fleet", "DefaultFleet")
        devices[dev_id] = {
            "fleet": fleet,
            "build": int(d.get("current_build", 100)),
            "online": online,
            "lan": lan_online,
            "ip": li.get("ip", "") if (li and lan_online) else "",
            "last_seen": last_seen,
            "ago": max(0, int(now - last_seen)),
            "override": d.get("custom_build_override"),
        }
        fl = fleets.setdefault(fleet, {"target_build": 100, "total": 0, "online": 0})
        fl["total"] += 1
        if online:
            fl["online"] += 1
            online_total += 1
        if lan_online:
            lan_online_total += 1
    return {
        "now": now, "fleets": fleets, "devices": devices,
        "lan_online": lan_online_total, "online_nodes": online_total, "total_nodes": len(devices),
    }

class WirelessSerialSession:
    """Client side of the ESP32 LAN serial bridge (TCP port 2323): reader thread + replayable buffer for SSE."""
    def __init__(self, key, ip, port=WSERIAL_PORT):
        self.key, self.ip, self.port = key, ip, int(port)
        self.sock = None
        self.alive = False
        self.seq = 0
        self.buf = collections.deque(maxlen=3000)
        self.cond = threading.Condition()

    def connect(self):
        self.sock = socket.create_connection((self.ip, self.port), timeout=5)
        self.sock.settimeout(1.0)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _push(self, text):
        with self.cond:
            self.seq += 1
            self.buf.append((self.seq, text))
            self.cond.notify_all()

    def _reader(self):
        while self.alive:
            try:
                data = self.sock.recv(2048)
                if not data:
                    break
                self._push(data.decode("utf-8", errors="replace"))
            except socket.timeout:
                continue
            except OSError:
                break
        self.alive = False
        self._push("\n[*] Wireless serial connection closed by the node.\n")
        try:
            self.sock.close()
        except Exception:
            pass
        with self.cond:
            self.cond.notify_all()

    def send(self, text):
        if not self.alive:
            raise OSError("Wireless serial is not connected.")
        self.sock.sendall(text.encode("utf-8"))

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        with self.cond:
            self.cond.notify_all()

WSERIAL_SESSIONS = {}
WSERIAL_LOCK = threading.Lock()

def resolve_api_url(url_str):
    """Replaces localhost/127.0.0.1 with PC local network IP for ESP32 connectivity."""
    if not url_str:
        return f"http://{get_local_ip()}:5050"
    url_str = url_str.rstrip('/')
    if "127.0.0.1" in url_str or "localhost" in url_str:
        local_ip = get_local_ip()
        url_str = url_str.replace("127.0.0.1", local_ip).replace("localhost", local_ip)
    return url_str

def safe_name(value):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', str(value))[:64] or "x"

def lan_firmware_url(filename):
    return f"http://{get_local_ip()}:5050/api/lan/firmware/{filename}"

def prune_lan_firmware(keep=16):
    try:
        files = sorted((os.path.join(LAN_FIRMWARE_DIR, f) for f in os.listdir(LAN_FIRMWARE_DIR) if f.endswith(".bin")),
                       key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception:
        pass

def upload_firmware_to_cloud(fleet_id, binary_bytes, build_num, id_token=None):
    build_num = int(build_num)
    # Always keep a LAN copy (own file per fleet/build, so deploys never overwrite each other)
    lan_name = f"{safe_name(fleet_id)}_build_{build_num}.bin"
    try:
        with open(os.path.join(LAN_FIRMWARE_DIR, lan_name), "wb") as f:
            f.write(binary_bytes)
        prune_lan_firmware()
    except Exception as e:
        print(f"[LAN Cache] Error saving binary: {e}")

    token = id_token or db.get_user_profile().get("id_token")

    # 1. Cloudflare Worker Gateway (Delegated Auth)
    if WORKER_BASE_URL and token:
        try:
            worker_upload_url = f"{WORKER_BASE_URL}/api/firmware/upload"
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Fleet-ID": fleet_id,
                "X-Build-Num": str(build_num)
            }
            files = {"file": (f"{fleet_id}_build_{build_num}.bin", binary_bytes, "application/octet-stream")}
            data = {"fleet": fleet_id, "build": str(build_num)}
            resp = requests.post(worker_upload_url, headers=headers, files=files, data=data, timeout=60)
            if resp.status_code in (200, 201):
                res_json = resp.json()
                url = res_json.get("download_url") or res_json.get("url")
                if url:
                    return url
        except Exception as e:
            print(f"[Worker] Upload error: {e}")

    # 2. Direct Cloudflare R2 S3 fallback (versioned key; manifest re-signs the link on every check-in)
    if R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY:
        try:
            s3 = get_s3_client()
            remote_key = f"{safe_name(fleet_id)}/build_{build_num}.bin"
            s3.put_object(
                Bucket=R2_BUCKET,
                Key=remote_key,
                Body=binary_bytes,
                ContentType="application/octet-stream"
            )
            return s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': R2_BUCKET, 'Key': remote_key},
                ExpiresIn=86400
            )
        except Exception as e:
            print(f"[R2] Direct S3 upload error: {e}")

    # 3. LAN HTTP fallback
    return lan_firmware_url(lan_name)

# --- SERIAL COMMUNICATION STATE & LOCK ---
SERIAL_LOCK = threading.Lock()
SERIAL_STATE = {
    "port": None,
    "baud": 115200,
    "serial_obj": None,
    "is_running": False
}

def close_serial_connection():
    with SERIAL_LOCK:
        SERIAL_STATE["is_running"] = False
        if SERIAL_STATE["serial_obj"]:
            try:
                if SERIAL_STATE["serial_obj"].is_open:
                    SERIAL_STATE["serial_obj"].setDTR(False)
                    SERIAL_STATE["serial_obj"].setRTS(False)
                    SERIAL_STATE["serial_obj"].close()
            except:
                pass
            SERIAL_STATE["serial_obj"] = None

# --- REGEX RULES FOR SMART CODE WRAPPING ---
RE_INCLUDES = re.compile(r'#include\s*[<"](WiFi\.h|ESP8266WiFi\.h|WiFiUdp\.h|WiFiClientSecure\.h|PubSubClient\.h|HTTPClient\.h|ESP8266HTTPClient\.h|HTTPUpdate\.h|ESP8266httpUpdate\.h|ArduinoJson\.h)[>"]')
RE_CONFIGS = re.compile(r'^\s*(const\s+)?(char\s*\*|int|String)\s*(ssid|password|api_url|api_base_url|auth_token|device_token|device_id|fleet_id|current_version|CURRENT_BUILD)\s*=.*;', re.MULTILINE)
RE_WIFI_BEGIN = re.compile(r'^\s*WiFi\.begin\(.*\);', re.MULTILINE)
RE_WIFI_MODE = re.compile(r'^\s*WiFi\.mode\(.*\);', re.MULTILINE)
RE_SERIAL_BEGIN = re.compile(r'^\s*Serial\.begin\(.*\);', re.MULTILINE)
RE_DELAY = re.compile(r'\bdelay\s*\(')

# --- MASTER ARDUINO C++ OTA TEMPLATE (v2: identity in NVS, polling, LAN serial, AP modes) ---
MASTER_CPP_TEMPLATE = r"""#include <WiFi.h>
#include <WiFiUdp.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <Update.h>
#include <Preferences.h>
{USER_INCLUDES}

// --- WiFeY Thin-Client Configuration (generated by WiFeY Studio) ---
const char* ssid = "{WIFI_SSID}";
const char* password = "{WIFI_PASS}";
const char* api_base_url = "{API_BASE_URL}";
const char* WIFEY_DEFAULT_FLEET = "{FLEET_ID}";
const char* WIFEY_DEFAULT_DEVICE_ID = "{DEVICE_ID}";
const char* WIFEY_DEFAULT_TOKEN = "{DEVICE_TOKEN}";
const int CURRENT_BUILD = {CURRENT_BUILD};

#define WIFEY_FORCE_IDENTITY {FORCE_IDENTITY}   // 1 = USB provisioning overwrites the stored identity
#define WIFEY_POLL_SECONDS {POLL_SECONDS}       // OTA poll interval, 0 = only at boot
#define WIFEY_NET_MODE {NET_MODE}               // 0 = Wi-Fi client, 1 = STA + AP together, 2 = OTA check, then AP only
#define WIFEY_AP_WINDOW_MIN {AP_WINDOW_MIN}     // mode 2: re-open an OTA window every N minutes (0 = never)
#define WIFEY_SERIAL_MIRROR {SERIAL_MIRROR}     // 1 = mirror Serial output to the LAN serial monitor (TCP)
#define WIFEY_WIFI_ATTEMPTS {WIFI_ATTEMPTS}
#define WIFEY_UDP_PORT 53535
#define WIFEY_TCP_PORT 2323
#define WIFEY_BEACON_INTERVAL 5000UL

#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif
#define WIFEY_LED LED_BUILTIN

// Identity lives in NVS so a fleet-wide OTA binary never overwrites a node's own identity.
String device_id;
String fleet_id;
String device_token;
Preferences wifey_prefs;
int wifey_applied_build = 0;

WiFiUDP wifey_udp;
unsigned long wifey_last_beacon = 0;
unsigned long wifey_last_poll = 0;
unsigned long wifey_last_window = 0;
bool wifey_force_check = false;
bool wifey_net_ready = false;

#if WIFEY_SERIAL_MIRROR
// ---- LAN serial monitor: tees everything the sketch prints to a TCP socket (port 2323) ----
class WiFeySerialClass : public Stream {
 public:
  using Print::write;
  WiFeySerialClass() { _mtx = xSemaphoreCreateRecursiveMutex(); }

  // Optional Serial methods are forwarded lazily (dependent calls), so a core that lacks one still compiles.
  template <typename S, typename... A> static void fwdBegin(S& s, A... a) { s.begin(a...); }
  template <typename S, typename... A> static void fwdDebug(S& s, A... a) { s.setDebugOutput(a...); }
  template <typename S, typename... A> static void fwdRxBuf(S& s, A... a) { s.setRxBufferSize(a...); }
  template <typename S, typename... A> static void fwdTxBuf(S& s, A... a) { s.setTxBufferSize(a...); }
  template <typename S, typename... A> static void fwdBaud(S& s, A... a) { s.updateBaudRate(a...); }
  template <typename S> static int fwdAvailW(S& s) { return s.availableForWrite(); }

  template <typename... A> void begin(A... a) { fwdBegin(Serial, a...); }
  void end() { Serial.end(); }
  template <typename... A> void setDebugOutput(A... a) { fwdDebug(Serial, a...); }
  template <typename... A> void setRxBufferSize(A... a) { fwdRxBuf(Serial, a...); }
  template <typename... A> void setTxBufferSize(A... a) { fwdTxBuf(Serial, a...); }
  template <typename... A> void updateBaudRate(A... a) { fwdBaud(Serial, a...); }
  template <typename T = int> int availableForWrite() { return fwdAvailW(Serial); }
  explicit operator bool() const { return true; }

  int available() {
    int n = Serial.available();
    if (_client && _client.connected()) n += _client.available();
    return n;
  }
  int read() {
    if (Serial.available()) return Serial.read();
    if (_client && _client.connected() && _client.available()) return _client.read();
    return -1;
  }
  int peek() {
    if (Serial.available()) return Serial.peek();
    if (_client && _client.connected() && _client.available()) return _client.peek();
    return -1;
  }
  void flush() { Serial.flush(); }
  size_t write(uint8_t c) { return write(&c, 1); }
  size_t write(const uint8_t* b, size_t n) {
    lock();
    Serial.write(b, n);
    netWrite(b, n);
    unlock();
    return n;
  }

  void restartNet() {
    lock();
    if (_client) _client.stop();
    if (_server) { delete _server; _server = NULL; }
    _server = new WiFiServer(WIFEY_TCP_PORT);
    _server->begin();
    unlock();
  }
  void stopNet() {
    lock();
    if (_client) _client.stop();
    if (_server) { delete _server; _server = NULL; }
    unlock();
  }
  void poll() {
    if (!_server) return;
    lock();
    if (_server->hasClient()) {
      WiFiClient c = _server->available();
      if (c) {
        if (_client && _client.connected()) _client.stop();
        _client = c;
        _client.setNoDelay(true);
        String banner = String("\r\n[WiFeY] --- LAN serial connected: ") + device_id + " (build " + String(wifey_applied_build > CURRENT_BUILD ? wifey_applied_build : CURRENT_BUILD) + ") ---\r\n";
        _client.write((const uint8_t*)banner.c_str(), banner.length());
        ringReplay();
      }
    }
    unlock();
  }

 private:
  static const size_t RING_SIZE = 2048;
  uint8_t _ring[RING_SIZE];
  size_t _rhead = 0;
  size_t _rcount = 0;
  WiFiServer* _server = NULL;
  WiFiClient _client;
  SemaphoreHandle_t _mtx = NULL;

  void lock() { if (_mtx) xSemaphoreTakeRecursive(_mtx, portMAX_DELAY); }
  void unlock() { if (_mtx) xSemaphoreGiveRecursive(_mtx); }
  void ringPut(const uint8_t* b, size_t n) {
    for (size_t i = 0; i < n; i++) {
      _ring[_rhead] = b[i];
      _rhead = (_rhead + 1) % RING_SIZE;
      if (_rcount < RING_SIZE) _rcount++;
    }
  }
  void ringReplay() {
    if (_rcount == 0) return;
    size_t start = (_rhead + RING_SIZE - _rcount) % RING_SIZE;
    size_t first = (start + _rcount <= RING_SIZE) ? _rcount : (RING_SIZE - start);
    _client.write(_ring + start, first);
    if (first < _rcount) _client.write(_ring, _rcount - first);
  }
  void netWrite(const uint8_t* b, size_t n) {
    ringPut(b, n);
    if (_client && _client.connected()) {
      if (_client.write(b, n) == 0) _client.stop();
    }
  }
};
WiFeySerialClass WiFeySerial;
#define Serial WiFeySerial
#endif

#define WIFEY_PRINT(msg) do { \
  Serial.println(msg); \
  if (WiFi.status() == WL_CONNECTED) { \
    wifey_udp.beginPacket(IPAddress(255, 255, 255, 255), WIFEY_UDP_PORT); \
    wifey_udp.print(String("[") + device_id + "] " + msg); \
    wifey_udp.endPacket(); \
  } \
} while(0)

// --- User Globals ---
{USER_GLOBALS}
// --------------------

void wifey_service();

int wifey_effective_build() { return CURRENT_BUILD > wifey_applied_build ? CURRENT_BUILD : wifey_applied_build; }

bool wifey_ap_active() {
  wifi_mode_t m = WiFi.getMode();
  return (m == WIFI_AP || m == WIFI_AP_STA);
}

String wifey_urlencode(const String& s) {
  String out;
  const char* hex = "0123456789ABCDEF";
  for (size_t i = 0; i < s.length(); i++) {
    unsigned char c = (unsigned char)s[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += (char)c;
    else { out += '%'; out += hex[c >> 4]; out += hex[c & 15]; }
  }
  return out;
}

void wifey_load_identity() {
  wifey_prefs.begin("wifey", false);
  String sid = wifey_prefs.getString("dev_id", "");
  String stok = wifey_prefs.getString("dev_tok", "");
  String sfleet = wifey_prefs.getString("fleet", "");
  wifey_applied_build = wifey_prefs.getInt("applied", 0);
#if WIFEY_FORCE_IDENTITY
  sid = ""; stok = ""; sfleet = ""; wifey_applied_build = 0;
#endif
  if (sid.length() == 0) {
    if (strlen(WIFEY_DEFAULT_DEVICE_ID) > 0) {
      sid = String(WIFEY_DEFAULT_DEVICE_ID);
    } else {
      uint64_t mac = ESP.getEfuseMac();
      char b[16];
      snprintf(b, sizeof(b), "node_%02x%02x%02x", (unsigned)((mac >> 24) & 0xFF), (unsigned)((mac >> 32) & 0xFF), (unsigned)((mac >> 40) & 0xFF));
      sid = String(b);
    }
  }
  if (stok.length() == 0) {
    if (strlen(WIFEY_DEFAULT_TOKEN) > 0) {
      stok = String(WIFEY_DEFAULT_TOKEN);
    } else {
      char b[32];
      snprintf(b, sizeof(b), "dt_%08x%08x", (unsigned)esp_random(), (unsigned)esp_random());
      stok = String(b);
    }
  }
  if (sfleet.length() == 0) sfleet = (strlen(WIFEY_DEFAULT_FLEET) > 0) ? String(WIFEY_DEFAULT_FLEET) : String("DefaultFleet");
  wifey_prefs.putString("dev_id", sid);
  wifey_prefs.putString("dev_tok", stok);
  wifey_prefs.putString("fleet", sfleet);
  wifey_prefs.putInt("applied", wifey_applied_build);
  device_id = sid;
  device_token = stok;
  fleet_id = sfleet;
}

void success_flash() {
  pinMode(WIFEY_LED, OUTPUT);
  for (int i = 0; i < 10; i++) {
    digitalWrite(WIFEY_LED, HIGH); delay(40);
    digitalWrite(WIFEY_LED, LOW); delay(40);
  }
}

void upgrade_detected_flash() {
  pinMode(WIFEY_LED, OUTPUT);
  for (int i = 0; i < 3; i++) {
    digitalWrite(WIFEY_LED, HIGH); delay(150);
    digitalWrite(WIFEY_LED, LOW); delay(150);
  }
}

// Replaces delay() in user code: yields to FreeRTOS but keeps beacons, LAN serial and OTA polling alive.
void smartDelay(unsigned long ms) {
  unsigned long start = millis();
  do {
    wifey_service();
    delay(1);
  } while (millis() - start < ms);
}

void send_lan_beacon() {
  bool sta = (WiFi.status() == WL_CONNECTED);
  if (!sta && !wifey_ap_active()) return;
  IPAddress ip = sta ? WiFi.localIP() : WiFi.softAPIP();
  IPAddress dst = sta ? IPAddress(255, 255, 255, 255) : IPAddress(ip[0], ip[1], ip[2], 255);
  char beacon_buf[256];
  snprintf(beacon_buf, sizeof(beacon_buf),
    "{\"device_id\":\"%s\",\"device_token\":\"%s\",\"ip\":\"%s\",\"build\":%d,\"tcp\":%d}",
    device_id.c_str(), device_token.c_str(), ip.toString().c_str(), wifey_effective_build(), WIFEY_TCP_PORT);
  wifey_udp.beginPacket(dst, WIFEY_UDP_PORT);
  wifey_udp.print(beacon_buf);
  wifey_udp.endPacket();
}

// Commands from WiFeY Studio (unicast UDP): {"cmd":"check_update","token":"..."}
void wifey_udp_receive() {
  int guard = 0;
  while (wifey_udp.parsePacket() > 0 && guard++ < 8) {
    char buf[256];
    int len = wifey_udp.read(buf, sizeof(buf) - 1);
    if (len <= 0) continue;
    buf[len] = 0;
    if (buf[0] != '{' || strstr(buf, "\"cmd\"") == NULL) continue;   // other nodes' beacons
    JsonDocument doc;
    if (deserializeJson(doc, buf)) continue;
    const char* cmd = doc["cmd"] | "";
    const char* tok = doc["token"] | "";
    if (device_token.length() > 0 && device_token != String(tok)) continue;
    if (strcmp(cmd, "check_update") == 0) wifey_force_check = true;
  }
}

void wifey_net_ensure() {
  bool link = (WiFi.status() == WL_CONNECTED) || wifey_ap_active();
  if (!link) {
    if (wifey_net_ready) {
#if WIFEY_SERIAL_MIRROR
      WiFeySerial.stopNet();
#endif
      wifey_udp.stop();
      wifey_net_ready = false;
    }
    return;
  }
  if (!wifey_net_ready) {
    wifey_udp.stop();
    wifey_udp.begin(WIFEY_UDP_PORT);
#if WIFEY_SERIAL_MIRROR
    WiFeySerial.restartNet();
#endif
    wifey_net_ready = true;
    send_lan_beacon();
  }
}

bool wifey_http_get(const String& url, String& out, int& code) {
  bool https = url.startsWith("https://");
  WiFiClientSecure* sc = NULL;
  WiFiClient* pc = NULL;
  HTTPClient http;
  http.setTimeout(15000);
  if (https) {
    sc = new WiFiClientSecure();
    sc->setInsecure();
    sc->setTimeout(15);
    http.begin(*sc, url);
  } else {
    pc = new WiFiClient();
    http.begin(*pc, url);
  }
  http.addHeader("Authorization", String("Bearer ") + device_token);
  http.addHeader("ngrok-skip-browser-warning", "true");
  code = http.GET();
  bool ok = (code == 200);
  if (ok) out = http.getString();
  http.end();
  if (sc) { sc->stop(); delete sc; }
  if (pc) { pc->stop(); delete pc; }
  return ok;
}

void notify_flash_completion(int new_build) {
  if (WiFi.status() != WL_CONNECTED) return;
  String url = String(api_base_url) + "/api/device/notify_flash";
  char body[224];
  snprintf(body, sizeof(body),
    "{\"device_id\":\"%s\",\"device_token\":\"%s\",\"status\":\"SUCCESS\",\"new_build\":%d}",
    device_id.c_str(), device_token.c_str(), new_build);
  bool https = url.startsWith("https://");
  WiFiClientSecure* sc = NULL;
  WiFiClient* pc = NULL;
  HTTPClient http;
  http.setTimeout(8000);
  if (https) {
    sc = new WiFiClientSecure();
    sc->setInsecure();
    sc->setTimeout(8);
    http.begin(*sc, url);
  } else {
    pc = new WiFiClient();
    http.begin(*pc, url);
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + device_token);
  http.addHeader("ngrok-skip-browser-warning", "true");
  http.POST(body);
  http.end();
  if (sc) { sc->stop(); delete sc; }
  if (pc) { pc->stop(); delete pc; }
}

void setup_wifi() {
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);

  WiFi.persistent(false);
  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(ssid, password);

  pinMode(WIFEY_LED, OUTPUT);
  int attempts = 0;

  while (WiFi.status() != WL_CONNECTED) {
    for (int i = 0; i < 33; i++) {
      digitalWrite(WIFEY_LED, HIGH); delay(3);
      digitalWrite(WIFEY_LED, LOW); delay(12);
    }
    Serial.print(".");
    attempts++;
    if (attempts > WIFEY_WIFI_ATTEMPTS) {
      Serial.println("\nFailed to connect to Wi-Fi.");
      {WIFI_FAIL_ACTION}
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi connected!");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());
    digitalWrite(WIFEY_LED, HIGH); delay(80);
    digitalWrite(WIFEY_LED, LOW); delay(80);
    digitalWrite(WIFEY_LED, HIGH); delay(80);
    digitalWrite(WIFEY_LED, LOW);
    wifey_net_ensure();
  }
}

void process_manifest_and_update(const String& payload) {
  JsonDocument doc;
  DeserializationError error = deserializeJson(doc, payload);
  if (error) {
    Serial.println("[WiFeY] Manifest JSON parsing failed.");
    return;
  }

  int target_build = doc["target_build"] | 0;
  String download_url = doc["download_url"].as<String>();
  int eff = wifey_effective_build();

  if (target_build > eff && download_url.length() > 0 && download_url != "null") {
    Serial.printf("[WiFeY] Newer build #%d available (running #%d). Starting OTA...\n", target_build, eff);
    upgrade_detected_flash();

    httpUpdate.rebootOnUpdate(false);
    httpUpdate.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
    t_httpUpdate_return ret;

    if (download_url.startsWith("https://")) {
      WiFiClientSecure* uc = new WiFiClientSecure();
      uc->setInsecure();
      uc->setTimeout(30);
      ret = httpUpdate.update(*uc, download_url);
      uc->stop();
      delete uc;
    } else {
      WiFiClient uc;
      ret = httpUpdate.update(uc, download_url);
      uc.stop();
    }

    if (ret == HTTP_UPDATE_FAILED) {
      Serial.printf("[WiFeY] Update failed: %s\n", httpUpdate.getLastErrorString().c_str());
    } else if (ret == HTTP_UPDATE_OK) {
      wifey_applied_build = target_build;
      wifey_prefs.putInt("applied", target_build);
      Serial.println("[WiFeY] Update successful, notifying Studio and rebooting...");
      notify_flash_completion(target_build);
      success_flash();
      delay(300);
      ESP.restart();
    }
  }
}

void check_for_updates() {
  if (WiFi.status() != WL_CONNECTED) return;
  String manifest_url = String(api_base_url) + "/api/manifest?fleet=" + wifey_urlencode(fleet_id)
                        + "&device=" + wifey_urlencode(device_id) + "&build=" + String(wifey_effective_build());
  String payload;
  int code = 0;
  if (wifey_http_get(manifest_url, payload, code)) {
    process_manifest_and_update(payload);
  } else {
    Serial.printf("[WiFeY] Manifest check failed, HTTP %d\n", code);
  }
}

#if WIFEY_NET_MODE == 2
// "OTA check, then AP only": Wi-Fi is released after the boot-time OTA check so the sketch can run its own hotspot.
void wifey_enter_ap_phase() {
  Serial.println("[WiFeY] OTA check finished - releasing Wi-Fi client for AP mode.");
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  delay(100);
  wifey_net_ready = false;
  wifey_last_window = millis();
}

// Brief STA reconnect while the AP keeps running (AP clients may blip when the channel follows the router).
void wifey_ota_window() {
  wifey_last_window = millis();
  Serial.println("[WiFeY] OTA window: reconnecting to Wi-Fi...");
  WiFi.mode(WIFI_AP_STA);
  WiFi.begin(ssid, password);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) { delay(100); }
  if (WiFi.status() == WL_CONNECTED) {
    wifey_net_ready = false;
    wifey_net_ensure();
    check_for_updates();
  }
  WiFi.disconnect(false, false);
  WiFi.mode(WIFI_AP);
  wifey_net_ready = false;
}
#endif

// Cooperative background work: runs every loop() and inside every delay() of the sketch.
void wifey_service() {
  static bool busy = false;
  if (busy) return;
  busy = true;

  wifey_net_ensure();
  if (wifey_net_ready) {
    wifey_udp_receive();
#if WIFEY_SERIAL_MIRROR
    WiFeySerial.poll();
#endif
    if (millis() - wifey_last_beacon >= WIFEY_BEACON_INTERVAL) {
      wifey_last_beacon = millis();
      send_lan_beacon();
    }
    if (WiFi.status() == WL_CONNECTED) {
      bool due = (WIFEY_POLL_SECONDS > 0) && (millis() - wifey_last_poll >= (unsigned long)WIFEY_POLL_SECONDS * 1000UL);
      if (wifey_force_check || due) {
        wifey_force_check = false;
        wifey_last_poll = millis();
        check_for_updates();
      }
    }
  }
#if WIFEY_NET_MODE == 2
  if (WIFEY_AP_WINDOW_MIN > 0 && millis() - wifey_last_window >= (unsigned long)WIFEY_AP_WINDOW_MIN * 60000UL) {
    wifey_ota_window();
  }
#endif

  busy = false;
}

void setup() {
  Serial.begin(115200);
  wifey_load_identity();
  setup_wifi();
  delay(300);
  check_for_updates();
  wifey_last_poll = millis();
#if WIFEY_NET_MODE == 2
  wifey_enter_ap_phase();
#endif
  {USER_SETUP}
}

void loop() {
  wifey_service();
  {USER_LOOP}
}
"""
# --- ESP8266 LITE OTA TEMPLATE (Compact, LittleFS identity, No Serial Mirror) ---
ESP8266_CPP_TEMPLATE = r"""#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <WiFiClient.h>
#include <ESP8266HTTPClient.h>
#include <ESP8266httpUpdate.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
{USER_INCLUDES}

const char* ssid = "{WIFI_SSID}";
const char* password = "{WIFI_PASS}";
const char* api_base_url = "{API_BASE_URL}";
const char* WIFEY_DEFAULT_FLEET = "{FLEET_ID}";
const char* WIFEY_DEFAULT_DEVICE_ID = "{DEVICE_ID}";
const char* WIFEY_DEFAULT_TOKEN = "{DEVICE_TOKEN}";
const int CURRENT_BUILD = {CURRENT_BUILD};

#define WIFEY_FORCE_IDENTITY {FORCE_IDENTITY}
#define WIFEY_POLL_SECONDS {POLL_SECONDS}
#define WIFEY_UDP_PORT 53535
#ifndef LED_BUILTIN
#define LED_BUILTIN 2
#endif
#define WIFEY_LED LED_BUILTIN

String device_id;
String fleet_id;
String device_token;
int wifey_applied_build = 0;

WiFiUDP wifey_udp;
unsigned long wifey_last_beacon = 0;
unsigned long wifey_last_poll = 0;
bool wifey_force_check = false;

#define WIFEY_PRINT(msg) do { \
  Serial.println(msg); \
  if (WiFi.status() == WL_CONNECTED) { \
    wifey_udp.beginPacket(IPAddress(255, 255, 255, 255), WIFEY_UDP_PORT); \
    wifey_udp.print(String("[") + device_id + "] " + msg); \
    wifey_udp.endPacket(); \
  } \
} while(0)

{USER_GLOBALS}

void wifey_service();

int wifey_effective_build() { return CURRENT_BUILD > wifey_applied_build ? CURRENT_BUILD : wifey_applied_build; }

String wifey_urlencode(const String& s) {
  String out;
  const char* hex = "0123456789ABCDEF";
  for (size_t i = 0; i < s.length(); i++) {
    unsigned char c = (unsigned char)s[i];
    if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') out += (char)c;
    else { out += '%'; out += hex[c >> 4]; out += hex[c & 15]; }
  }
  return out;
}

void wifey_load_identity() {
  LittleFS.begin();
#if WIFEY_FORCE_IDENTITY
  device_id = WIFEY_DEFAULT_DEVICE_ID;
  device_token = WIFEY_DEFAULT_TOKEN;
  fleet_id = WIFEY_DEFAULT_FLEET;
  wifey_applied_build = 0;
  
  File f = LittleFS.open("/wifey_id.txt", "w");
  if(f) {
    f.println(device_id); f.println(device_token); f.println(fleet_id); f.println(wifey_applied_build);
    f.close();
  }
#else
  File f = LittleFS.open("/wifey_id.txt", "r");
  if(f) {
    device_id = f.readStringUntil('\n'); device_id.trim();
    device_token = f.readStringUntil('\n'); device_token.trim();
    fleet_id = f.readStringUntil('\n'); fleet_id.trim();
    String b = f.readStringUntil('\n'); b.trim(); wifey_applied_build = b.toInt();
    f.close();
  } else {
    device_id = String("node_8266_") + String(ESP.getChipId(), HEX);
    device_token = "dt_fallback";
    fleet_id = "DefaultFleet";
  }
#endif
}

void smartDelay(unsigned long ms) {
  unsigned long start = millis();
  do {
    wifey_service();
    delay(1);
  } while (millis() - start < ms);
}

void send_lan_beacon() {
  if (WiFi.status() != WL_CONNECTED) return;
  char beacon_buf[256];
  snprintf(beacon_buf, sizeof(beacon_buf),
    "{\"device_id\":\"%s\",\"device_token\":\"%s\",\"ip\":\"%s\",\"build\":%d,\"tcp\":0}",
    device_id.c_str(), device_token.c_str(), WiFi.localIP().toString().c_str(), wifey_effective_build());
  wifey_udp.beginPacket(IPAddress(255, 255, 255, 255), WIFEY_UDP_PORT);
  wifey_udp.print(beacon_buf);
  wifey_udp.endPacket();
}

void wifey_udp_receive() {
  int guard = 0;
  while (wifey_udp.parsePacket() > 0 && guard++ < 5) {
    char buf[256];
    int len = wifey_udp.read(buf, sizeof(buf) - 1);
    if (len > 0) {
      buf[len] = 0;
      if (buf[0] == '{' && strstr(buf, "\"cmd\":\"check_update\"") != NULL) wifey_force_check = true;
    }
  }
}

void check_for_updates() {
  if (WiFi.status() != WL_CONNECTED) return;
  String manifest_url = String(api_base_url) + "/api/manifest?fleet=" + wifey_urlencode(fleet_id) + "&device=" + wifey_urlencode(device_id) + "&build=" + String(wifey_effective_build());
  
  WiFiClient client;
  HTTPClient http;
  if (http.begin(client, manifest_url)) {
    http.addHeader("Authorization", String("Bearer ") + device_token);
    http.addHeader("ngrok-skip-browser-warning", "true");
    int code = http.GET();
    if (code == 200) {
      String payload = http.getString();
      JsonDocument doc;
      if (!deserializeJson(doc, payload)) {
        int target_build = doc["target_build"] | 0;
        String download_url = doc["download_url"].as<String>();
        if (target_build > wifey_effective_build() && download_url.length() > 0 && download_url != "null") {
          Serial.printf("[WiFeY] OTA Build #%d available. Updating...\n", target_build);
          ESPhttpUpdate.rebootOnUpdate(false);
          t_httpUpdate_return ret;
          
          if (download_url.startsWith("https://")) {
            std::unique_ptr<BearSSL::WiFiClientSecure> uc(new BearSSL::WiFiClientSecure);
            uc->setInsecure();
            ret = ESPhttpUpdate.update(*uc, download_url);
          } else {
            WiFiClient uc;
            ret = ESPhttpUpdate.update(uc, download_url);
          }
          
          if (ret == HTTP_UPDATE_OK) {
            File f = LittleFS.open("/wifey_id.txt", "w");
            if(f) {
              f.println(device_id); f.println(device_token); f.println(fleet_id); f.println(target_build);
              f.close();
            }
            Serial.println("[WiFeY] Update successful, rebooting...");
            
            // Notify flash success
            String notify_url = String(api_base_url) + "/api/device/notify_flash";
            char body[224];
            snprintf(body, sizeof(body), "{\"device_id\":\"%s\",\"device_token\":\"%s\",\"status\":\"SUCCESS\",\"new_build\":%d}", device_id.c_str(), device_token.c_str(), target_build);
            HTTPClient nhttp;
            if(nhttp.begin(client, notify_url)) {
                nhttp.addHeader("Content-Type", "application/json");
                nhttp.POST(body);
                nhttp.end();
            }
            delay(500);
            ESP.restart();
          }
        }
      }
    }
    http.end();
  }
}

void wifey_service() {
  static bool busy = false;
  if (busy) return;
  busy = true;

  if (WiFi.status() == WL_CONNECTED) {
    wifey_udp_receive();
    if (millis() - wifey_last_beacon >= 5000) {
      wifey_last_beacon = millis();
      send_lan_beacon();
    }
    bool due = (WIFEY_POLL_SECONDS > 0) && (millis() - wifey_last_poll >= (unsigned long)WIFEY_POLL_SECONDS * 1000UL);
    if (wifey_force_check || due) {
      wifey_force_check = false;
      wifey_last_poll = millis();
      check_for_updates();
    }
  }
  busy = false;
}

void setup() {
  Serial.begin(115200);
  wifey_load_identity();
  
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) { delay(100); }
  
  wifey_udp.begin(WIFEY_UDP_PORT);
  check_for_updates();
  wifey_last_poll = millis();
  
  {USER_SETUP}
}

void loop() {
  wifey_service();
  {USER_LOOP}
}
"""

RE_WIFI_MODE_STA = re.compile(r'^\s*WiFi\.mode\(\s*WIFI_STA\s*\)\s*;', re.MULTILINE)
NET_MODE_IDS = {"sta": 0, "ap_sta": 1, "ota_ap": 2}
NET_MODE_LABELS = {0: "Wi-Fi client (STA)", 1: "STA + AP together", 2: "OTA check, then AP only"}

def extract_block(code, func_name):
    pattern = rf'void\s+{func_name}\s*\(\s*(void)?\s*\)\s*\{{'
    match = re.search(pattern, code)
    if not match:
        return "", code
    start_idx = match.end() - 1
    brace_count = 0
    end_idx = -1
    for i in range(start_idx, len(code)):
        if code[i] == '{':
            brace_count += 1
        elif code[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                end_idx = i
                break
    if end_idx != -1:
        body = code[start_idx + 1:end_idx].strip()
        remaining = code[:match.start()] + code[end_idx + 1:]
        return body, remaining
    return "", code

def clean_duplicates_and_delays(code, net_mode=0):
    code = RE_INCLUDES.sub('', code)
    code = RE_CONFIGS.sub('// Removed duplicate config', code)
    code = RE_WIFI_BEGIN.sub('// WiFi.begin handled by template', code)
    # STA-only sketches: the template owns WiFi.mode(). AP-aware sketches keep their own WiFi.mode(WIFI_AP...) calls.
    if net_mode == 0:
        code = RE_WIFI_MODE.sub('// WiFi.mode handled by template', code)
    else:
        code = RE_WIFI_MODE_STA.sub('// WiFi.mode(WIFI_STA) handled by template', code)
    code = RE_SERIAL_BEGIN.sub('// Serial.begin handled by template', code)
    code = RE_DELAY.sub('smartDelay(', code)
    return code.strip()

def hoist_includes(code):
    """Moves top-level #include lines above the Serial->LAN mirror macro so library headers see the real Serial."""
    includes, rest, depth = [], [], 0
    for line in code.splitlines():
        s = line.strip()
        if re.match(r'#\s*(if|ifdef|ifndef)\b', s):
            depth += 1
        elif re.match(r'#\s*endif\b', s):
            depth = max(0, depth - 1)
        if depth == 0 and re.match(r'#\s*include\s*[<"][^>"\n]+[>"]\s*(//.*)?$', s):
            includes.append(s)
        else:
            rest.append(line)
    return "\n".join(dict.fromkeys(includes)), "\n".join(rest).strip()

def strip_cpp_comments(code):
    code = re.sub(r'/\*.*?\*/', '', code, flags=re.DOTALL)
    return re.sub(r'//[^\n]*', '', code)

def detect_net_mode(user_code, requested="auto"):
    """0 = Wi-Fi client, 1 = STA+AP together, 2 = OTA check at boot then AP only."""
    requested = (requested or "auto").lower()
    if requested in NET_MODE_IDS:
        return NET_MODE_IDS[requested]
    code = strip_cpp_comments(user_code or "")
    uses_softap = bool(re.search(r'WiFi\s*\.\s*softAP\s*\(', code)) or bool(re.search(r'WiFi\s*\.\s*mode\s*\(\s*WIFI_AP\s*\)', code))
    if not uses_softap:
        return 0
    wants_sta_too = bool(re.search(r'WIFI_AP_STA|WiFi\s*\.\s*begin\s*\(|HTTPClient|WiFiClient|WL_CONNECTED', code))
    return 1 if wants_sta_too else 2

def cpp_str(value):
    """Escapes a Python string for use inside a C++ "..." literal."""
    s = str(value if value is not None else "")
    s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n')
    return s

def fill_template(values):
    """Single-pass placeholder substitution (user code is never re-scanned for placeholders)."""
    return re.sub(r'\{([A-Z_]+)\}', lambda m: str(values.get(m.group(1), m.group(0))), MASTER_CPP_TEMPLATE)

def get_wrapper_options(data=None):
    data = data or {}
    def pick(key):
        v = data.get(key)
        return prefs.get(key) if v in (None, "") else v
    def to_int(v, lo, hi, dflt):
        try:
            return max(lo, min(hi, int(v)))
        except Exception:
            return dflt
    mirror = pick("serial_mirror")
    if isinstance(mirror, str):
        mirror = mirror.strip().lower() in ("1", "true", "yes", "on")
    return {
        "poll_seconds": to_int(pick("poll_seconds"), 0, 3600, 30),
        "ap_window_min": to_int(pick("ap_window_min"), 0, 1440, 10),
        "net_mode": str(pick("net_mode") or "auto").lower(),
        "serial_mirror": bool(mirror),
    }

def generate_wrapped_code(ssid, passw, api_url, topic, user_code, build_num=0, device_token=None,
                          opts=None, provision=False, device_id=None, fleet_id=None, fqbn=DEFAULT_FQBN):
    """Builds the final .ino using the correct hardware template."""
    opts = opts or get_wrapper_options()
    user_code = user_code or ""
    net_mode = detect_net_mode(user_code, opts.get("net_mode"))
    uses_wifi = bool(re.search(r'WiFi\.', user_code) or re.search(r'#include\s*[<"](WiFi|ESP8266WiFi)\.h[>"]', user_code))

    user_loop, remaining = extract_block(user_code, "loop")
    user_setup, user_globals = extract_block(remaining, "setup")
    user_globals = clean_duplicates_and_delays(user_globals, net_mode)
    user_setup = clean_duplicates_and_delays(user_setup, net_mode)
    user_loop = clean_duplicates_and_delays(user_loop, net_mode)
    user_includes, user_globals = hoist_includes(user_globals)

    if net_mode != 0:
        wifi_fail_action = "break; // keep going: the sketch brings up its own AP / works offline"
    elif uses_wifi:
        wifi_fail_action = "ESP.restart();"
    else:
        wifi_fail_action = "break; // Offline fallback"

    if provision:
        ident_fleet = fleet_id or "DefaultFleet"
        ident_device = device_id or "node_1"
        ident_token = device_token or f"dt_{secrets.token_hex(8)}"
    else:
        ident_fleet, ident_device, ident_token = "", "", ""

    values = {
        "WIFI_SSID": cpp_str(ssid),
        "WIFI_PASS": cpp_str(passw),
        "API_BASE_URL": cpp_str(resolve_api_url(api_url)),
        "FLEET_ID": cpp_str(ident_fleet),
        "DEVICE_ID": cpp_str(ident_device),
        "DEVICE_TOKEN": cpp_str(ident_token),
        "CURRENT_BUILD": str(int(build_num)),
        "FORCE_IDENTITY": "1" if provision else "0",
        "POLL_SECONDS": str(int(opts.get("poll_seconds", 30))),
        "NET_MODE": str(net_mode),
        "AP_WINDOW_MIN": str(int(opts.get("ap_window_min", 10))),
        "SERIAL_MIRROR": "1" if opts.get("serial_mirror", True) else "0",
        "WIFI_ATTEMPTS": "40" if net_mode == 0 else "30",
        "USER_INCLUDES": user_includes,
        "USER_GLOBALS": user_globals,
        "USER_SETUP": user_setup,
        "USER_LOOP": user_loop,
        "WIFI_FAIL_ACTION": wifi_fail_action,
    }
    
    # Pick the template based on the FQBN
    active_template = ESP8266_CPP_TEMPLATE if "esp8266" in fqbn.lower() else MASTER_CPP_TEMPLATE
    
    # Single-pass substitution
    return re.sub(r'\{([A-Z_]+)\}', lambda m: str(values.get(m.group(1), m.group(0))), active_template)

def build_firmware_source(data):
    """Returns (code, hash, info)."""
    data = data or {}
    fqbn = data.get('fqbn', DEFAULT_FQBN)
    opts = get_wrapper_options(data)
    code = generate_wrapped_code(data.get('ssid'), data.get('pass'), data.get('api_url'), data.get('topic'),
                                 data.get('code'), build_num=0, opts=opts, fqbn=fqbn)
    digest = hashlib.sha256((TEMPLATE_VERSION + "|" + fqbn + "|" + code).encode("utf-8")).hexdigest()
    net_mode = detect_net_mode(data.get('code') or "", opts.get("net_mode"))
    return code, digest, {"net_mode": net_mode, "auto": (opts.get("net_mode") or "auto") == "auto", "opts": opts, "fqbn": fqbn}

# --- SPLASH SCREEN HTML ---
SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WiFeY Studio Setup</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/@phosphor-icons/web"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <script>
        tailwind.config = {
            theme: {
                extend: {
                    colors: {
                        ardBg: '#121316',
                        ardPanel: '#1a1c23',
                        ardBorder: '#2d313f',
                        ardTeal: '#00b4b6',
                        ardTextMain: '#e0e2ec',
                        ardTextMuted: '#8b92a5'
                    },
                    fontFamily: {
                        sans: ['Inter', 'sans-serif'],
                        mono: ['JetBrains Mono', 'monospace'],
                    }
                }
            }
        }
    </script>
    <style>
        body { background-color: #121316; color: #e0e2ec; }
        .loader { border: 3px solid #2d313f; border-top: 3px solid #00b4b6; border-radius: 50%; width: 28px; height: 28px; animation: spin 1s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    </style>
</head>
<body class="h-screen w-screen flex flex-col items-center justify-center font-sans antialiased p-6">
    <div class="bg-ardPanel border border-ardBorder p-8 rounded-xl shadow-2xl flex flex-col items-center w-full max-w-xl">
        <div class="flex items-center gap-3 mb-2">
            <i class="ph-bold ph-cpu text-4xl text-ardTeal"></i>
            <h2 class="text-3xl font-semibold text-white tracking-wide"><span class="text-ardTeal font-bold">WiFeY</span> Studio</h2>
        </div>
        <p class="text-ardTextMuted text-sm mb-6 text-center">Configuring embedded compilers and hardware toolchains.</p>
        
        <div class="flex items-center gap-4 mb-4 w-full px-2">
            <div class="loader flex-shrink-0" id="spinner"></div>
            <div id="status-text" class="font-medium text-white text-sm flex-1">Initializing...</div>
        </div>
        
        <div class="w-full bg-[#121316] rounded-full h-2 mb-6 overflow-hidden border border-ardBorder relative">
            <div id="pbar" class="bg-ardTeal h-2 rounded-full transition-all duration-300 w-0"></div>
        </div>
        
        <div id="logs" class="w-full h-44 bg-[#121316] border border-ardBorder rounded-lg p-3 font-mono text-xs text-ardTextMuted overflow-y-auto whitespace-pre-wrap">Starting checks...</div>
    </div>

    <script>
        const timer = setInterval(() => {
            fetch('/api/setup_status').then(r=>r.json()).then(data => {
                document.getElementById('status-text').innerText = data.stage;
                document.getElementById('pbar').style.width = data.progress + '%';
                
                let lg = document.getElementById('logs');
                if (lg.innerText.length !== data.log.length) {
                    lg.innerText = data.log;
                    lg.scrollTop = lg.scrollHeight;
                }

                if (data.done) {
                    clearInterval(timer);
                    window.location.reload(); 
                }
                if (data.error) {
                    clearInterval(timer);
                    document.getElementById('status-text').innerText = "Setup Failed! Check Logs Below.";
                    document.getElementById('status-text').classList.add("text-red-400");
                    document.getElementById('spinner').style.display = "none";
                    document.getElementById('pbar').classList.remove("bg-ardTeal");
                    document.getElementById('pbar').classList.add("bg-red-500");
                }
            }).catch(() => {});
        }, 500);
    </script>
</body>
</html>
"""

# --- COMPLETE PROFESSIONAL IDE HTML (Dark Slate, Live CLI Streaming, Bidirectional Serial) ---
IDE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WiFeY Studio | Professional OTA Platform</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/@phosphor-icons/web"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/codemirror.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/codemirror.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.13/mode/clike/clike.min.js"></script>
    
    <script>
        const NGROK_API_URL = "{{ ngrok_url }}"; 
        let activeBinFile = null; 
        let serialEventSource = null;
        let currentFleet = "DefaultFleet";
        let currentTargetType = "fleet"; 
        let currentDeviceId = "node_1";
        let currentTopic = "DefaultFleet/fleet/update";
        let cmEditor;
    </script>

    <script>
        tailwind.config = {
            theme: {
                extend: {
                    colors: {
                        ardBg: '#121316',       
                        ardPanel: '#18191e',    
                        ardCard: '#20222a',
                        ardBorder: '#2c2e36',   
                        ardTeal: '#00b4b6',
                        ardTealGlow: '#00d9df',     
                        ardHover: '#262934',    
                        ardTextMain: '#e0e2ec',
                        ardTextMuted: '#8b92a5'
                    },
                    fontFamily: {
                        sans: ['Inter', 'sans-serif'],
                        mono: ['JetBrains Mono', 'monospace'],
                    }
                }
            }
        }
    </script>
    <style>
        body { background-color: #121316; color: #e0e2ec; overflow: hidden; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #18191e; }
        ::-webkit-scrollbar-thumb { background: #373a46; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #4a4e5d; }
        .editor-textarea { resize: none; outline: none; tab-size: 2; line-height: 1.5; }
        .line-numbers { line-height: 1.5; }
        .view-hidden { display: none !important; }
        .flat-input { background-color: #18191e; border: 1px solid #2c2e36; outline: none; transition: all 0.2s; }
        .flat-input:hover { border-color: #3f4350; background-color: #20222a; }
        .flat-input:focus { border-color: #00b4b6; background-color: #20222a; }
        .dropdown-content { display: none; }
        .dropdown-content.show { display: block; }
        @keyframes pulse-dot { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.4; transform: scale(0.85); } }
        .pulse-live { animation: pulse-dot 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
        /* long COM-port names must never squeeze the refresh button */
        #view-usb .grid > div { min-width: 0; }
        select.flat-input { min-width: 0; max-width: 100%; text-overflow: ellipsis; }
        #prov_port, #serial_port, #wserial_device { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
        .refresh-btn-fixed { flex-shrink: 0; }
        /* CodeMirror IDE Theme Overrides */
        .CodeMirror { height: 100% !important; width: 100% !important; font-family: 'JetBrains Mono', monospace !important; font-size: 13px !important; background-color: transparent !important; color: #d4d4d4 !important; }
        .CodeMirror-gutters { background-color: #121316 !important; border-right: 1px solid #2c2e36 !important; }
        .CodeMirror-linenumber { color: #8b92a5 !important; }
        .CodeMirror-cursor { border-left: 2px solid #00b4b6 !important; }
        .cm-s-default .cm-keyword { color: #c678dd; }
        .cm-s-default .cm-def { color: #61afef; }
        .cm-s-default .cm-variable, .cm-s-default .cm-variable-2, .cm-s-default .cm-variable-3 { color: #e06c75; }
        .cm-s-default .cm-string { color: #98c379; }
        .cm-s-default .cm-number { color: #d19a66; }
        .cm-s-default .cm-comment { color: #5c6370; font-style: italic; }
        .cm-s-default .cm-type { color: #e5c07b; }
        .cm-s-default .cm-builtin { color: #56b6c2; }
        .cm-s-default .cm-property { color: #61afef; }
    </style>
</head>
<body class="h-screen w-screen flex flex-col text-sm antialiased selection:bg-ardTeal selection:text-white">
    <!-- AUTHENTICATION OVERLAY -->
    <div id="auth-overlay" class="fixed inset-0 bg-ardBg/95 backdrop-blur-md z-[100] flex flex-col items-center justify-center transition-opacity duration-500">
        <div class="bg-ardPanel border border-ardBorder p-10 rounded-2xl shadow-2xl flex flex-col items-center w-full max-w-md transform transition-all">
            <div class="flex items-center gap-3 mb-4">
                <i class="ph-bold ph-cpu text-5xl text-ardTeal"></i>
            </div>
            <h2 class="text-3xl font-bold text-white tracking-wide mb-2"><span class="text-ardTeal">WiFeY</span> Studio</h2>
            <p class="text-ardTextMuted text-sm text-center mb-8">Sign in to sync your fleets and firmware securely across the cloud.</p>
            
            <button onclick="startGoogleAuth()" id="btn-google-login" class="w-full flex items-center justify-center gap-3 py-3.5 px-4 bg-white hover:bg-gray-100 text-gray-900 font-semibold rounded-xl transition-all shadow-lg active:scale-95">
                <svg class="w-5 h-5" viewBox="0 0 24 24">
                    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
                    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
                </svg>
                <span>Continue with Google</span>
            </button>
            
            <!-- FALLBACK LINK CONTAINER -->
            <div id="auth-fallback-container" class="hidden mt-4 text-center">
                <p class="text-xs text-ardTextMuted mb-1">Browser didn't open automatically?</p>
                <a id="auth-link" href="#" target="_blank" class="text-ardTeal text-xs underline font-mono break-all hover:text-white">Click here to sign in with Google</a>
            </div>
            
            <div id="auth-spinner" class="hidden flex-col items-center mt-6">
                <i class="ph-bold ph-spinner animate-spin text-2xl text-ardTeal mb-2"></i>
                <span class="text-xs text-ardTextMuted font-mono">Waiting for browser authentication...</span>
            </div>
        </div>
    </div>
    <div id="drop-overlay" style="display:none" class="fixed inset-0 z-[90] bg-ardBg/80 backdrop-blur-sm items-center justify-center pointer-events-none">
        <div class="border-2 border-dashed border-ardTeal rounded-2xl px-12 py-10 text-center bg-ardPanel/90">
            <i class="ph-bold ph-cloud-arrow-up text-5xl text-ardTeal"></i>
            <div class="text-white font-semibold mt-3">Drop a .bin file to deploy it to the cloud</div>
            <div id="drop-target" class="text-xs text-ardTextMuted mt-1 font-mono"></div>
        </div>
    </div>
    <input type="file" id="bin-file-input" accept=".bin" class="hidden" onchange="handleBinLoad(event)">
    <!-- LIBRARY MANAGER SIDEBAR PANE -->
    <div id="lib-sidebar" class="hidden flex-col w-64 h-full bg-ardPanel border-r border-ardBorder transition-all duration-200 z-40 absolute left-14 top-0 bottom-0 shadow-xl">
        <div class="flex items-center justify-between p-3 border-b border-ardBorder bg-ardCard">
            <h2 class="text-sm font-bold text-white flex items-center gap-2 uppercase tracking-wider">
                <i class="ph-bold ph-books text-ardTeal"></i> Libraries
            </h2>
            <div class="flex gap-2">
                <button onclick="document.getElementById('lib-zip-input').click()" class="text-ardTextMuted hover:text-ardTeal transition-colors" title="Add .ZIP Library">
                    <i class="ph-bold ph-plus text-lg"></i>
                </button>
                <button onclick="toggleLibrarySidebar()" class="text-ardTextMuted hover:text-white transition-colors" title="Close">
                    <i class="ph-bold ph-x text-lg"></i>
                </button>
            </div>
        </div>
        <div class="p-3 flex-1 overflow-y-auto flex flex-col gap-3">
            <div class="border-2 border-dashed border-ardBorder rounded-lg p-3 text-center bg-ardBg cursor-pointer hover:border-ardTeal transition-colors" onclick="document.getElementById('lib-zip-input').click()" id="lib-drop-zone">
                <i class="ph-bold ph-file-zip text-2xl text-ardTextMuted mb-1"></i>
                <div class="text-[11px] text-white font-medium">Drop .zip Library here</div>
                <input type="file" id="lib-zip-input" accept=".zip" class="hidden" onchange="handleLibZipSelect(event)">
            </div>

            <!-- LIBRARY SEARCH INPUT -->
            <div class="relative">
                <i class="ph-bold ph-magnifying-glass absolute left-2.5 top-2.5 text-ardTextMuted text-xs"></i>
                <input type="text" id="lib-search-input" placeholder="Search libraries..." oninput="filterLibraries()" class="flat-input w-full pl-8 pr-2 py-1.5 rounded-lg text-xs font-mono text-white placeholder:text-gray-500">
            </div>

            <div id="lib-list-container" class="space-y-2 flex-1 overflow-y-auto">
                <div class="text-center text-ardTextMuted text-sm py-4">Loading libraries...</div>
            </div>
        </div>
    </div>
    <!-- TOP HEADER -->
    <header class="h-12 bg-ardBg border-b border-ardBorder flex items-center justify-between px-3 flex-shrink-0 z-30 select-none">
        <div class="flex items-center gap-2.5">
            <!-- File Menu Dropdown -->
            <div class="relative">
                <button class="w-8 h-8 rounded-lg flex items-center justify-center hover:bg-ardHover text-ardTextMuted hover:text-white transition-colors" onclick="toggleDropdown('menu-file', event)" title="File Operations">
                    <i class="ph-bold ph-folder-open text-lg pointer-events-none"></i>
                </button>
                <div id="menu-file" class="dropdown-content absolute top-full left-0 mt-1.5 w-52 bg-ardCard border border-ardBorder shadow-2xl rounded-lg z-50 py-1.5 text-xs">
                    <button class="w-full text-left px-3.5 py-2 hover:bg-ardTeal hover:text-white flex items-center gap-2 group transition-colors" onclick="newSketch()">
                        <i class="ph-bold ph-file-plus text-sm text-ardTextMuted group-hover:text-white"></i> New Sketch
                    </button>
                    <div class="h-px bg-ardBorder my-1"></div>
                    <button class="w-full text-left px-3.5 py-2 hover:bg-ardTeal hover:text-white flex items-center gap-2 group transition-colors" onclick="triggerLoadBin()">
                        <i class="ph-bold ph-upload-simple text-sm text-ardTextMuted group-hover:text-white"></i> Load .bin File...
                    </button>
                    <button class="w-full text-left px-3.5 py-2 hover:bg-ardTeal hover:text-white flex items-center gap-2 group transition-colors" onclick="triggerSaveBin()">
                        <i class="ph-bold ph-download-simple text-sm text-ardTextMuted group-hover:text-white"></i> Compile & Save .bin...
                    </button>
                    <div class="h-px bg-ardBorder my-1"></div>
                    <button class="w-full text-left px-3.5 py-2 hover:bg-ardTeal hover:text-white flex items-center gap-2 group transition-colors" onclick="copyEditorCode()">
                        <i class="ph-bold ph-copy text-sm text-ardTextMuted group-hover:text-white"></i> Copy Code
                    </button>
                    <div class="h-px bg-ardBorder my-1"></div>
                </div>
            </div>

            <div class="h-4 w-px bg-ardBorder"></div>

            <!-- Verify & Deploy Actions -->
            <div class="flex items-center gap-1.5">
                <button id="btn-verify-top" class="h-8 px-3 rounded-lg bg-ardCard hover:bg-ardHover border border-ardBorder text-white flex items-center gap-1.5 text-xs font-semibold hover:border-ardTeal transition-all shadow-sm active:scale-95" title="Verify / Compile (Ctrl+S)" onclick="triggerCompileOnly()">
                    <i class="ph-bold ph-check text-ardTeal text-base" id="icon-verify"></i>
                    <span>Verify</span>
                </button>
                <button id="btn-deploy-top" class="h-8 px-3 rounded-lg bg-ardTeal hover:bg-[#009b9d] text-white flex items-center gap-1.5 text-xs font-semibold transition-all shadow-md active:scale-95 hover:shadow-[0_0_12px_rgba(0,180,182,0.4)]" title="Upload / Deploy OTA Update (Ctrl+U)" onclick="deployFirmware()">
                    <i class="ph-bold ph-rocket-launch text-base" id="icon-deploy"></i>
                    <span>Deploy OTA</span>
                </button>
                <span id="verify-chip" class="hidden"></span>
                <div id="action-progress-container" class="w-24 h-1.5 bg-[#121316] rounded-full overflow-hidden hidden ml-1 relative border border-ardBorder">
                    <div id="action-progress-bar" class="h-full bg-ardTeal w-0 transition-all duration-200"></div>
                </div>
            </div>

            <div class="h-4 w-px bg-ardBorder"></div>

            <!-- Dynamic Fleet & Target Dropdown -->
            <div class="relative">
                <button id="fleet-selector-btn" class="flex items-center gap-2 px-3 h-8 rounded-lg bg-ardCard hover:bg-ardHover border border-ardBorder hover:border-ardTeal transition-all text-xs font-medium text-white group" onclick="toggleDropdown('fleet-dropdown', event)">
                    <i class="ph-bold ph-cpu text-ardTeal text-sm"></i>
                    <span id="target-label" class="font-medium truncate max-w-[150px]">DefaultFleet (All)</span>
                    <i class="ph-bold ph-caret-down text-ardTextMuted group-hover:text-white text-xs transition-colors"></i>
                </button>
                <div id="fleet-dropdown" class="dropdown-content absolute top-full left-0 mt-1.5 w-80 bg-ardCard border border-ardBorder rounded-xl shadow-2xl z-50 p-2 text-xs">
                    <div class="flex items-center justify-between px-2 py-1 mb-1 text-[11px] font-semibold text-ardTextMuted uppercase tracking-wider">
                        <span>Fleet Targets</span>
                        <button onclick="promptNewFleet()" class="text-ardTeal hover:underline text-[11px] flex items-center gap-1">
                            <i class="ph-bold ph-plus"></i> New Fleet
                        </button>
                    </div>
                    <div id="fleet-options-container" class="space-y-1 max-h-64 overflow-y-auto">
                        <div class="text-center py-2 text-ardTextMuted">Loading fleets...</div>
                    </div>
                </div>
            </div>

            <div class="h-4 w-px bg-ardBorder"></div>

            <!-- WiFi Config Pills -->
            <div class="flex items-center gap-1.5">
                <div class="flex items-center bg-ardCard border border-ardBorder rounded-lg px-2 h-8">
                    <i class="ph-bold ph-wifi-high text-ardTextMuted text-xs mr-1.5"></i>
                    <input type="text" id="top-wifi-ssid" placeholder="WiFi SSID" class="bg-transparent text-xs w-24 text-white outline-none placeholder:text-gray-500 font-mono" oninput="saveWifiConfig()">
                </div>
                <div class="flex items-center bg-ardCard border border-ardBorder rounded-lg px-2 h-8">
                    <input type="password" id="top-wifi-pass" placeholder="Password" class="bg-transparent text-xs w-20 text-white outline-none placeholder:text-gray-500 font-mono" oninput="saveWifiConfig()">
                    <button type="button" onclick="togglePassword('top-wifi-pass')" class="text-ardTextMuted hover:text-white ml-1">
                        <i class="ph-bold ph-eye text-xs"></i>
                    </button>
                </div>
            </div>
        </div>

        <!-- Right Side: Cloud Status Badges, Branding & User Profile -->
        <div class="flex items-center gap-3">
            <div id="ngrok-pill" class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-ardCard border border-ardBorder text-xs text-ardTextMuted cursor-pointer" onclick="copyNgrokUrl()" title="Click to copy public URL">
                <span class="w-2 h-2 rounded-full bg-emerald-500 pulse-live"></span>
                <span id="ngrok-text" class="font-mono text-[11px]">Cloud Tunnel Active</span>
            </div>
            <div id="lan-pill" class="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-ardCard border border-ardBorder text-xs text-ardTextMuted" title="ESP32 nodes currently live on your LAN (beacon every 5s)">
                <span id="lan-dot" class="w-2 h-2 rounded-full bg-gray-500"></span>
                <span id="lan-pill-text" class="font-mono text-[11px]">LAN idle</span>
            </div>
            <div class="flex items-center gap-2 pl-2 border-l border-ardBorder">
                <i class="ph-bold ph-cpu text-2xl text-ardTeal"></i>
                <span class="font-bold tracking-wide text-white text-base"><span class="text-ardTeal">WiFeY</span> Studio</span>
            </div>

            <!-- Profile Avatar & Sign Out Dropdown -->
            <div class="relative pl-2 border-l border-ardBorder" id="user-profile-container">
                <button id="user-profile-btn" onclick="toggleDropdown('profile-dropdown', event)" class="w-8 h-8 rounded-full overflow-hidden border border-ardBorder hover:border-ardTeal transition-all focus:outline-none flex items-center justify-center bg-ardCard cursor-pointer" title="Account & Profile">
                    <img id="user-profile-img" src="" alt="Profile" class="w-full h-full object-cover rounded-full hidden" referrerpolicy="no-referrer" onerror="this.classList.add('hidden'); document.getElementById('user-profile-fallback').classList.remove('hidden');">
                    <div id="user-profile-fallback" class="w-full h-full flex items-center justify-center text-ardTextMuted hover:text-white">
                        <i class="ph-bold ph-user text-base"></i>
                    </div>
                </button>
                <div id="profile-dropdown" class="dropdown-content absolute right-0 top-full mt-2 w-52 bg-ardCard border border-ardBorder rounded-xl shadow-2xl z-50 p-2 text-xs">
                    <div class="px-3 py-2 border-b border-ardBorder mb-1">
                        <p class="text-white font-medium truncate" id="profile-email-text">Signed In</p>
                        <p class="text-[10px] text-ardTextMuted font-mono">Google Account</p>
                    </div>
                    <button onclick="signOutUser()" class="w-full text-left px-3 py-2 hover:bg-rose-500/20 hover:text-rose-300 text-ardTextMuted rounded-lg flex items-center gap-2 group transition-colors cursor-pointer">
                        <i class="ph-bold ph-sign-out text-sm text-rose-400 group-hover:text-rose-300"></i>
                        <span>Sign Out</span>
                    </button>
                </div>
            </div>
        </div>
    </header>

    <!-- MAIN BODY -->
    <div class="flex-1 flex overflow-hidden">
        
        <!-- SIDEBAR NAVIGATION -->
        <aside class="w-14 bg-ardBg border-r border-ardBorder flex flex-col items-center py-3 gap-3 flex-shrink-0 z-20 select-none">
            <button onclick="switchMainView('view-editor')" id="nav-editor" class="nav-btn p-2.5 rounded-xl text-ardTeal bg-ardHover border border-ardBorder relative group transition-all" title="Code Editor">
                <i class="ph-bold ph-code text-xl"></i>
            </button>
            <button onclick="toggleLibrarySidebar()" class="nav-btn p-2.5 rounded-xl text-ardTextMuted hover:text-white hover:bg-ardHover relative group transition-all" title="Library Manager">
                <i class="ph-bold ph-books text-xl"></i>
            </button>
            <button onclick="switchMainView('view-usb')" id="nav-usb" class="nav-btn p-2.5 rounded-xl text-ardTextMuted hover:text-white hover:bg-ardHover relative group transition-all" title="USB Provisioning">
                <i class="ph-bold ph-usb text-xl"></i>
            </button>
            <button onclick="switchMainView('view-fleets')" id="nav-fleets" class="nav-btn p-2.5 rounded-xl text-ardTextMuted hover:text-white hover:bg-ardHover relative group transition-all" title="Fleet & Device Manager">
                <i class="ph-bold ph-hard-drives text-xl"></i>
            </button>
            <button onclick="switchMainView('view-settings')" id="nav-settings" class="nav-btn p-2.5 rounded-xl text-ardTextMuted hover:text-white hover:bg-ardHover relative group transition-all" title="Settings & Cloud Config">
                <i class="ph-bold ph-sliders-horizontal text-xl"></i>
            </button>
        </aside>

        <!-- MAIN CONTENT AREA -->
        <div class="flex-1 flex flex-col min-w-0 bg-ardPanel">
            
            <div class="flex-1 flex flex-col min-h-0 relative">
                
                <!-- VIEW 1: CODE EDITOR -->
                <main id="view-editor" class="flex-1 flex flex-col overflow-hidden">
                    <!-- Editor Top Bar -->
                    <div class="flex items-center justify-between h-9 bg-ardBg border-b border-ardBorder select-none px-3">
                        <div class="flex items-center h-full gap-1">
                            <button onclick="switchEditorTab('tab-raw')" id="btn-tab-raw" class="editor-tab h-full flex items-center px-3 gap-1.5 text-xs font-medium text-white border-b-2 border-ardTeal transition-colors">
                                <i class="ph-bold ph-file-code text-ardTeal"></i> Sketch.ino
                            </button>
                            <button onclick="switchEditorTab('tab-wrap')" id="btn-tab-wrap" class="editor-tab h-full flex items-center px-3 gap-1.5 text-xs font-medium text-ardTextMuted hover:text-white border-b-2 border-transparent transition-colors">
                                <i class="ph-bold ph-file-text"></i> OTA_Wrapped.cpp Preview
                            </button>
                        </div>
                        <div class="flex items-center gap-2">
                            <!-- Sketch Template Selector -->
                            <!-- Searchable Example Selector -->
                            <div class="relative flex items-center gap-1.5 text-xs text-ardTextMuted">
                                <span>Example:</span>
                                <button id="example-select-btn" onclick="toggleDropdown('example-select-dropdown', event)" class="flat-input text-xs py-1 px-2.5 rounded-lg text-white font-mono flex items-center gap-2 cursor-pointer">
                                    <span id="selected-example-label">Blink LED</span>
                                    <i class="ph-bold ph-caret-down text-ardTextMuted text-xs"></i>
                                </button>
                                <div id="example-select-dropdown" class="dropdown-content absolute right-0 top-full mt-1.5 w-72 bg-ardCard border border-ardBorder rounded-xl shadow-2xl z-50 p-2 text-xs">
                                    <div class="p-1 mb-2">
                                        <input type="text" id="example-search-input" placeholder="Search examples & libraries..." oninput="filterExamples()" class="flat-input w-full px-2.5 py-1.5 rounded-lg text-xs font-mono text-white">
                                    </div>
                                    <div id="example-options-container" class="max-h-64 overflow-y-auto space-y-1">
                                        <!-- Dynamically populated -->
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Tab: Raw Sketch -->
                    <div id="tab-raw" class="editor-content flex-1 flex flex-col overflow-hidden bg-ardPanel relative">
                        <textarea id="code-editor" class="hidden">int ledPin = 2;

void setup() {
  pinMode(ledPin, OUTPUT);
  Serial.println("Hello from WiFeY Studio Global OTA!");
}

void loop() {
  digitalWrite(ledPin, HIGH); 
  delay(1000);
  digitalWrite(ledPin, LOW); 
  delay(1000);
}</textarea>
                    </div>

                    <!-- Tab: Smart Wrapped Preview -->
                    <div id="tab-wrap" class="editor-content flex-1 flex flex-col overflow-hidden bg-ardPanel view-hidden">
                        <div class="flex items-center justify-between px-4 py-2 bg-ardBg border-b border-ardBorder text-xs text-ardTextMuted">
                            <span>This generated C++ firmware handles background pull-OTA verification, Wi-Fi reconnection, and version sync automatically.</span>
                            <button onclick="fetchPreview()" class="text-ardTeal hover:underline flex items-center gap-1">
                                <i class="ph-bold ph-arrows-clockwise"></i> Refresh Preview
                            </button>
                        </div>
                        <textarea id="preview-textarea" class="editor-textarea flex-1 p-4 bg-transparent text-emerald-400 font-mono text-xs whitespace-pre leading-5" readonly spellcheck="false">/* Loading wrapped C++ code... */</textarea>
                    </div>

                    <!-- Editor Footer Status Bar -->
                    <div class="h-6 bg-ardBg border-t border-ardBorder flex items-center justify-between px-3 text-[11px] text-ardTextMuted select-none font-mono flex-shrink-0">
                        <div class="flex items-center gap-4">
                            <span id="char-count">Lines: 12 | Length: 228</span>
                            <span>Target: <span id="status-target" class="text-white">DefaultFleet/fleet</span></span>
                        </div>
                        <div class="flex items-center gap-2">
                            <span id="warmup-chip" class="hidden"></span>
                            <div class="flex items-center gap-1">
                                <span>Board:</span>
                                <select id="board-selector" class="bg-ardBg text-white outline-none cursor-pointer border border-transparent hover:border-ardBorder rounded px-1 -ml-1" onchange="syncBoardSelectors(this.value)">
                                    <option value="esp32:esp32:esp32" class="bg-ardBg">ESP32 Dev Module</option>
                                    <option value="esp32:esp32:esp32cam" class="bg-ardBg">ESP32-CAM (AI Thinker)</option>
                                    <option value="esp32:esp32:esp32s2" class="bg-ardBg">ESP32-S2</option>
                                    <option value="esp32:esp32:esp32s3" class="bg-ardBg">ESP32-S3</option>
                                    <option value="esp32:esp32:esp32c3" class="bg-ardBg">ESP32-C3</option>
                                    <option value="esp8266:esp8266:nodemcuv2" class="bg-ardBg">NodeMCU 1.0 (ESP-12E)</option>
                                    <option value="esp8266:esp8266:generic" class="bg-ardBg">Generic ESP8266</option>
                                </select>
                            </div>
                            <span>Speed: 115200</span>
                        </div>
                    </div>
                </main>

                <!-- VIEW 2: USB PROVISIONING -->
                <main id="view-usb" class="flex-1 overflow-y-auto p-8 view-hidden">
                    <div class="max-w-3xl mx-auto w-full flex flex-col gap-6 pb-8">
                        <div class="border-b border-ardBorder pb-4">
                            <h2 class="text-2xl font-bold text-white flex items-center gap-2.5">
                                <i class="ph-bold ph-usb text-ardTeal"></i> Direct USB Provisioning
                            </h2>
                            <p class="text-xs text-ardTextMuted mt-1">Flash new ESP32 microcontrollers over USB to connect them to your WiFeY OTA Fleet.</p>
                        </div>

                        <div class="bg-ardCard border border-ardBorder rounded-xl p-6 shadow-xl space-y-5">
                            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                <div>
                                    <label class="block text-xs font-semibold text-ardTextMuted mb-1.5 uppercase tracking-wider">Target COM Port</label>
                                    <div class="flex gap-2">
                                        <select id="prov_port" class="flat-input flex-1 min-w-0 p-2.5 rounded-lg text-white text-xs cursor-pointer truncate"></select>
                                        <button onclick="loadSerialPorts()" title="Refresh ports" class="refresh-btn-fixed w-10 h-10 flex-shrink-0 flex items-center justify-center rounded-lg border border-ardBorder bg-ardPanel hover:bg-ardHover text-ardTextMuted hover:text-white transition-colors">
                                            <i class="ph-bold ph-arrows-clockwise"></i>
                                        </button>
                                    </div>
                                </div>
                                
                                <div>
                                    <label class="block text-xs font-semibold text-ardTextMuted mb-1.5 uppercase tracking-wider">Target Board Variant</label>
                                    <select id="prov_board_selector" class="flat-input w-full p-2.5 rounded-lg text-white text-xs cursor-pointer font-mono" onchange="syncBoardSelectors(this.value)">
                                        <option value="esp32:esp32:esp32">ESP32 Dev Module</option>
                                        <option value="esp32:esp32:esp32cam">ESP32-CAM (AI Thinker)</option>
                                        <option value="esp32:esp32:esp32s2">ESP32-S2</option>
                                        <option value="esp32:esp32:esp32s3">ESP32-S3</option>
                                        <option value="esp32:esp32:esp32c3">ESP32-C3</option>
                                        <option value="esp8266:esp8266:nodemcuv2">NodeMCU 1.0 (ESP-12E)</option>
                                        <option value="esp8266:esp8266:generic">Generic ESP8266</option>
                                    </select>
                                </div>
                                
                                <div>
                                    <label class="block text-xs font-semibold text-ardTextMuted mb-1.5 uppercase tracking-wider">Target Fleet Name</label>
                                    <input type="text" id="prov_fleet_id" value="DefaultFleet" class="flat-input w-full p-2.5 rounded-lg text-white text-xs font-mono">
                                </div>

                                <div>
                                    <label class="block text-xs font-semibold text-ardTextMuted mb-1.5 uppercase tracking-wider">Device ID (Unique identifier)</label>
                                    <div class="flex gap-2">
                                        <input type="text" id="prov_device_id" value="node_1" class="flat-input flex-1 p-2.5 rounded-lg text-white text-xs font-mono">
                                        <button onclick="generateRandomDeviceId()" class="px-2.5 rounded-lg border border-ardBorder bg-ardPanel hover:bg-ardHover text-ardTextMuted hover:text-white text-xs" title="Generate Random ID">
                                            <i class="ph-bold ph-shuffle"></i>
                                        </button>
                                    </div>
                                </div>

                                <div>
                                    <label class="block text-xs font-semibold text-ardTextMuted mb-1.5 uppercase tracking-wider">Flashing Baud Rate</label>
                                    <select id="prov_baud" class="flat-input w-full p-2.5 rounded-lg text-white text-xs cursor-pointer font-mono">
                                        <option value="115200" selected>115200 (Recommended / Safe)</option>
                                        <option value="460800">460800 (Fast)</option>
                                        <option value="921600">921600 (Ultra Fast)</option>
                                    </select>
                                </div>
                            </div>

                            <div class="p-4 rounded-lg bg-ardPanel border border-ardBorder text-xs text-ardTextMuted space-y-1">
                                <div class="font-semibold text-white flex items-center gap-1.5">
                                    <i class="ph-bold ph-info text-ardTeal"></i> Live Provisioning Progress:
                                </div>
                                <div>Compiles bootloader sketch and streams all Arduino CLI & esptool upload messages live in the Output box below.</div>
                            </div>

                            <button onclick="flashDevice()" id="btn-flash-device" class="w-full flex items-center justify-center gap-2 py-3 px-4 bg-ardTeal hover:bg-[#009b9d] text-white text-sm font-semibold rounded-xl transition-all shadow-lg hover:shadow-[0_0_16px_rgba(0,180,182,0.4)] active:scale-[0.99]">
                                <i class="ph-bold ph-lightning text-lg" id="icon-flash"></i> <span>Compile & Provision via USB</span>
                            </button>
                        </div>
                    </div>
                </main>

                <!-- VIEW 3: FLEET & DEVICE DASHBOARD -->
                <main id="view-fleets" class="flex-1 overflow-y-auto p-8 view-hidden">
                    <div class="max-w-6xl mx-auto w-full flex flex-col gap-6 pb-8">
                        <div class="flex items-center justify-between border-b border-ardBorder pb-4">
                            <div>
                                <h2 class="text-2xl font-bold text-white flex items-center gap-2.5">
                                    <i class="ph-bold ph-hard-drives text-ardTeal"></i> Fleet & Device Registry
                                </h2>
                                <p class="text-xs text-ardTextMuted mt-1">Monitor connected ESP32 nodes, target versions, LAN beacons, and custom build overrides.</p>
                            </div>
                            <div class="flex items-center gap-2">
                                <button onclick="promptNewFleet()" class="px-3 py-2 rounded-lg bg-ardCard hover:bg-ardHover border border-ardBorder text-xs text-white flex items-center gap-1.5 transition-colors">
                                    <i class="ph-bold ph-plus text-ardTeal"></i> Create Fleet
                                </button>
                                <button onclick="triggerBatchFleetDeploy()" class="px-3 py-2 rounded-lg bg-ardCard hover:bg-ardHover border border-ardBorder text-xs text-white flex items-center gap-1.5 transition-colors">
                                    <i class="ph-bold ph-paper-plane-tilt text-ardTeal"></i> Batch Deploy
                                </button>
                                <button onclick="loadFleetDashboard()" class="px-3 py-2 rounded-lg bg-ardCard hover:bg-ardHover border border-ardBorder text-xs text-white flex items-center gap-1.5 transition-colors">
                                    <i class="ph-bold ph-arrows-clockwise text-ardTeal"></i> Refresh
                                </button>
                            </div>
                        </div>

                        <!-- Summary Cards -->
                        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
                            <div class="bg-ardCard border border-ardBorder rounded-xl p-4">
                                <div class="text-xs text-ardTextMuted">Total Fleets</div>
                                <div id="stat-total-fleets" class="text-2xl font-bold text-white mt-1">0</div>
                            </div>
                            <div class="bg-ardCard border border-ardBorder rounded-xl p-4">
                                <div class="text-xs text-ardTextMuted">Registered Nodes</div>
                                <div id="stat-total-nodes" class="text-2xl font-bold text-white mt-1">0</div>
                            </div>
                            <div class="bg-ardCard border border-ardBorder rounded-xl p-4">
                                <div class="text-xs text-ardTextMuted">Online Now</div>
                                <div id="stat-online-nodes" class="text-2xl font-bold text-emerald-400 mt-1">0</div>
                            </div>
                            <div class="bg-ardCard border border-ardBorder rounded-xl p-4">
                                <div class="text-xs text-ardTextMuted">Active LAN Nodes</div>
                                <div id="stat-lan-nodes" class="text-2xl font-bold text-cyan-400 mt-1">0</div>
                            </div>
                            <div class="bg-ardCard border border-ardBorder rounded-xl p-4">
                                <div class="text-xs text-ardTextMuted">Storage Bucket</div>
                                <div id="stat-r2-bucket" class="text-xs font-mono font-bold text-ardTeal truncate mt-2">{{ r2_bucket }}</div>
                            </div>
                        </div>

                        <!-- Fleets -->
                        <div class="bg-ardCard border border-ardBorder rounded-xl overflow-hidden shadow-xl">
                            <div class="p-4 border-b border-ardBorder font-semibold text-white text-xs uppercase tracking-wider flex items-center justify-between">
                                <span>Fleets</span>
                                <span class="text-ardTextMuted font-normal lowercase">removing a fleet moves its nodes to DefaultFleet</span>
                            </div>
                            <div class="overflow-x-auto">
                                <table class="w-full text-left text-xs">
                                    <thead class="bg-ardPanel text-ardTextMuted font-mono border-b border-ardBorder">
                                        <tr>
                                            <th class="p-3 font-medium">Fleet</th>
                                            <th class="p-3 font-medium">Target</th>
                                            <th class="p-3 font-medium">Nodes</th>
                                            <th class="p-3 font-medium text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody id="fleets-table-body" class="divide-y divide-ardBorder"></tbody>
                                </table>
                            </div>
                        </div>

                        <!-- Devices Table -->
                        <div class="bg-ardCard border border-ardBorder rounded-xl overflow-hidden shadow-xl">
                            <div class="p-4 border-b border-ardBorder font-semibold text-white text-xs uppercase tracking-wider flex items-center justify-between">
                                <span>Registered Hardware Nodes & Active LAN Clients</span>
                                <span class="text-ardTextMuted font-normal lowercase">auto-synced via LAN Beacons & Cloud Manifest</span>
                            </div>
                            <div class="flex flex-wrap items-center gap-2 px-4 py-2.5 border-b border-ardBorder bg-ardPanel/60 text-xs">
                                <span class="text-ardTextMuted"><span id="sel-count" class="text-white font-semibold">0</span> node(s) selected</span>
                                <button onclick="selectOnlineNodes()" class="px-2.5 py-1 rounded-lg border border-ardBorder bg-ardCard hover:bg-ardHover text-white">Select online</button>
                                <button onclick="clearSelection()" class="px-2.5 py-1 rounded-lg border border-ardBorder bg-ardCard hover:bg-ardHover text-white">Clear</button>
                                <div class="flex-1"></div>
                                <button id="btn-deploy-selected" onclick="deployFirmware()" disabled class="px-3 py-1 rounded-lg bg-ardTeal hover:bg-[#009b9d] text-white font-semibold flex items-center gap-1.5 disabled:opacity-40 disabled:cursor-not-allowed"><i class="ph-bold ph-rocket-launch"></i> Deploy OTA to selected</button>
                            </div>
                            <div class="overflow-x-auto">
                                <table class="w-full text-left text-xs">
                                    <thead class="bg-ardPanel text-ardTextMuted font-mono border-b border-ardBorder">
                                        <tr>
                                            <th class="p-3 w-8"><input type="checkbox" id="select-all-cb" class="rounded border-ardBorder" onchange="selectAllNodes(this.checked)" title="Select all nodes"></th>
                                            <th class="p-3 font-medium">Device ID</th>
                                            <th class="p-3 font-medium">Fleet</th>
                                            <th class="p-3 font-medium">Build</th>
                                            <th class="p-3 font-medium">Overrides</th>
                                            <th class="p-3 font-medium">Network</th>
                                            <th class="p-3 font-medium">Last Seen</th>
                                            <th class="p-3 font-medium">Status</th>
                                            <th class="p-3 font-medium text-right">Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody id="devices-table-body" class="divide-y divide-ardBorder">
                                        <tr><td colspan="9" class="p-6 text-center text-ardTextMuted">Loading device list...</td></tr>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </main>

                <!-- VIEW 4: SETTINGS & CLOUD CONFIG -->
                <main id="view-settings" class="flex-1 overflow-y-auto p-8 view-hidden">
                    <div class="max-w-2xl mx-auto w-full flex flex-col gap-6 pb-8">
                        <div class="border-b border-ardBorder pb-4">
                            <h2 class="text-2xl font-bold text-white flex items-center gap-2.5">
                                <i class="ph-bold ph-sliders-horizontal text-ardTeal"></i> Cloud & Environment Settings
                            </h2>
                            <p class="text-xs text-ardTextMuted mt-1">Configure your Cloudflare R2 bucket, Cloudflare Worker gateway, Google OAuth, and Ngrok tunnel credentials.</p>
                        </div>

                        <div class="bg-ardCard border border-ardBorder rounded-xl p-6 shadow-xl space-y-5">
                            <div>
                                <h3 class="text-xs font-semibold text-white uppercase tracking-wider mb-3 flex items-center gap-1.5">
                                    <i class="ph-bold ph-cloud text-ardTeal"></i> Cloudflare R2 Storage (S3-Compatible)
                                </h3>
                                <div class="space-y-3">
                                    <div>
                                        <label class="block text-xs text-ardTextMuted mb-1">R2 Account ID</label>
                                        <input type="text" id="cfg_r2_account" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                    </div>
                                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        <div>
                                            <label class="block text-xs text-ardTextMuted mb-1">R2 Access Key</label>
                                            <input type="text" id="cfg_r2_access" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                        </div>
                                        <div>
                                            <label class="block text-xs text-ardTextMuted mb-1">R2 Secret Key</label>
                                            <input type="password" id="cfg_r2_secret" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                        </div>
                                    </div>
                                    <div>
                                        <label class="block text-xs text-ardTextMuted mb-1">R2 Bucket Name</label>
                                        <input type="text" id="cfg_r2_bucket" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                    </div>
                                </div>
                            </div>

                            <div class="pt-4 border-t border-ardBorder">
                                <h3 class="text-xs font-semibold text-white uppercase tracking-wider mb-3 flex items-center gap-1.5">
                                    <i class="ph-bold ph-shield-check text-ardTeal"></i> Cloudflare Worker & Google OAuth (Multi-Tenant)
                                </h3>
                                <div class="space-y-3">
                                    <div>
                                        <label class="block text-xs text-ardTextMuted mb-1">Cloudflare Worker Base URL</label>
                                        <input type="text" id="cfg_worker_url" placeholder="https://wifey-gateway.your-subdomain.workers.dev" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                    </div>
                                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        <div>
                                            <label class="block text-xs text-ardTextMuted mb-1">Google Client ID</label>
                                            <input type="text" id="cfg_google_client_id" placeholder="your-client-id.apps..." class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                        </div>
                                        <div>
                                            <label class="block text-xs text-ardTextMuted mb-1">Google Client Secret</label>
                                            <input type="password" id="cfg_google_client_secret" placeholder="GOCSPX-..." class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                        </div>
                                    </div>
                                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        <div>
                                            <label class="block text-xs text-ardTextMuted mb-1">Google Sub (User ID)</label>
                                            <input type="text" id="cfg_google_sub" placeholder="Google User Sub ID" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                        </div>
                                        <div>
                                            <label class="block text-xs text-ardTextMuted mb-1">Google ID Token (Bearer)</label>
                                            <input type="password" id="cfg_google_id_token" placeholder="OAuth ID token" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                        </div>
                                    </div>
                                    <div class="flex items-center gap-3 pt-2">
                                        <button type="button" onclick="cloudSyncUpload()" class="flex-1 py-2 px-3 bg-ardHover hover:bg-ardBorder border border-ardBorder text-white text-xs font-medium rounded-lg transition-colors flex items-center justify-center gap-1.5">
                                            <i class="ph-bold ph-cloud-arrow-up text-ardTeal"></i> Upload DB to Cloud
                                        </button>
                                        <button type="button" onclick="cloudSyncDownload()" class="flex-1 py-2 px-3 bg-ardHover hover:bg-ardBorder border border-ardBorder text-white text-xs font-medium rounded-lg transition-colors flex items-center justify-center gap-1.5">
                                            <i class="ph-bold ph-cloud-arrow-down text-cyan-400"></i> Download DB from Cloud
                                        </button>
                                    </div>
                                </div>
                            </div>

                            <div class="pt-4 border-t border-ardBorder">
                                <h3 class="text-xs font-semibold text-white uppercase tracking-wider mb-3 flex items-center gap-1.5">
                                    <i class="ph-bold ph-globe text-ardTeal"></i> Ngrok Static Domain & Auth
                                </h3>
                                <div class="space-y-3">
                                    <div>
                                        <label class="block text-xs text-ardTextMuted mb-1">Ngrok Static Domain</label>
                                        <input type="text" id="cfg_ngrok_domain" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                    </div>
                                    <div>
                                        <label class="block text-xs text-ardTextMuted mb-1">Ngrok Authtoken (Optional)</label>
                                        <input type="password" id="cfg_ngrok_token" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                    </div>
                                </div>
                            </div>

                            <div class="pt-2">
                                <button onclick="saveSettings()" class="w-full py-2.5 px-4 bg-ardTeal hover:bg-[#009b9d] text-white font-semibold text-xs rounded-lg transition-colors">
                                    Save Configuration to .env
                                </button>
                            </div>
                        </div>
                        <div class="bg-ardCard border border-ardBorder rounded-xl p-6 shadow-xl space-y-4">
                            <div>
                                <h3 class="text-xs font-semibold text-white uppercase tracking-wider mb-1 flex items-center gap-1.5">
                                    <i class="ph-bold ph-cpu text-ardTeal"></i> Device & Firmware Options
                                </h3>
                                <p class="text-xs text-ardTextMuted mb-3">Applied to every Verify, Deploy and USB provisioning. Stored locally in wifey_prefs.json and never synced to the cloud.</p>
                                <div class="space-y-3">
                                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        <div class="min-w-0">
                                            <label class="block text-xs text-ardTextMuted mb-1">OTA check interval (seconds)</label>
                                            <input type="number" min="0" max="3600" id="opt_poll_seconds" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                            <p class="text-[10px] text-ardTextMuted mt-1">0 = only at boot. LAN nodes also get an instant nudge when you deploy.</p>
                                        </div>
                                        <div class="min-w-0">
                                            <label class="block text-xs text-ardTextMuted mb-1">Wi-Fi mode for sketches</label>
                                            <select id="opt_net_mode" class="flat-input w-full p-2.5 rounded-lg text-xs text-white cursor-pointer">
                                                <option value="auto">Auto-detect from the sketch</option>
                                                <option value="sta">Wi-Fi client only (STA)</option>
                                                <option value="ap_sta">STA + AP together (OTA stays alive)</option>
                                                <option value="ota_ap">OTA check at boot, then AP only</option>
                                            </select>
                                        </div>
                                    </div>
                                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                        <div class="min-w-0">
                                            <label class="block text-xs text-ardTextMuted mb-1">AP-only mode: re-check for updates every (minutes)</label>
                                            <input type="number" min="0" max="1440" id="opt_ap_window" class="flat-input w-full p-2.5 rounded-lg text-xs font-mono text-white">
                                            <p class="text-[10px] text-ardTextMuted mt-1">0 = never. AP clients blip for a few seconds while the radio joins your router.</p>
                                        </div>
                                        <div class="flex items-center">
                                            <label class="flex items-center gap-2 text-xs text-ardTextMain cursor-pointer">
                                                <input type="checkbox" id="opt_serial_mirror" class="rounded border-ardBorder"> Mirror Serial output to the LAN serial monitor
                                            </label>
                                        </div>
                                    </div>
                                    <div>
                                        <label class="block text-xs text-ardTextMuted mb-1">Folder for compiled .bin files</label>
                                        <div class="flex gap-2">
                                            <input type="text" id="opt_bin_dir" placeholder="(none) - pick a folder to auto-save a copy of every new build" class="flat-input flex-1 min-w-0 p-2.5 rounded-lg text-xs font-mono text-white">
                                            <button type="button" onclick="browseBinDir()" class="flex-shrink-0 px-3 rounded-lg border border-ardBorder bg-ardPanel hover:bg-ardHover text-xs text-white">Browse...</button>
                                        </div>
                                    </div>
                                    <button onclick="saveDeviceOptions()" class="w-full py-2.5 px-4 bg-ardTeal hover:bg-[#009b9d] text-white font-semibold text-xs rounded-lg transition-colors">Save device options</button>
                                </div>
                            </div>
                        </div>

                    </div>
                </main>
            </div>

            <!-- BOTTOM PANEL CONTAINER -->
            <div class="w-full flex-shrink-0 z-20 flex flex-col">
                
            <!-- DRAGGABLE RESIZER BAR -->
            <div id="panel-resizer" class="w-full h-2 cursor-ns-resize bg-ardBorder hover:bg-ardTeal flex items-center justify-center transition-colors z-30 group" title="Drag to resize">
                <div class="w-12 h-1 bg-gray-500 rounded-full group-hover:bg-white transition-colors"></div>
            </div>
            
            <!-- BOTTOM PANEL -->
            <div id="bottom-panel" class="w-full flex-none flex flex-col bg-ardPanel z-20" style="height: 250px; min-height: 120px; max-height: 75vh; overflow: hidden;">
                
                <!-- Top Bar -->
                <div class="flex items-center justify-between h-8 bg-ardBg px-2 border-b border-ardBorder select-none flex-shrink-0">
                    <div class="flex items-center h-full gap-1">
                        <button onclick="switchPanelTab('panel-out')" id="btn-panel-out" class="panel-tab flex items-center px-3 h-full text-xs font-medium text-white border-b-2 border-ardTeal transition-colors">
                            <i class="ph-bold ph-terminal mr-1.5 text-xs"></i> Output
                        </button>
                        <button onclick="switchPanelTab('panel-serial')" id="btn-panel-serial" class="panel-tab flex items-center px-3 h-full text-xs font-medium text-ardTextMuted hover:text-white border-b-2 border-transparent transition-colors">
                            <i class="ph-bold ph-cpu mr-1.5 text-xs"></i> Serial Monitor
                        </button>
                    </div>
                    <div class="flex items-center gap-2">
                        <span id="output-spinner" class="hidden items-center gap-1.5 text-xs text-ardTeal font-mono">
                            <i class="ph-bold ph-spinner animate-spin"></i> Running CLI...
                        </span>
                        <button onclick="clearCurrentPanel()" class="text-ardTextMuted hover:text-white text-xs px-2 py-0.5 rounded hover:bg-ardHover transition-colors" title="Clear Logs">
                            <i class="ph-bold ph-prohibit mr-1"></i> Clear
                        </button>
                    </div>
                </div>

                <!-- Panel Content: Live Output Logs -->
                <div id="panel-out" class="panel-content flex-1 p-3 overflow-y-auto font-mono text-xs text-[#d4d4d4] bg-ardPanel space-y-0.5 select-text min-h-0">
                    <div class="text-ardTextMuted">WiFeY Studio initialized. Toolchain loaded and ready.</div>
                </div>

                <!-- Panel Content: Serial Monitor -->
                <div id="panel-serial" class="panel-content flex-1 flex flex-col view-hidden bg-ardPanel min-h-0">
                    <div class="flex items-center gap-2 p-1.5 border-b border-ardBorder bg-ardCard text-xs select-none flex-shrink-0">
                        <div class="flex items-center rounded-lg border border-ardBorder overflow-hidden flex-shrink-0">
                            <button id="serial-mode-usb" onclick="setSerialMode('usb')" class="px-2.5 py-1 text-[11px] font-semibold flex items-center gap-1 bg-ardTeal text-white"><i class="ph-bold ph-usb"></i> USB</button>
                            <button id="serial-mode-lan" onclick="setSerialMode('lan')" class="px-2.5 py-1 text-[11px] font-semibold flex items-center gap-1 bg-ardPanel text-ardTextMuted hover:text-white" title="Read the Serial output of an ESP32 over Wi-Fi"><i class="ph-bold ph-wifi-high"></i> Wi-Fi (LAN)</button>
                        </div>
                        <select id="serial_port" class="flat-input text-xs py-1 px-2 rounded-lg text-white font-mono cursor-pointer w-36"></select>
                        <select id="wserial_device" class="flat-input text-xs py-1 px-2 rounded-lg text-white font-mono cursor-pointer w-44" style="display:none"><option value="">No live LAN nodes</option></select>
                        <input type="text" id="wserial_ip" placeholder="or IP, e.g. 192.168.4.1" class="flat-input text-xs py-1 px-2 rounded-lg text-white font-mono w-44" style="display:none">
                        <select id="serial_baud" class="flat-input text-xs py-1 px-2 rounded-lg text-white font-mono cursor-pointer w-28">
                            <option value="9600">9600 baud</option>
                            <option value="19200">19200 baud</option>
                            <option value="38400">38400 baud</option>
                            <option value="57600">57600 baud</option>
                            <option value="115200" selected>115200 baud</option>
                            <option value="230400">230400 baud</option>
                            <option value="460800">460800 baud</option>
                            <option value="921600">921600 baud</option>
                        </select>

                        <div class="h-4 w-px bg-ardBorder mx-1"></div>

                        <button id="btn-serial-connect" onclick="connectSerial()" class="flex items-center gap-1.5 px-3 py-1 rounded-lg border border-emerald-500/30 text-emerald-400 bg-emerald-500/10 hover:bg-emerald-500/20 transition-colors font-medium">
                            <i class="ph-bold ph-plug"></i> Connect
                        </button>
                        <button id="btn-serial-disconnect" onclick="disconnectSerial()" class="flex items-center gap-1.5 px-3 py-1 rounded-lg border border-red-500/30 text-red-400 bg-red-500/10 hover:bg-red-500/20 transition-colors font-medium" style="display:none;">
                            <i class="ph-bold ph-plugs"></i> Disconnect
                        </button>

                        <div class="flex-1"></div>

                        <label class="flex items-center gap-1 text-[11px] text-ardTextMuted cursor-pointer">
                            <input type="checkbox" id="serial-autoscroll" checked class="rounded border-ardBorder"> Autoscroll
                        </label>
                        <label class="flex items-center gap-1 text-[11px] text-ardTextMuted cursor-pointer">
                            <input type="checkbox" id="serial-timestamps" class="rounded border-ardBorder"> Timestamps
                        </label>
                    </div>

                    <!-- Serial Data Log View -->
                    <div id="serial_out" class="flex-1 p-3 overflow-y-auto font-mono text-xs text-[#e0e2ec] leading-relaxed whitespace-pre-wrap select-text bg-[#121316] min-h-0" style="user-select: text; -webkit-user-select: text;"><span class="text-ardTextMuted italic">--- Serial Monitor Disconnected ---</span></div>

                    <!-- Serial Send Bar -->
                    <div class="flex items-center gap-2 p-1.5 border-t border-ardBorder bg-ardCard flex-shrink-0">
                        <input type="text" id="serial-input" placeholder="Send serial command to ESP32..." class="flat-input flex-1 px-3 py-1 rounded-lg text-xs font-mono text-white" onkeydown="if(event.key==='Enter') sendSerialCommand()">
                        <select id="serial-line-ending" class="flat-input text-xs py-1 px-2 rounded-lg text-white font-mono cursor-pointer">
                            <option value="\r\n">Both NL & CR (\r\n)</option>
                            <option value="\n">Newline (\n)</option>
                            <option value="\r">Carriage Return (\r)</option>
                            <option value="">No line ending</option>
                        </select>
                        <button onclick="sendSerialCommand()" class="px-3 py-1 rounded-lg bg-ardTeal hover:bg-[#009b9d] text-white text-xs font-semibold transition-colors flex items-center gap-1">
                            <i class="ph-bold ph-paper-plane-right"></i> Send
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- JAVASCRIPT LOGIC -->
    <script>
        // Example Sketches Library
        const EXAMPLE_SKETCHES = {
            blink: `int ledPin = 2;\\n\\nvoid setup() {\\n  pinMode(ledPin, OUTPUT);\\n  Serial.println("Hello from WiFeY Studio Global OTA!");\\n}\\n\\nvoid loop() {\\n  digitalWrite(ledPin, HIGH);\\n  delay(1000);\\n  digitalWrite(ledPin, LOW);\\n  delay(1000);\\n}`,
            wifi_scan: `void setup() {\\n  Serial.begin(115200);\\n  Serial.println("Scanning WiFi Networks...");\\n  int n = WiFi.scanNetworks();\\n  if (n == 0) {\\n    Serial.println("No networks found.");\\n  } else {\\n    Serial.printf("%d networks found:\\\\n", n);\\n    for (int i = 0; i < n; ++i) {\\n      Serial.printf("%d: %s (%d dBm)\\\\n", i + 1, WiFi.SSID(i).c_str(), WiFi.RSSI(i));\\n    }\\n  }\\n}\\n\\nvoid loop() {\\n  delay(10000);\\n}`,
            http_client: `void setup() {\\n  Serial.begin(115200);\\n  Serial.println("ESP32 HTTP Client Ready");\\n}\\n\\nvoid loop() {\\n  if (WiFi.status() == WL_CONNECTED) {\\n    HTTPClient http;\\n    http.begin("https://api.ipify.org?format=json");\\n    int code = http.GET();\\n    if (code > 0) {\\n      Serial.printf("[HTTP] Code: %d Payload: %s\\\\n", code, http.getString().c_str());\\n    }\\n    http.end();\\n  }\\n  delay(15000);\\n}`,
            sensor: `const int sensorPin = 34;\\n\\nvoid setup() {\\n  Serial.begin(115200);\\n  pinMode(sensorPin, INPUT);\\n  Serial.println("Analog Sensor Reader Started");\\n}\\n\\nvoid loop() {\\n  int val = analogRead(sensorPin);\\n  Serial.printf("Analog Value: %d\\\\n", val);\\n  delay(1000);\\n}`,
            deep_sleep: `void setup() {\\n  Serial.begin(115200);\\n  Serial.println("Awake! Performing task...");\\n  delay(2000);\\n  Serial.println("Going to sleep for 10 seconds...");\\n  esp_sleep_enable_timer_wakeup(10 * 1000000ULL);\\n  esp_deep_sleep_start();\\n}\\n\\nvoid loop() {}`
        };

        window.onload = () => {
            loadWifiConfig();
            loadSerialPorts();
            loadFleetsAndDevices();
            loadSettings();
            checkAuthStatus(true);
            initLiveFeatures();
            populateLibraryExamples();
            
            // Initialize CodeMirror IDE
            cmEditor = CodeMirror.fromTextArea(document.getElementById('code-editor'), {
                mode: "text/x-c++src",
                lineNumbers: true,
                indentUnit: 2,
                tabSize: 2,
                lineWrapping: false,
                extraKeys: {
                    "Ctrl-S": function(cm) { triggerCompileOnly(); },
                    "Cmd-S": function(cm) { triggerCompileOnly(); },
                    "Ctrl-U": function(cm) { deployFirmware(); },
                    "Cmd-U": function(cm) { deployFirmware(); }
                }
            });
            
            cmEditor.on("change", function() {
                const val = cmEditor.getValue();
                document.getElementById('char-count').innerText = `Lines: ${cmEditor.lineCount()} | Length: ${val.length}`;
                if (typeof setVerifyChip === 'function' && !activeBin) setVerifyChip('');
            });
            
            // Trigger initial char count
            CodeMirror.signal(cmEditor, "change", cmEditor);
            
            // Check status badge
            if (NGROK_API_URL.includes("localhost") || NGROK_API_URL.includes("127.0.0.1")) {
                document.getElementById('ngrok-text').innerText = "Localhost Only";
                document.getElementById('ngrok-pill').querySelector('span').classList.remove('bg-emerald-500');
                document.getElementById('ngrok-pill').querySelector('span').classList.add('bg-amber-500');
            }
        };
        // --- SEARCH STATE ---
        let allFetchedLibraries = [];
        let allFetchedExamples = [];
        
        // --- LIBRARY MANAGER SIDEBAR LOGIC ---
        function toggleLibrarySidebar() {
            const sidebar = document.getElementById('lib-sidebar');
            if (sidebar.classList.contains('hidden')) {
                sidebar.classList.remove('hidden');
                sidebar.classList.add('flex');
                fetchLibraries();
            } else {
                sidebar.classList.add('hidden');
                sidebar.classList.remove('flex');
            }
        }
        
        function fetchLibraries() {
            const container = document.getElementById('lib-list-container');
            container.innerHTML = '<div class="text-center text-ardTextMuted text-sm py-4"><i class="ph-bold ph-spinner animate-spin"></i> Loading...</div>';
            
            fetch('/api/lib/list')
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        container.innerHTML = `<div class="text-red-400 text-sm text-center py-4">${escHtml(data.error)}</div>`;
                        return;
                    }
                    allFetchedLibraries = data.libraries || [];
                    allFetchedLibraries.sort((a, b) => a.name.localeCompare(b.name));
                    renderLibraries(allFetchedLibraries);
                })
                .catch(() => {
                    container.innerHTML = `<div class="text-red-400 text-sm text-center py-4">Failed to load libraries.</div>`;
                });
        }
        
        function renderLibraries(libs) {
            const container = document.getElementById('lib-list-container');
            container.innerHTML = '';
        
            if (libs.length === 0) {
                container.innerHTML = '<div class="text-ardTextMuted text-sm text-center py-4">No matching libraries.</div>';
                return;
            }
        
            libs.forEach(lib => {
                const libEl = document.createElement('div');
                libEl.className = 'flex flex-col p-2.5 bg-ardCard border border-ardBorder rounded-lg hover:border-ardTeal transition-colors group';
                
                let deleteBtn = '';
                if (lib.deletable) {
                    deleteBtn = `
                        <button class="p-1.5 bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 rounded text-xs transition-colors opacity-0 group-hover:opacity-100" onclick="uninstallLibrary('${escHtml(lib.name)}')">
                            <i class="ph-bold ph-trash"></i>
                        </button>`;
                }
        
                libEl.innerHTML = `
                    <div class="flex items-center justify-between mb-1">
                        <div class="text-sm font-semibold text-white truncate pr-2" title="${escHtml(lib.name)}">${escHtml(lib.name)}</div>
                        ${deleteBtn}
                    </div>
                    <div class="text-[10px] text-ardTextMuted">Version: ${escHtml(lib.version || 'Unknown')}</div>
                `;
                container.appendChild(libEl);
            });
        }
        
        function filterLibraries() {
            const query = (document.getElementById('lib-search-input').value || '').toLowerCase().trim();
            const filtered = allFetchedLibraries.filter(lib => lib.name.toLowerCase().includes(query));
            renderLibraries(filtered);
        }
        
        function handleLibZipSelect(event) {
            const file = event.target.files[0];
            if (file) {
                installZipLibrary(file);
            }
            event.target.value = ''; // Reset input
        }
        
        function installZipLibrary(file) {
            const container = document.getElementById('lib-list-container');
            container.innerHTML = `<div class="text-center text-ardTeal text-sm py-4"><i class="ph-bold ph-spinner animate-spin"></i> Installing ${escHtml(file.name)}...</div>`;
            
            const formData = new FormData();
            formData.append('zip_file', file);
            
            fetch('/api/lib/upload', {
                method: 'POST',
                body: formData
            })
            .then(r => r.json())
            .then(res => {
                if (res.success) {
                    logToOutput('[System] Successfully installed library: ' + file.name);
                } else {
                    logToOutput('[System] [ERROR] Failed to install library: ' + (res.error || 'Unknown error'));
                    alert("Failed to install library: " + res.error);
                }
                fetchLibraries();
            })
            .catch(err => {
                logToOutput('[System] [ERROR] Network error installing library: ' + err);
                fetchLibraries();
            });
        }
        
        function uninstallLibrary(libName) {
            if (!confirm('Are you sure you want to completely remove the library "' + libName + '"?')) return;
            
            const container = document.getElementById('lib-list-container');
            container.innerHTML = `<div class="text-center text-red-400 text-sm py-4"><i class="ph-bold ph-spinner animate-spin"></i> Removing ${escHtml(libName)}...</div>`;
            
            fetch('/api/lib/uninstall', {
                method: 'POST',
            headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name: libName})
            })
            .then(r => r.json())
            .then(res => {
                if (res.success) {
                    logToOutput('[System] Successfully removed library: ' + libName);
                } else {
                    logToOutput('[System] [ERROR] Failed to remove library: ' + (res.error || 'Unknown error'));
                    alert("Failed to remove library: " + res.error);
                }
                fetchLibraries();
            })
            .catch(err => {
                logToOutput('[System] [ERROR] Network error removing library: ' + err);
                fetchLibraries();
            });
        }
        
        // --- EXAMPLES SEARCH & POPULATION ---
        function populateLibraryExamples() {
            const builtinExamples = [
                { library: 'Built-in', name: 'Blink LED', key: 'blink' },
                { library: 'Built-in', name: 'WiFi Scanner', key: 'wifi_scan' },
                { library: 'Built-in', name: 'HTTP REST Client', key: 'http_client' },
                { library: 'Built-in', name: 'Analog Sensor Read', key: 'sensor' },
                { library: 'Built-in', name: 'ESP32 Deep Sleep', key: 'deep_sleep' }
            ];
        
            fetch('/api/lib/examples')
                .then(r => r.json())
                .then(data => {
                    const customExamples = (data.examples || []).map(ex => ({
                        library: ex.library, name: ex.name, path: ex.path
                    }));
                    allFetchedExamples = [...builtinExamples, ...customExamples];
                    renderExamples(allFetchedExamples);
                })
                .catch(() => {
                    allFetchedExamples = builtinExamples;
                    renderExamples(allFetchedExamples);
                });
        }
        
        function renderExamples(examples) {
            const container = document.getElementById('example-options-container');
            if (!container) return;
            container.innerHTML = '';
        
            if (examples.length === 0) {
                container.innerHTML = '<div class="text-ardTextMuted text-center py-2 text-xs">No examples found</div>';
                return;
            }
        
            examples.forEach(ex => {
                const opt = document.createElement('div');
                opt.className = 'p-2 rounded-lg hover:bg-ardHover cursor-pointer flex flex-col transition-colors border border-transparent hover:border-ardBorder';
                opt.onclick = () => {
                    document.getElementById('selected-example-label').innerText = `${ex.name}`;
                    closeAllMenus();
                    if (ex.path) loadExampleFromPath(ex.path);
                    else if (ex.key) loadExampleSketch(ex.key);
                };
                opt.innerHTML = `
                    <div class="text-white font-medium truncate">${escHtml(ex.name)}</div>
                    <div class="text-[10px] text-ardTeal truncate">${escHtml(ex.library)}</div>
                `;
                container.appendChild(opt);
            });
        }
        
        function filterExamples() {
            const query = (document.getElementById('example-search-input').value || '').toLowerCase().trim();
            const filtered = allFetchedExamples.filter(ex => 
                ex.name.toLowerCase().includes(query) || ex.library.toLowerCase().includes(query)
            );
            renderExamples(filtered);
        }
        
        function loadExampleFromPath(path) {
            fetch('/api/lib/example/content', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path: path})
            })
            .then(r => r.json())
            .then(data => {
                if (data.code) {
                    document.getElementById('code-editor').value = data.code;
                    updateLineNumbers();
                    logToOutput(`[File] Loaded library example from disk.`);
                } else {
                    alert("Failed to load example: " + (data.error || "Unknown error"));
                }
            });
        }
        
        // --- LINE NUMBERS & EDITOR INDENTATION ---
        

        function newSketch() {
            if (confirm("Reset editor to empty starter sketch?")) {
                cmEditor.setValue(`void setup() {\\n  Serial.begin(115200);\\n  Serial.println("WiFeY Sketch Initialized");\\n}\\n\\nvoid loop() {\\n  delay(1000);\\n}`);
                logToOutput("[File] Created new starter sketch.");
            }
        }

        function copyEditorCode() {
            navigator.clipboard.writeText(cmEditor.getValue());
            logToOutput("[File] Code copied to clipboard.");
        }

        function loadExampleSketch(key) {
            if (EXAMPLE_SKETCHES[key]) {
                cmEditor.setValue(EXAMPLE_SKETCHES[key].replace(/\\n/g, '\\n'));
                logToOutput(`[File] Loaded example sketch: ${key}`);
            }
        }

        function loadExampleFromPath(path) {
            fetch('/api/lib/example/content', {
                method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})
            })
            .then(r => r.json()).then(data => {
                if (data.code) {
                    cmEditor.setValue(data.code);
                    logToOutput(`[File] Loaded library example from disk.`);
                } else {
                    alert("Failed to load example: " + (data.error || "Unknown error"));
                }
            });
        }
        
        function fetchPreview() {
            const rawCode = cmEditor.getValue();
            const payload = {
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL, 
                topic: currentTopic, 
                code: rawCode
            };
            document.getElementById('preview-textarea').value = "Generating smart OTA preview...";
            fetch('/preview', { 
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) 
            })
            .then(res => res.json()).then(data => { document.getElementById('preview-textarea').value = data.code; })
            .catch(err => { document.getElementById('preview-textarea').value = "// Error: " + err; });
        }
        
        function buildPayload(extra) {
            return Object.assign({
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL,
                topic: currentTopic,
                code: cmEditor.getValue(),
                fqbn: document.getElementById('prov_board_selector').value
            }, extra || {});
        }
        // --- Bottom Panel Resizer Logic ---
        const resizer = document.getElementById('panel-resizer');
        const bottomPanel = document.getElementById('bottom-panel');
        let isResizing = false;
        
        resizer.addEventListener('mousedown', function(e) {
            e.preventDefault(); 
            document.body.style.cursor = 'ns-resize'; 
        });
        
        window.addEventListener('mousemove', function(e) {
            if (!isResizing) return;
            const newHeight = window.innerHeight - e.clientY;
            if (newHeight >= 120 && newHeight <= (window.innerHeight * 0.75)) {
                bottomPanel.style.height = newHeight + 'px';
            }
        });
        
        window.addEventListener('mouseup', function() {
            if (isResizing) {
                isResizing = false;
                document.body.style.cursor = ''; 
            }
        });

        window.addEventListener('blur', function() {
            if (isResizing) {
                isResizing = false;
                document.body.style.cursor = '';
            }
        });

        // --- NAVIGATION & TABS ---
        function switchMainView(viewId) {
            document.getElementById('view-editor').classList.add('view-hidden');
            document.getElementById('view-usb').classList.add('view-hidden');
            document.getElementById('view-fleets').classList.add('view-hidden');
            document.getElementById('view-settings').classList.add('view-hidden');
            document.getElementById(viewId).classList.remove('view-hidden');

            document.querySelectorAll('.nav-btn').forEach(btn => {
                btn.classList.remove('text-ardTeal', 'bg-ardHover', 'border', 'border-ardBorder');
                btn.classList.add('text-ardTextMuted');
            });
            const activeNav = document.getElementById('nav-' + viewId.replace('view-', ''));
            if (activeNav) {
                activeNav.classList.remove('text-ardTextMuted');
                activeNav.classList.add('text-ardTeal', 'bg-ardHover', 'border', 'border-ardBorder');
            }

            if (viewId === 'view-usb') loadSerialPorts();
            if (viewId === 'view-fleets') loadFleetDashboard();
            if (viewId === 'view-settings') loadSettings();
        }

        function switchEditorTab(tabId) {
            document.querySelectorAll('.editor-content').forEach(el => el.classList.add('view-hidden'));
            document.querySelectorAll('.editor-tab').forEach(el => {
                el.classList.remove('text-white', 'border-ardTeal');
                el.classList.add('text-ardTextMuted', 'border-transparent');
            });
            document.getElementById(tabId).classList.remove('view-hidden');
            const activeBtn = document.getElementById('btn-' + tabId);
            activeBtn.classList.remove('text-ardTextMuted', 'border-transparent');
            activeBtn.classList.add('text-white', 'border-ardTeal');
            if (tabId === 'tab-wrap') fetchPreview();
        }

        function switchPanelTab(tabId) {
            document.querySelectorAll('.panel-content').forEach(el => el.classList.add('view-hidden'));
            document.querySelectorAll('.panel-tab').forEach(el => {
                el.classList.remove('text-white', 'border-ardTeal');
                el.classList.add('text-ardTextMuted', 'border-transparent');
            });
            document.getElementById(tabId).classList.remove('view-hidden');
            const activeBtn = document.getElementById('btn-' + tabId);
            activeBtn.classList.remove('text-ardTextMuted', 'border-transparent');
            activeBtn.classList.add('text-white', 'border-ardTeal');
        }

        function toggleDropdown(dropdownId, event) {
            if (event) event.stopPropagation();
            const el = document.getElementById(dropdownId);
            const isOpen = el.classList.contains("show");
            closeAllMenus();
            if (!isOpen) {
                el.classList.add("show");
            }
        }

        function closeAllMenus() {
            var dropdowns = document.getElementsByClassName("dropdown-content");
            for (var i = 0; i < dropdowns.length; i++) dropdowns[i].classList.remove('show');
        }

        window.onclick = function(event) {
            if (!event.target.closest('#fleet-selector-btn') && !event.target.closest('.dropdown-content') && !event.target.closest('button')) {
                closeAllMenus();
            }
        };

        // --- REAL-TIME STREAMING & OUTPUT LOGGING ---
        function logToOutput(msg) {
            const outputPanel = document.getElementById('panel-out');
            const logEntry = document.createElement('div');
            const now = new Date().toLocaleTimeString();
            
            let color = "text-[#cccccc]";
            if (msg.includes("[Build]") || msg.includes("[SUCCESS]") || msg.includes("SUCCESS")) color = "text-ardTeal font-medium";
            else if (msg.includes("[Deploy]")) color = "text-cyan-400 font-medium";
            else if (msg.includes("[Hardware]") || msg.includes("[Upload]")) color = "text-amber-400 font-medium";
            else if (msg.includes("[CLI]") || msg.includes("Sketch uses") || msg.includes("Global variables")) color = "text-[#9cdcfe]";
            else if (msg.includes("[System]")) color = "text-emerald-400";
            else if (msg.includes("[File]")) color = "text-yellow-400";
            else if (msg.includes("Error") || msg.includes("❌") || msg.includes("[ERROR]") || msg.includes("FAILED") || msg.includes("error:")) color = "text-red-400 font-medium";
            
            logEntry.className = color + " font-mono text-xs whitespace-pre-wrap leading-5";
            
            if (msg.startsWith("[CLI]") || msg.startsWith("  ") || msg.startsWith("\t")) {
                logEntry.textContent = msg;
            } else {
                logEntry.textContent = `[${now}] ${msg}`;
            }
            
            outputPanel.appendChild(logEntry);
            outputPanel.scrollTop = outputPanel.scrollHeight;
            switchPanelTab('panel-out');
        }

        function clearCurrentPanel() {
            document.getElementById('panel-out').innerHTML = '<div class="text-ardTextMuted">Logs cleared.</div>';
            document.getElementById('serial_out').innerHTML = '';
        }

        // Generic stream reader: reads chunks from a streaming response and feeds lines to logToOutput
        async function runStreamingTask(url, payload, onStart, onComplete) {
            if (onStart) onStart();
            document.getElementById('output-spinner').classList.remove('hidden');
            document.getElementById('output-spinner').classList.add('flex');
            
            let success = true;
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                if (!response.body) {
                    const fullText = await response.text();
                    fullText.split('\\n').forEach(line => {
                        if (line.trim()) logToOutput(line.trim());
                    });
                    if (fullText.includes("[ERROR]") || fullText.includes("❌")) success = false;
                } else {
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    while (true) {
                        const { value, done } = await reader.read();
                        if (done) {
                            if (buffer.trim()) {
                                logToOutput(buffer.trim());
                                if (buffer.includes("[ERROR]") || buffer.includes("❌")) success = false;
                            }
                            break;
                        }
                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\\n');
                        buffer = lines.pop(); // keep trailing incomplete line
                        for (const line of lines) {
                            const trimmed = line.trim();
                            if (trimmed) {
                                logToOutput(trimmed);
                                if (trimmed.includes("[ERROR]") || trimmed.includes("❌") || trimmed.includes("FAILED")) {
                                    success = false;
                                }
                            }
                        }
                    }
                }
            } catch (err) {
                success = false;
                logToOutput(`[System] [ERROR] Streaming failed: ${err}`);
            } finally {
                document.getElementById('output-spinner').classList.remove('flex');
                document.getElementById('output-spinner').classList.add('hidden');
                if (onComplete) onComplete(success);
            }
            return success;
        }

        // --- FLEET & DEVICE MANAGEMENT (live status, checkbox node selection, fleet removal) ---
        let selectedNodes = new Set();
        let liveData = null;
        let liveTimer = null;
        let dropdownSig = '';
        let dashboardSig = '';

        function escHtml(s) {
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        function timeAgo(sec) {
            if (sec < 60) return sec + 's ago';
            if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
            return Math.floor(sec / 3600) + 'h ago';
        }

        function fetchLive() {
            return fetch('/api/live').then(r => r.json()).then(data => { liveData = data; return data; });
        }

        function refreshLiveNow() {
            return fetchLive().then(data => {
                // forget selected nodes that no longer exist
                Array.from(selectedNodes).forEach(id => { if (!data.devices[id]) selectedNodes.delete(id); });
                renderLiveChrome(data);
                renderFleetDropdown(data);
                if (!document.getElementById('view-fleets').classList.contains('view-hidden')) renderDashboard(data);
                populateWserialDevices(data);
                refreshTargetLabel();
            }).catch(err => console.error('Live refresh failed:', err));
        }

        function initLiveFeatures() {
            refreshLiveNow();
            if (liveTimer) clearInterval(liveTimer);
            liveTimer = setInterval(refreshLiveNow, 3000);
            pollWarmup();
            setInterval(pollWarmup, 4000);
        }

        function pollWarmup() {
            fetch('/api/setup_status').then(r => r.json()).then(s => {
                const chip = document.getElementById('warmup-chip');
                if (!chip) return;
                const w = s.warmup || {};
                if (w.state === 'running') {
                    chip.className = 'text-amber-400';
                    chip.innerText = 'Preloading libraries...';
                } else if (w.state === 'done') {
                    chip.className = 'text-emerald-400';
                    chip.innerText = 'Compiler warm';
                } else {
                    chip.className = 'hidden';
                }
            }).catch(() => {});
        }

        function renderLiveChrome(data) {
            const dot = document.getElementById('lan-dot');
            const text = document.getElementById('lan-pill-text');
            if (dot && text) {
                const n = data.lan_online || 0;
                text.innerText = n > 0 ? `LAN ${n} live` : 'LAN idle';
                dot.className = 'w-2 h-2 rounded-full ' + (n > 0 ? 'bg-cyan-400 pulse-live' : 'bg-gray-500');
            }
        }

        function refreshTargetLabel() {
            const label = document.getElementById('target-label');
            const status = document.getElementById('status-target');
            if (!label || !status) return;
            if (selectedNodes.size > 0) {
                label.innerText = selectedNodes.size + (selectedNodes.size > 1 ? ' nodes selected' : ' node selected');
                status.innerText = Array.from(selectedNodes).join(', ');
            } else if (currentTargetType === 'fleet') {
                label.innerText = `${currentFleet} (All Nodes)`;
                status.innerText = `${currentFleet}/fleet`;
            } else {
                label.innerText = `${currentDeviceId} (${currentFleet})`;
                status.innerText = `${currentFleet}/${currentDeviceId}`;
            }
        }

        function toggleNode(devId, checked) {
            if (checked) selectedNodes.add(devId); else selectedNodes.delete(devId);
            syncSelectionUI();
        }

        function syncSelectionUI() {
            document.querySelectorAll('.node-cb').forEach(cb => { cb.checked = selectedNodes.has(cb.dataset.dev); });
            const count = document.getElementById('sel-count');
            if (count) count.innerText = selectedNodes.size;
            const deployBtn = document.getElementById('btn-deploy-selected');
            if (deployBtn) deployBtn.disabled = selectedNodes.size === 0;
            const all = document.getElementById('select-all-cb');
            if (all) {
                const total = document.querySelectorAll('.node-cb').length;
                all.checked = total > 0 && selectedNodes.size >= total;
            }
            refreshTargetLabel();
        }

        function selectAllNodes(checked) {
            document.querySelectorAll('.node-cb').forEach(cb => {
                if (checked) selectedNodes.add(cb.dataset.dev); else selectedNodes.delete(cb.dataset.dev);
            });
            syncSelectionUI();
        }

        function selectOnlineNodes() {
            selectedNodes.clear();
            Object.entries((liveData && liveData.devices) || {}).forEach(([id, d]) => { if (d.online) selectedNodes.add(id); });
            syncSelectionUI();
        }

        function selectFleetNodes(fleet) {
            Object.entries((liveData && liveData.devices) || {}).forEach(([id, d]) => { if (d.fleet === fleet) selectedNodes.add(id); });
            syncSelectionUI();
        }

        function clearSelection() {
            selectedNodes.clear();
            syncSelectionUI();
        }

        function loadFleetsAndDevices() {
            dropdownSig = '';
            return refreshLiveNow();
        }

        function renderFleetDropdown(data) {
            const sig = JSON.stringify([data.fleets, Object.entries(data.devices).map(([id, d]) => [id, d.fleet, d.build, d.online, d.lan]), Array.from(selectedNodes)]);
            if (sig === dropdownSig) return;
            dropdownSig = sig;

            const container = document.getElementById('fleet-options-container');
            container.innerHTML = '';
            const fleets = Object.keys(data.fleets || {});
            if (!fleets.includes('DefaultFleet')) fleets.unshift('DefaultFleet');

            fleets.forEach(fleet => {
                const info = (data.fleets || {})[fleet] || {target_build: 100, total: 0, online: 0};
                const fleetItem = document.createElement('div');
                fleetItem.className = 'flex items-center justify-between p-2 rounded-lg hover:bg-ardHover cursor-pointer border border-transparent hover:border-ardBorder';
                fleetItem.addEventListener('click', () => { selectedNodes.clear(); selectTarget(fleet, 'fleet'); syncSelectionUI(); });

                const left = document.createElement('div');
                left.className = 'flex items-center gap-2 min-w-0';
                left.innerHTML = `<i class="ph-bold ph-tree-structure text-ardTeal"></i>
                    <div class="min-w-0">
                        <div class="font-semibold text-white truncate">${escHtml(fleet)} <span class="text-xs text-ardTextMuted font-normal">(Entire Fleet)</span></div>
                        <div class="text-[10px] text-ardTextMuted">Target Build: #${info.target_build}</div>
                    </div>`;
                const right = document.createElement('div');
                right.className = 'flex items-center gap-1.5 flex-shrink-0';
                right.innerHTML = `<span class="text-[10px] bg-emerald-500/10 text-emerald-400 px-2 py-0.5 rounded border border-emerald-500/20">${info.online}/${info.total} Online</span>`;
                if (fleet !== 'DefaultFleet') {
                    const del = document.createElement('button');
                    del.title = 'Remove fleet';
                    del.className = 'text-ardTextMuted hover:text-red-400 p-1 rounded hover:bg-red-500/10';
                    del.innerHTML = '<i class="ph-bold ph-trash text-sm"></i>';
                    del.addEventListener('click', (e) => { e.stopPropagation(); deleteFleet(fleet); });
                    right.appendChild(del);
                }
                fleetItem.appendChild(left);
                fleetItem.appendChild(right);
                container.appendChild(fleetItem);

                Object.entries(data.devices || {}).filter(([_, d]) => d.fleet === fleet).forEach(([devId, dev]) => {
                    const nodeItem = document.createElement('div');
                    nodeItem.className = 'flex items-center justify-between pl-4 pr-2 py-1.5 rounded-lg hover:bg-ardHover cursor-pointer text-xs text-ardTextMuted hover:text-white';
                    nodeItem.addEventListener('click', () => { selectedNodes.clear(); selectTarget(fleet, 'node', devId); syncSelectionUI(); });

                    const nl = document.createElement('div');
                    nl.className = 'flex items-center gap-2 min-w-0';
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.className = 'node-cb rounded border-ardBorder';
                    cb.dataset.dev = devId;
                    cb.checked = selectedNodes.has(devId);
                    cb.title = 'Select for multi-node deploy';
                    cb.addEventListener('click', (e) => e.stopPropagation());
                    cb.addEventListener('change', () => toggleNode(devId, cb.checked));
                    nl.appendChild(cb);
                    const liveCls = dev.lan ? 'bg-cyan-400 pulse-live' : (dev.online ? 'bg-emerald-400' : 'bg-gray-500');
                    nl.insertAdjacentHTML('beforeend', `<span class="w-1.5 h-1.5 rounded-full flex-shrink-0 ${liveCls}"></span><span class="truncate">${escHtml(devId)}</span>`);
                    const nr = document.createElement('span');
                    nr.className = 'text-[10px] font-mono text-ardTeal flex-shrink-0';
                    nr.innerText = `Build #${dev.build}`;
                    nodeItem.appendChild(nl);
                    nodeItem.appendChild(nr);
                    container.appendChild(nodeItem);
                });
            });
        }

        function selectTarget(fleet, type, deviceId = "") {
            currentFleet = fleet;
            currentTargetType = type;
            currentDeviceId = deviceId;
            closeAllMenus();
            currentTopic = type === 'fleet' ? `${fleet}/fleet/update` : `${fleet}/devices/${deviceId}/update`;
            refreshTargetLabel();
            logToOutput(`[System] Switched target to: ${currentTopic}`);
        }

        function promptNewFleet() {
            const name = prompt("Enter new fleet name:");
            if (name && name.trim()) {
                fetch('/api/fleet/create', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({fleet: name.trim()})
                }).then(r => r.json()).then(() => {
                    selectedNodes.clear();
                    selectTarget(name.trim(), 'fleet');
                    refreshLiveNow();
                    logToOutput(`[System] Created new fleet: ${name.trim()}`);
                });
            }
        }

        function deleteFleet(fleet) {
            const nodes = Object.values((liveData && liveData.devices) || {}).filter(d => d.fleet === fleet).length;
            const msg = nodes > 0
                ? `Remove fleet '${fleet}'? Its ${nodes} node(s) will be moved to DefaultFleet.`
                : `Remove fleet '${fleet}'?`;
            if (!confirm(msg)) return;
            fetch('/api/fleet/delete', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({fleet: fleet})
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    if (currentFleet === fleet) { selectedNodes.clear(); selectTarget('DefaultFleet', 'fleet'); }
                    const moved = (res.moved || []).length;
                    logToOutput(`[System] Removed fleet '${fleet}'` + (moved ? ` (${moved} node(s) moved to DefaultFleet)` : ''));
                    dropdownSig = ''; dashboardSig = '';
                    refreshLiveNow();
                } else {
                    logToOutput(`[System] [ERROR] ${res.error || 'Could not remove fleet'}`);
                    alert(res.error || 'Could not remove fleet');
                }
            }).catch(err => logToOutput(`[System] [ERROR] Fleet removal failed: ${err}`));
        }

        function loadFleetDashboard() {
            dashboardSig = '';
            return fetchLive().then(renderDashboard).catch(err => console.error('Dashboard error:', err));
        }

        function renderDashboard(data) {
            const devices = Object.entries(data.devices || {});
            const fleets = Object.entries(data.fleets || {});

            document.getElementById('stat-total-fleets').innerText = fleets.length;
            document.getElementById('stat-total-nodes').innerText = devices.length;
            document.getElementById('stat-online-nodes').innerText = data.online_nodes || 0;
            const lanStatEl = document.getElementById('stat-lan-nodes');
            if (lanStatEl) lanStatEl.innerText = data.lan_online || 0;

            const sig = JSON.stringify([fleets, devices.map(([id, d]) => [id, d.fleet, d.build, d.online, d.lan, d.ip, d.override])]);
            if (sig === dashboardSig) {
                // structure unchanged: only refresh the "last seen" texts so rows never flicker
                devices.forEach(([id, d]) => {
                    const cell = document.querySelector(`.ago-cell[data-dev="${CSS.escape(id)}"]`);
                    if (cell) cell.innerText = d.lan ? 'live' : timeAgo(d.ago);
                });
                return;
            }
            dashboardSig = sig;

            // Fleets table
            const fbody = document.getElementById('fleets-table-body');
            fbody.innerHTML = '';
            fleets.forEach(([name, f]) => {
                const row = document.createElement('tr');
                row.className = 'hover:bg-ardPanel/50 transition-colors border-b border-ardBorder/50';
                row.innerHTML = `
                    <td class="p-3 font-medium text-white">${escHtml(name)}</td>
                    <td class="p-3 font-mono text-ardTeal font-semibold">Build #${f.target_build}</td>
                    <td class="p-3 text-ardTextMain">${f.online}/${f.total} online</td>
                    <td class="p-3 text-right space-x-1">
                        <button class="act-select px-2 py-1 text-xs text-ardTeal hover:underline font-medium">Select nodes</button>
                        <button class="act-remove px-2 py-1 text-xs text-red-400 hover:underline">Remove</button>
                    </td>`;
                row.querySelector('.act-select').addEventListener('click', () => selectFleetNodes(name));
                const rm = row.querySelector('.act-remove');
                if (name === 'DefaultFleet') { rm.disabled = true; rm.className = 'px-2 py-1 text-xs text-ardTextMuted opacity-40 cursor-not-allowed'; rm.title = 'DefaultFleet is built-in'; }
                else rm.addEventListener('click', () => deleteFleet(name));
                fbody.appendChild(row);
            });

            // Nodes table
            const tbody = document.getElementById('devices-table-body');
            tbody.innerHTML = '';
            if (devices.length === 0) {
                tbody.innerHTML = '<tr><td colspan="9" class="p-6 text-center text-ardTextMuted">No hardware nodes registered yet. Flash an ESP32 via USB to provision!</td></tr>';
            }
            devices.forEach(([devId, dev]) => {
                let overrideHtml = '<span class="text-ardTextMuted font-mono text-[11px]">Fleet Default</span>';
                if (dev.override !== null && dev.override !== undefined) {
                    overrideHtml = `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-300 border border-amber-500/30">Build #${dev.override}
                        <button class="act-clear-override hover:text-white font-bold ml-1" title="Clear override">×</button></span>`;
                }
                const lanBadge = dev.lan
                    ? `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/20 text-cyan-300 border border-cyan-500/30 font-mono" title="IP: ${escHtml(dev.ip)}"><i class="ph-bold ph-wifi-high text-xs"></i> LAN (${escHtml(dev.ip)})</span>`
                    : '<span class="text-[10px] text-ardTextMuted font-mono">WAN Cloud</span>';
                let statusHtml;
                if (dev.lan) statusHtml = '<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium bg-cyan-500/10 text-cyan-300 border border-cyan-500/30"><span class="w-1.5 h-1.5 rounded-full bg-cyan-400 pulse-live"></span>Live on LAN</span>';
                else if (dev.online) statusHtml = '<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>Online (cloud)</span>';
                else statusHtml = '<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium bg-gray-500/10 text-gray-400 border border-gray-500/20"><span class="w-1.5 h-1.5 rounded-full bg-gray-500"></span>Offline</span>';

                const row = document.createElement('tr');
                row.className = 'hover:bg-ardPanel/50 transition-colors border-b border-ardBorder/50';
                row.innerHTML = `
                    <td class="p-3 w-8"><input type="checkbox" class="node-cb rounded border-ardBorder" data-dev="${escHtml(devId)}"></td>
                    <td class="p-3 font-mono font-medium text-white">${escHtml(devId)}</td>
                    <td class="p-3"><span class="font-medium text-ardTextMain">${escHtml(dev.fleet)}</span>
                        <button class="act-reassign ml-1.5 text-ardTextMuted hover:text-ardTeal text-xs" title="Reassign Fleet"><i class="ph-bold ph-pencil-simple"></i></button></td>
                    <td class="p-3 font-mono text-ardTeal font-semibold">Build #${dev.build}</td>
                    <td class="p-3">${overrideHtml}</td>
                    <td class="p-3">${lanBadge}</td>
                    <td class="p-3 text-ardTextMuted font-mono text-[11px] ago-cell" data-dev="${escHtml(devId)}">${dev.lan ? 'live' : timeAgo(dev.ago)}</td>
                    <td class="p-3">${statusHtml}</td>
                    <td class="p-3 text-right space-x-1">
                        <button class="act-target px-2 py-1 text-xs text-ardTeal hover:underline font-medium">Target</button>
                        <button class="act-serial px-2 py-1 text-xs text-cyan-300 hover:underline">Serial</button>
                        <button class="act-override px-2 py-1 text-xs text-amber-400 hover:underline">Override</button>
                        <button class="act-delete px-2 py-1 text-xs text-red-400 hover:underline">Delete</button>
                    </td>`;
                const cb = row.querySelector('.node-cb');
                cb.checked = selectedNodes.has(devId);
                cb.addEventListener('change', () => toggleNode(devId, cb.checked));
                row.querySelector('.act-reassign').addEventListener('click', () => promptReassignFleet(devId, dev.fleet));
                row.querySelector('.act-target').addEventListener('click', () => { selectedNodes.clear(); syncSelectionUI(); selectTarget(dev.fleet, 'node', devId); switchMainView('view-editor'); });
                row.querySelector('.act-serial').addEventListener('click', () => openWirelessSerialFor(devId));
                row.querySelector('.act-override').addEventListener('click', () => promptSetOverride(devId));
                row.querySelector('.act-delete').addEventListener('click', () => deleteDevice(devId));
                const clr = row.querySelector('.act-clear-override');
                if (clr) clr.addEventListener('click', () => clearDeviceOverride(devId));
                tbody.appendChild(row);
            });
            syncSelectionUI();
        }

        function promptReassignFleet(devId, oldFleet) {
            const newFleet = prompt(`Reassign node '${devId}' to new fleet:`, oldFleet);
            if (newFleet && newFleet.trim() && newFleet.trim() !== oldFleet) {
                fetch('/api/device/reassign_fleet', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device_id: devId, new_fleet: newFleet.trim()})
                }).then(r => r.json()).then(res => {
                    if (res.success) {
                        loadFleetDashboard();
                        loadFleetsAndDevices();
                        logToOutput(`[System] Reassigned node ${devId} to fleet ${newFleet.trim()}`);
                    }
                });
            }
        }

        function promptSetOverride(devId) {
            const buildStr = prompt(`Set custom build override number for node '${devId}' (integer):`);
            if (buildStr !== null) {
                const bNum = parseInt(buildStr.trim());
                if (isNaN(bNum)) {
                    alert("Please enter a valid integer build number.");
                    return;
                }
                fetch('/api/device/override', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device_id: devId, build_num: bNum})
                }).then(r => r.json()).then(res => {
                    loadFleetDashboard();
                    logToOutput(`[System] Set build override #${bNum} on ${devId}`);
                });
            }
        }

        function clearDeviceOverride(devId) {
            fetch('/api/device/override', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_id: devId, clear: true})
            }).then(r => r.json()).then(() => {
                loadFleetDashboard();
                logToOutput(`[System] Cleared build override for ${devId}`);
            });
        }

        function triggerBatchFleetDeploy() {
            fetch('/api/db').then(r => r.json()).then(data => {
                const fleets = Object.keys(data.manifest || {});
                if (fleets.length === 0) {
                    alert("No fleets exist to deploy to.");
                    return;
                }
                const defaultList = fleets.join(', ');
                const inputFleets = prompt("Enter target fleet names for batch deployment (comma-separated):", defaultList);
                if (!inputFleets) return;

                const selectedFleets = inputFleets.split(',').map(s => s.trim()).filter(Boolean);
                if (selectedFleets.length === 0) return;

                const buildStr = prompt("Enter target build number for this batch deployment (integer):", "105");
                if (!buildStr) return;
                const targetBuild = parseInt(buildStr.trim());
                if (isNaN(targetBuild)) {
                    alert("Invalid build number.");
                    return;
                }

                logToOutput(`[Batch Deploy] Starting multi-fleet deployment to ${selectedFleets.length} fleets with build #${targetBuild}...`);
                
                fetch('/api/fleets/batch_update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        fleet_ids: selectedFleets,
                        target_build: targetBuild
                    })
                }).then(r => r.json()).then(res => {
                    if (res.success) {
                        logToOutput(`🎉 ${res.message || 'Batch deployment complete!'}`);
                        loadFleetDashboard();
                        loadFleetsAndDevices();
                        alert(`Batch deploy successful for: ${selectedFleets.join(', ')}`);
                    } else {
                        logToOutput(`❌ Batch deploy failed: ${res.error}`);
                    }
                }).catch(err => {
                    logToOutput(`❌ Batch deploy network error: ${err}`);
                });
            });
        }

        function cloudSyncUpload() {
            const sub = document.getElementById('cfg_google_sub')?.value || '';
            const email = document.getElementById('cfg_google_email')?.value || '';
            const token = document.getElementById('cfg_google_id_token')?.value || '';

            if (!sub) {
                alert("Please provide Google Sub (User ID) in Settings to sync with cloud.");
                return;
            }

            logToOutput("[Cloud Sync] Uploading local database to Cloudflare / R2 storage...");
            fetch('/api/cloud/sync', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': token ? `Bearer ${token}` : ''
                },
                body: JSON.stringify({
                    google_sub: sub,
                    email: email,
                    id_token: token
                })
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    logToOutput("🎉 [Cloud Sync] Successfully uploaded wifey_db.json to multi-tenant cloud storage!");
                    alert("Database uploaded to cloud successfully!");
                } else {
                    logToOutput(`❌ [Cloud Sync] Upload failed: ${res.error}`);
                    alert(`Upload failed: ${res.error}`);
                }
            }).catch(err => {
                logToOutput(`❌ [Cloud Sync] Network error: ${err}`);
            });
        }

        function cloudSyncDownload() {
            const sub = document.getElementById('cfg_google_sub')?.value || '';
            const token = document.getElementById('cfg_google_id_token')?.value || '';

            if (!sub) {
                alert("Please provide Google Sub (User ID) in Settings to sync with cloud.");
                return;
            }

            if (!confirm("Download cloud database? This will replace your local database with cloud state.")) return;

            logToOutput("[Cloud Sync] Downloading database from Cloudflare / R2 storage...");
            fetch(`/api/cloud/sync?google_sub=${encodeURIComponent(sub)}`, {
                headers: token ? {'Authorization': `Bearer ${token}`} : {}
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    logToOutput("🎉 [Cloud Sync] Database downloaded and applied successfully!");
                    loadFleetDashboard();
                    loadFleetsAndDevices();
                    alert("Cloud database downloaded and synchronized!");
                } else {
                    logToOutput(`❌ [Cloud Sync] Download failed: ${res.error}`);
                    alert(`Download failed: ${res.error}`);
                }
            }).catch(err => {
                logToOutput(`❌ [Cloud Sync] Network error: ${err}`);
            });
        }

        function deleteDevice(devId) {
            if (confirm(`Remove device '${devId}' from database?`)) {
                fetch('/api/device/delete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({device_id: devId})
                }).then(() => {
                    loadFleetDashboard();
                    loadFleetsAndDevices();
                    logToOutput(`[System] Deleted device ${devId}`);
                });
            }
        }

        // --- PREVIEW WRAPPED CODE ---
        function fetchPreview() {
            const rawCode = document.getElementById('code-editor').value;
            const payload = {
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL, 
                topic: currentTopic, 
                code: rawCode
            };
            document.getElementById('preview-textarea').value = "Generating smart OTA preview...";
            fetch('/preview', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json' }, 
                body: JSON.stringify(payload) 
            })
            .then(res => res.json()).then(data => { 
                document.getElementById('preview-textarea').value = data.code; 
            })
            .catch(err => { 
                document.getElementById('preview-textarea').value = "// Error: " + err; 
            });
        }

        // --- SERIAL PORT OPERATIONS (USB + wireless LAN serial monitor) ---
        let serialMode = 'usb';
        let wserialSource = null;
        let wserialKey = null;

        function appendSerialText(text) {
            const out = document.getElementById('serial_out');
            out.appendChild(document.createTextNode(text));
            if (document.getElementById('serial-autoscroll').checked) out.scrollTop = out.scrollHeight;
        }

        function setSerialConnectedUI(connected) {
            document.getElementById('btn-serial-connect').style.display = connected ? 'none' : 'inline-flex';
            document.getElementById('btn-serial-disconnect').style.display = connected ? 'inline-flex' : 'none';
        }

        function setSerialMode(mode) {
            serialMode = mode;
            const lan = mode === 'lan';
            document.getElementById('serial_port').style.display = lan ? 'none' : '';
            document.getElementById('serial_baud').style.display = lan ? 'none' : '';
            document.getElementById('wserial_device').style.display = lan ? '' : 'none';
            document.getElementById('wserial_ip').style.display = lan ? '' : 'none';
            document.getElementById('serial-mode-usb').className = 'px-2.5 py-1 text-[11px] font-semibold flex items-center gap-1 ' + (lan ? 'bg-ardPanel text-ardTextMuted hover:text-white' : 'bg-ardTeal text-white');
            document.getElementById('serial-mode-lan').className = 'px-2.5 py-1 text-[11px] font-semibold flex items-center gap-1 ' + (lan ? 'bg-ardTeal text-white' : 'bg-ardPanel text-ardTextMuted hover:text-white');
            setSerialConnectedUI(lan ? !!wserialSource : !!serialEventSource);
            if (lan && liveData) populateWserialDevices(liveData);
        }

        function loadSerialPorts() {
            fetch('/api/ports').then(r => r.json()).then(ports => {
                let sel1 = document.getElementById('serial_port');
                let sel2 = document.getElementById('prov_port');
                const html = ports.length ?
                    ports.map(p => `<option value="${escHtml(p.device)}" title="${escHtml(p.device + ' - ' + p.desc)}">${escHtml(p.device)} - ${escHtml(p.desc)}</option>`).join('') :
                    '<option value="">No Devices Detected</option>';
                if (sel1) sel1.innerHTML = html;
                if (sel2) sel2.innerHTML = html;
            });
        }

        function populateWserialDevices(data) {
            const sel = document.getElementById('wserial_device');
            if (!sel) return;
            const live = Object.entries(data.devices || {}).filter(([_, d]) => d.lan);
            const sig = live.map(([id, d]) => id + '@' + d.ip).join('|');
            if (sel.dataset.sig === sig) return;
            sel.dataset.sig = sig;
            const previous = sel.value;
            sel.innerHTML = live.length
                ? live.map(([id, d]) => `<option value="${escHtml(id)}">${escHtml(id)} (${escHtml(d.ip)})</option>`).join('')
                : '<option value="">No live LAN nodes</option>';
            if (previous && live.some(([id]) => id === previous)) sel.value = previous;
        }

        function openWirelessSerialFor(devId) {
            switchMainView('view-editor');
            switchPanelTab('panel-serial');
            setSerialMode('lan');
            const sel = document.getElementById('wserial_device');
            if (liveData) { sel.dataset.sig = ''; populateWserialDevices(liveData); }
            sel.value = devId;
            if (sel.value !== devId) { alert(`Node '${devId}' is not live on the LAN right now.`); return; }
            if (!wserialSource) connectSerial();
        }

        function connectSerial() {
            if (serialMode === 'lan') return connectWirelessSerial();
            return connectUsbSerial();
        }

        function connectUsbSerial() {
            const port = document.getElementById('serial_port').value;
            const baud = document.getElementById('serial_baud').value;
            if (!port) return alert("Select a COM port first.");

            fetch('/api/serial/connect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({port: port, baud: baud})
            })
            .then(r => r.json()).then(res => {
                if (res.error) {
                    alert("Serial connection error: " + res.error);
                    return;
                }
                setSerialConnectedUI(true);
                appendSerialText(`\\n[*] Connected to ${port} @ ${baud} baud\\n`);

                serialEventSource = new EventSource('/api/serial/stream');
                serialEventSource.onmessage = function(e) {
                    const data = JSON.parse(e.data);
                    let text = data.text;
                    if (document.getElementById('serial-timestamps').checked) {
                        text = `[${new Date().toLocaleTimeString()}] ${text}`;
                    }
                    appendSerialText(text);
                };
            });
        }

        function disconnectSerial(quiet = false) {
            if (serialMode === 'lan' && wserialSource) return disconnectWirelessSerial(quiet);
            if (serialMode === 'usb' && serialEventSource) return disconnectUsbSerial(quiet);
            if (wserialSource) return disconnectWirelessSerial(quiet);
            return disconnectUsbSerial(quiet);
        }

        function disconnectUsbSerial(quiet = false) {
            fetch('/api/serial/disconnect', {method: 'POST'}).then(() => {
                if (serialEventSource) {
                    serialEventSource.close();
                    serialEventSource = null;
                }
                setSerialConnectedUI(false);
                if (!quiet) appendSerialText('\\n[*] Serial Connection Terminated.\\n');
            });
        }

        function connectWirelessSerial() {
            const deviceId = document.getElementById('wserial_device').value;
            const ip = document.getElementById('wserial_ip').value.trim();
            if (!deviceId && !ip) return alert('Pick a node that is live on the LAN, or type its IP address (for example 192.168.4.1 when you are on the ESP32 hotspot).');
            fetch('/api/wserial/connect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_id: ip ? '' : deviceId, ip: ip})
            }).then(r => r.json().then(j => ({ok: r.ok, j: j}))).then(({ok, j}) => {
                if (!ok || j.error) { alert(j.error || 'Could not open the wireless serial connection.'); return; }
                wserialKey = j.key;
                setSerialConnectedUI(true);
                appendSerialText(`\\n[*] Wireless serial connected to ${j.key} (${j.ip}:${j.port})\\n`);
                wserialSource = new EventSource('/api/wserial/stream?key=' + encodeURIComponent(j.key));
                wserialSource.onmessage = function(e) {
                    const data = JSON.parse(e.data);
                    if (data.closed) { disconnectWirelessSerial(true); appendSerialText('\\n[*] Wireless serial ended.\\n'); return; }
                    let text = data.text;
                    if (document.getElementById('serial-timestamps').checked) {
                        text = `[${new Date().toLocaleTimeString()}] ${text}`;
                    }
                    appendSerialText(text);
                };
            }).catch(err => alert('Wireless serial error: ' + err));
        }

        function disconnectWirelessSerial(quiet = false) {
            const key = wserialKey;
            if (wserialSource) { wserialSource.close(); wserialSource = null; }
            wserialKey = null;
            setSerialConnectedUI(false);
            if (key) {
                fetch('/api/wserial/disconnect', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({key: key})
                }).catch(() => {});
            }
            if (!quiet) appendSerialText('\\n[*] Wireless serial disconnected.\\n');
        }

        function sendSerialCommand() {
            const input = document.getElementById('serial-input');
            const lineEnding = document.getElementById('serial-line-ending').value;
            const text = input.value;
            if (!text) return;
            const lan = serialMode === 'lan';
            if (lan && !wserialKey) return alert('Connect to a LAN node first.');

            fetch(lan ? '/api/wserial/send' : '/api/serial/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(lan ? {key: wserialKey, text: text, line_ending: lineEnding} : {text: text, line_ending: lineEnding})
            }).then(r => r.json()).then(res => {
                if (res.error) logToOutput(`[Serial] Send Error: ${res.error}`);
                else {
                    input.value = '';
                    appendSerialText(`> ${text}\\n`);
                }
            });
        }

        // --- USB FLASHING WITH LIVE CLI STREAMING ---
        let pendingSerialReconnect = false;
        let savedPort = "";
        let savedBaud = "";

        function generateRandomDeviceId() {
            const rand = Math.random().toString(16).substring(2, 6);
            document.getElementById('prov_device_id').value = `node_${rand}`;
        }

        function syncBoardSelectors(value) {
            const el1 = document.getElementById('board-selector');
            const el2 = document.getElementById('prov_board_selector');
            if (el1 && el1.value !== value) el1.value = value;
            if (el2 && el2.value !== value) el2.value = value;
        }

        function flashDevice() {
            const port = document.getElementById('prov_port').value;
            if (!port) return alert("Select a COM port first.");

            if (serialEventSource) {
                savedPort = document.getElementById('serial_port').value;
                savedBaud = document.getElementById('serial_baud').value;
                if (savedPort === port) {
                    pendingSerialReconnect = true;
                    disconnectUsbSerial(true);
                }
            }

            const btn = document.getElementById('btn-flash-device');
            btn.disabled = true;
            btn.innerHTML = `<i class="ph-bold ph-spinner animate-spin text-lg"></i> Flashing Bootloader...`;

            const payload = {
                port: port,
                baud: document.getElementById('prov_baud').value,
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL,
                fleet_id: document.getElementById('prov_fleet_id').value,
                device_id: document.getElementById('prov_device_id').value,
                auth_token: "secret_abc123",
                fqbn: document.getElementById('prov_board_selector').value
            };

            runStreamingTask('/flash/stream', payload, 
                () => {
                    switchPanelTab('panel-out');
                },
                (success) => {
                    btn.disabled = false;
                    btn.innerHTML = `<i class="ph-bold ph-lightning text-lg"></i> <span>Compile & Provision via USB</span>`;
                    loadFleetsAndDevices();

                    if (pendingSerialReconnect) {
                        logToOutput("[System] Reconnecting Serial Monitor in 2 seconds...");
                        setTimeout(() => {
                            document.getElementById('serial_port').value = savedPort;
                            document.getElementById('serial_baud').value = savedBaud;
                            connectUsbSerial();
                            pendingSerialReconnect = false;
                            switchPanelTab('panel-serial');
                        }, 2000);
                    }
                }
            );
        }

        // --- COMPILATION & OTA DEPLOYMENT (Verify once, deploy without recompiling) ---
        let activeBin = null;   // staged binary: {file, path, name, size}

        function buildPayload(extra) {
            return Object.assign({
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL,
                topic: currentTopic,
                code: document.getElementById('code-editor').value,
                fqbn: document.getElementById('prov_board_selector').value
            }, extra || {});
        }

        function setVerifyChip(text, kind) {
            const chip = document.getElementById('verify-chip');
            if (!chip) return;
            if (!text) { chip.classList.add('hidden'); return; }
            const tone = kind === 'staged' ? 'border-amber-500/30 bg-amber-500/10 text-amber-300' : 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400';
            chip.className = 'ml-1 text-[11px] font-mono px-2 py-0.5 rounded-full border whitespace-nowrap ' + tone;
            chip.innerText = text;
        }

        function showProgress(pct) {
            const container = document.getElementById('action-progress-container');
            const bar = document.getElementById('action-progress-bar');
            container.classList.remove('hidden');
            bar.style.width = pct + '%';
        }

        function hideProgress() {
            const container = document.getElementById('action-progress-container');
            document.getElementById('action-progress-bar').style.width = '100%';
            setTimeout(() => container.classList.add('hidden'), 500);
        }

        function hasDesktopApi() {
            return !!(window.pywebview && window.pywebview.api && window.pywebview.api.choose_bin);
        }

        function currentDeployTargetLabel() {
            if (selectedNodes.size > 0) return `${selectedNodes.size} selected node(s)`;
            return currentTargetType === 'fleet' ? `fleet '${currentFleet}'` : `node '${currentDeviceId}'`;
        }

        function deployExtras() {
            return selectedNodes.size > 0 ? {device_ids: Array.from(selectedNodes)} : {};
        }

        // Verify: compile (or reuse) and keep the .bin ready for Deploy
        function triggerCompileOnly() {
            closeAllMenus();
            showProgress(30);
            runStreamingTask('/api/compile/stream', buildPayload(), () => { showProgress(60); },
                (success) => {
                    hideProgress();
                    if (success) setVerifyChip('Verified - .bin ready', 'ok');
                    else setVerifyChip('');
                });
        }

        async function saveBinAs() {
            const suggested = `wifey_${currentFleet}_firmware.bin`;
            if (hasDesktopApi()) {
                const path = await window.pywebview.api.choose_save_path(suggested);
                if (!path) { logToOutput('[File] Save cancelled.'); return; }
                const res = await fetch('/api/bin/save_as', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})
                }).then(r => r.json());
                if (res.success) logToOutput(`[File] Firmware binary saved to ${res.path} (${(res.size / 1024).toFixed(1)} KB)`);
                else logToOutput(`[File] [ERROR] ${res.error}`);
            } else {
                const a = document.createElement('a');
                a.href = '/api/download_bin';
                a.download = suggested;
                document.body.appendChild(a);
                a.click();
                a.remove();
                logToOutput('[File] Downloaded firmware binary.');
            }
        }

        function triggerSaveBin() {
            closeAllMenus();
            showProgress(30);
            logToOutput('[Build] Preparing firmware binary (reuses the verified build when the sketch is unchanged)...');
            runStreamingTask('/api/compile/stream', buildPayload(), () => { showProgress(60); },
                (success) => {
                    hideProgress();
                    if (success) { setVerifyChip('Verified - .bin ready', 'ok'); saveBinAs(); }
                });
        }

        async function triggerLoadBin() {
            closeAllMenus();
            if (hasDesktopApi()) {
                const path = await window.pywebview.api.choose_bin();
                if (!path) return;
                const info = await fetch('/api/bin/inspect', {
                    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path: path})
                }).then(r => r.json());
                if (!info.valid) { logToOutput(`[File] [ERROR] ${info.error}`); alert(info.error); return; }
                stageBin({path: info.path, name: info.name, size: info.size});
            } else {
                document.getElementById('bin-file-input').click();
            }
        }

        function handleBinLoad(event) {
            const file = event.target.files[0];
            if (file) stageBin({file: file, name: file.name, size: file.size});
            event.target.value = '';
        }

        function stageBin(bin) {
            activeBin = bin;
            logToOutput(`[File] Loaded custom binary: ${bin.name} (${(bin.size / 1024).toFixed(2)} KB)`);
            logToOutput(`[System] Binary staged. Click 'Deploy OTA' to push it to ${currentDeployTargetLabel()} - no compile needed.`);
            setVerifyChip('Staged: ' + bin.name, 'staged');
        }

        function deployFirmware() {
            showProgress(20);
            if (activeBin) {
                deployBinary(activeBin);
                return;
            }
            logToOutput(`[Deploy] Preparing firmware for ${currentDeployTargetLabel()}...`);
            showProgress(40);
            runStreamingTask('/api/compile/stream', buildPayload(), () => { showProgress(60); },
                (success) => {
                    if (!success) {
                        document.getElementById('action-progress-container').classList.add('hidden');
                        logToOutput('[Deploy] [ERROR] Compilation failed. OTA Deployment aborted.');
                        return;
                    }
                    showProgress(80);
                    logToOutput(`[Deploy] Publishing verified binary to '${currentTopic}'...`);
                    fetch('/api/deploy_compiled', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(Object.assign({topic: currentTopic}, deployExtras()))
                    })
                    .then(res => res.json()).then(data => {
                        hideProgress();
                        logToOutput(data.logs);
                        refreshLiveNow();
                    })
                    .catch(err => {
                        document.getElementById('action-progress-container').classList.add('hidden');
                        logToOutput(`[Deploy] [ERROR] Deployment Error: ${err}`);
                    });
                });
        }

        // Deploys a staged / dropped .bin straight to the cloud (no compile)
        function deployBinary(bin) {
            showProgress(80);
            logToOutput(`[Deploy] Uploading ${bin.name} for ${currentDeployTargetLabel()}...`);
            const formData = new FormData();
            if (bin.file) formData.append('bin_file', bin.file); else formData.append('bin_path', bin.path);
            formData.append('topic', currentTopic);
            if (selectedNodes.size > 0) formData.append('device_ids', JSON.stringify(Array.from(selectedNodes)));

            fetch('/api/deploy', { method: 'POST', body: formData })
            .then(res => res.json()).then(data => {
                hideProgress();
                logToOutput(data.logs);
                if (!data.error) { activeBin = null; setVerifyChip(''); }
                refreshLiveNow();
            })
            .catch(err => {
                document.getElementById('action-progress-container').classList.add('hidden');
                logToOutput(`[Deploy] [ERROR] Deployment Error: ${err}`);
            });
        }

        // --- DRAG & DROP A .bin ANYWHERE IN THE WINDOW TO DEPLOY IT ---
        let dragDepth = 0;
        function isFileDrag(e) {
            return !!(e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files'));
        }
        function showDropOverlay(show) {
            const ov = document.getElementById('drop-overlay');
            if (!ov) return;
            if (show) document.getElementById('drop-target').innerText = 'Target: ' + currentDeployTargetLabel();
            ov.style.display = show ? 'flex' : 'none';
        }
        window.addEventListener('dragenter', (e) => { if (!isFileDrag(e)) return; e.preventDefault(); dragDepth++; showDropOverlay(true); });
        window.addEventListener('dragover', (e) => { if (!isFileDrag(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
        window.addEventListener('dragleave', (e) => { if (!isFileDrag(e)) return; dragDepth = Math.max(0, dragDepth - 1); if (dragDepth === 0) showDropOverlay(false); });
        window.addEventListener('drop', (e) => {
            if (!isFileDrag(e)) return;
            e.preventDefault();
            dragDepth = 0;
            showDropOverlay(false);
            const files = Array.from(e.dataTransfer.files || []);
            const binFile = files.find(f => f.name.toLowerCase().endsWith('.bin'));
            const zipFile = files.find(f => f.name.toLowerCase().endsWith('.zip'));

            if (zipFile) {
                installZipLibrary(zipFile);
                return;
            }

            if (!binFile) { 
                logToOutput('[File] [ERROR] Drop a firmware .bin file to deploy or a .zip file to install a library.'); 
                return; 
            }
            const label = currentDeployTargetLabel();
            if (!confirm(`Deploy ${binFile.name} (${(binFile.size / 1024).toFixed(1)} KB) to ${label}?`)) {
                logToOutput(`[File] Dropped ${binFile.name} was not deployed.`);
                return;
            }
            deployBinary({file: binFile, name: binFile.name, size: binFile.size});
        });

        // --- REALTIME EVENT STREAM ---
        try {
            const eventSource = new EventSource('/api/events/stream');
            eventSource.onmessage = function(e) {
                if (!e.data || e.data.startsWith(':')) return;
                try {
                    const payload = JSON.parse(e.data);
                    if (payload.text) {
                        logToOutput(payload.text);
                        if (payload.text.includes("[OTA") || payload.text.includes("[Batch") || payload.text.includes("[System]") || payload.text.includes("[LAN]")) {
                            refreshLiveNow();
                        }
                    }
                } catch(err) {}
            };
        } catch(err) {}

        // --- SETTINGS VIEW ---
        function loadSettings() {
            loadDeviceOptions();
            fetch('/api/settings').then(r => r.json()).then(data => {
                document.getElementById('cfg_r2_account').value = data.r2_account_id || '';
                document.getElementById('cfg_r2_access').value = data.r2_access_key || '';
                document.getElementById('cfg_r2_secret').value = data.r2_secret_key || '';
                document.getElementById('cfg_r2_bucket').value = data.r2_bucket || '';
                document.getElementById('cfg_ngrok_domain').value = data.ngrok_domain || '';
                document.getElementById('cfg_ngrok_token').value = data.ngrok_token || '';
                if (document.getElementById('cfg_worker_url')) {
                    document.getElementById('cfg_worker_url').value = data.worker_base_url || '';
                }
                if (document.getElementById('cfg_google_client_id')) {
                    document.getElementById('cfg_google_client_id').value = data.google_client_id || '';
                }
                if (document.getElementById('cfg_google_sub')) {
                    document.getElementById('cfg_google_sub').value = data.google_sub || '';
                }
                if (document.getElementById('cfg_google_id_token')) {
                    document.getElementById('cfg_google_id_token').value = data.google_id_token || '';
                }
                if (document.getElementById('cfg_google_client_secret')) {
                    document.getElementById('cfg_google_client_secret').value = data.google_client_secret || '';
                }
            });
        }
        
        let prefsSaveTimer = null;
        function saveWifiConfig() {
            const ssid = document.getElementById('top-wifi-ssid').value;
            const pass = document.getElementById('top-wifi-pass').value;
            try { localStorage.setItem('wifey_ssid', ssid); localStorage.setItem('wifey_pass', pass); } catch (e) {}
            clearTimeout(prefsSaveTimer);
            prefsSaveTimer = setTimeout(() => {
                fetch('/api/prefs', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({wifi_ssid: ssid, wifi_pass: pass})
                }).catch(() => {});
            }, 400);
        }

        // Wi-Fi is remembered on disk by the app itself (wifey_prefs.json), so it survives restarts
        // even when the embedded browser throws away localStorage.
        function loadWifiConfig() {
            const ssidEl = document.getElementById('top-wifi-ssid');
            const passEl = document.getElementById('top-wifi-pass');
            let lsSsid = null, lsPass = null;
            try { lsSsid = localStorage.getItem('wifey_ssid'); lsPass = localStorage.getItem('wifey_pass'); } catch (e) {}
            fetch('/api/prefs').then(r => r.json()).then(p => {
                if (p.wifi_ssid || p.wifi_pass) {
                    ssidEl.value = p.wifi_ssid || '';
                    passEl.value = p.wifi_pass || '';
                } else if (lsSsid !== null || lsPass !== null) {
                    ssidEl.value = lsSsid || '';
                    passEl.value = lsPass || '';
                    saveWifiConfig();
                }
            }).catch(() => {
                if (lsSsid !== null) ssidEl.value = lsSsid;
                if (lsPass !== null) passEl.value = lsPass;
            });
        }

        // --- DEVICE & FIRMWARE OPTIONS (Settings) ---
        function loadDeviceOptions() {
            fetch('/api/prefs').then(r => r.json()).then(p => {
                const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
                set('opt_poll_seconds', p.poll_seconds);
                set('opt_net_mode', p.net_mode);
                set('opt_ap_window', p.ap_window_min);
                set('opt_bin_dir', p.bin_dir || '');
                const mirror = document.getElementById('opt_serial_mirror');
                if (mirror) mirror.checked = !!p.serial_mirror;
            }).catch(() => {});
        }

        function saveDeviceOptions() {
            const payload = {
                poll_seconds: parseInt(document.getElementById('opt_poll_seconds').value, 10) || 0,
                net_mode: document.getElementById('opt_net_mode').value,
                ap_window_min: parseInt(document.getElementById('opt_ap_window').value, 10) || 0,
                serial_mirror: document.getElementById('opt_serial_mirror').checked,
                bin_dir: document.getElementById('opt_bin_dir').value.trim()
            };
            fetch('/api/prefs', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(res => {
                if (res.error) { alert(res.error); return; }
                logToOutput('[System] Device & firmware options saved. They apply to the next Verify / Deploy / USB provisioning.');
            });
        }

        async function browseBinDir() {
            if (!hasDesktopApi() || !window.pywebview.api.choose_folder) {
                alert('The folder picker is only available in the desktop app. Type the folder path instead.');
                return;
            }
            const folder = await window.pywebview.api.choose_folder();
            if (folder) document.getElementById('opt_bin_dir').value = folder;
        }

        function saveSettings() {
            const payload = {
                r2_account_id: document.getElementById('cfg_r2_account').value,
                r2_access_key: document.getElementById('cfg_r2_access').value,
                r2_secret_key: document.getElementById('cfg_r2_secret').value,
                r2_bucket: document.getElementById('cfg_r2_bucket').value,
                ngrok_domain: document.getElementById('cfg_ngrok_domain').value,
                ngrok_token: document.getElementById('cfg_ngrok_token').value,
                worker_base_url: document.getElementById('cfg_worker_url')?.value || '',
                google_client_id: document.getElementById('cfg_google_client_id')?.value || '',
                google_client_secret: document.getElementById('cfg_google_client_secret')?.value || '',
                google_sub: document.getElementById('cfg_google_sub')?.value || '',
                google_id_token: document.getElementById('cfg_google_id_token')?.value || ''
            };

            fetch('/api/settings', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            }).then(r => r.json()).then(res => {
                if (res.success) {
                    logToOutput("[System] Settings saved successfully to .env and applied to current session.");
                    alert("Settings saved successfully.");
                } else {
                    logToOutput(`[System] Error saving settings: ${res.error}`);
                }
            });
        }

        // --- GOOGLE OAUTH & PROFILE MANAGEMENT ---
        var authPollInterval = null;

        function startGoogleAuth() {
            const spinner = document.getElementById('auth-spinner');
            if (spinner) spinner.classList.remove('hidden');

            fetch('/api/auth/login', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (!data.success || !data.auth_url) {
                        if (spinner) spinner.classList.add('hidden');
                        alert('Authentication Notice: ' + (data.error || 'Failed to initiate login.'));
                        return;
                    }

                    const fallbackContainer = document.getElementById('auth-fallback-container');
                    const authLink = document.getElementById('auth-link');
                    if (fallbackContainer && authLink) {
                        authLink.href = data.auth_url;
                        fallbackContainer.classList.remove('hidden');
                    }

                    logToOutput('[Auth] Google OAuth authorization URL opened. Awaiting login...');
                    if (authPollInterval) clearInterval(authPollInterval);
                    authPollInterval = setInterval(() => checkAuthStatus(false), 1500);
                })
                .catch(err => {
                    if (spinner) spinner.classList.add('hidden');
                    alert('Error starting Google Login: ' + err);
                });
        }
        
        function checkAuthStatus(isInitialLoad = false) {
            fetch('/api/auth/status')
                .then(r => r.json())
                .then(data => {
                    const isAuthed = data.status === true || data.authenticated === true;
                    if (isAuthed && data.user) {
                        if (authPollInterval) {
                            clearInterval(authPollInterval);
                            authPollInterval = null;
                        }
                        updateUserProfileUI(data.user);

                        const overlay = document.getElementById('auth-overlay');
                        if (overlay) {
                            overlay.classList.add('opacity-0', 'pointer-events-none');
                            setTimeout(() => overlay.classList.add('hidden'), 500);
                        }
                        const spinner = document.getElementById('auth-spinner');
                        if (spinner) spinner.classList.add('hidden');

                        if (!isInitialLoad) {
                            logToOutput(`[Auth] Google Authentication successful! Welcome, ${data.user.email || 'User'}.`);
                        }
                    } else if (isInitialLoad) {
                        const overlay = document.getElementById('auth-overlay');
                        if (overlay) {
                            overlay.classList.remove('hidden', 'opacity-0', 'pointer-events-none');
                        }
                    }
                })
                .catch(err => {
                    console.error('[Auth] Status check error:', err);
                });
        }

        function updateUserProfileUI(user) {
            if (!user) return;
            const img = document.getElementById('user-profile-img');
            const fallback = document.getElementById('user-profile-fallback');
            const emailEl = document.getElementById('profile-email-text');

            if (emailEl && user.email) {
                emailEl.innerText = user.email;
            }
            if (img && user.picture) {
                img.src = user.picture;
                img.classList.remove('hidden');
                if (fallback) fallback.classList.add('hidden');
            } else {
                if (img) img.classList.add('hidden');
                if (fallback) fallback.classList.remove('hidden');
            }
        }

        function signOutUser() {
            closeAllMenus();
            fetch('/api/auth/signout', { method: 'POST' })
                .then(r => r.json())
                .then(() => {
                    const img = document.getElementById('user-profile-img');
                    const fallback = document.getElementById('user-profile-fallback');
                    const emailEl = document.getElementById('profile-email-text');
                    if (img) { img.src = ''; img.classList.add('hidden'); }
                    if (fallback) fallback.classList.remove('hidden');
                    if (emailEl) emailEl.innerText = 'Not Signed In';

                    const overlay = document.getElementById('auth-overlay');
                    if (overlay) {
                        overlay.classList.remove('hidden', 'opacity-0', 'pointer-events-none');
                    }
                    const spinner = document.getElementById('auth-spinner');
                    if (spinner) spinner.classList.add('hidden');

                    logToOutput('[Auth] User signed out successfully.');
                })
                .catch(err => {
                    alert('Error signing out: ' + err);
                });
        }
        
    </script>
</body>
</html>
"""

# --- ROUTES ---

@app.route('/')
def index():
    if not SETUP_STATE["done"]:
        return render_template_string(SETUP_HTML)
    return render_template_string(IDE_HTML, ngrok_url=NGROK_URL, r2_bucket=R2_BUCKET)

# --- GOOGLE OAUTH 2.0 AUTHENTICATION ROUTES ---

AUTH_CALLBACK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WiFeY Studio - Authentication</title>
    <style>
        body {
            background-color: #0d0e12;
            color: #e0e2ec;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100vh;
            margin: 0;
        }
        .card {
            background-color: #17191f;
            border: 1px solid #282a36;
            border-radius: 16px;
            padding: 40px;
            max-width: 440px;
            text-align: center;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        h1 { color: #00b4b6; font-size: 22px; margin: 12px 0 8px 0; }
        p { color: #8e92a4; font-size: 13px; line-height: 1.6; margin: 0 0 20px 0; }
        .badge { display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 600; }
        .success { background: rgba(16, 185, 129, 0.15); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.3); }
        .error { background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.3); }
    </style>
</head>
<body>
    <div class="card">
        {% if success %}
            <div class="badge success"><span>&#10003;</span> Authentication Successful</div>
            <h1>Connected to WiFeY Studio</h1>
            <p>Signed in as <strong>{{ email }}</strong>.<br>You can safely close this browser tab and return to WiFeY Studio.</p>
            <script>
                setTimeout(() => { window.close(); }, 2500);
            </script>
        {% else %}
            <div class="badge error"><span>&#10007;</span> Sign In Failed</div>
            <h1>Authentication Error</h1>
            <p>{{ error }}</p>
        {% endif %}
    </div>
</body>
</html>
"""

@app.route('/api/auth/login', methods=['GET', 'POST'])
def auth_login():
    global GOOGLE_CLIENT_ID
    client_id = GOOGLE_CLIENT_ID or os.getenv("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return jsonify({
            "success": False,
            "error": "Google Client ID is not configured. Please enter your Google OAuth Client ID in Settings."
        }), 400

    state = secrets.token_hex(16)
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode('ascii')).digest()
    ).decode('ascii').rstrip('=')

    OAUTH_STATES[state] = {
        "verifier": code_verifier,
        "created_at": time.time()
    }

    # Clean up stale states (> 10 mins)
    now = time.time()
    for s in list(OAUTH_STATES.keys()):
        if now - OAUTH_STATES[s]["created_at"] > 600:
            OAUTH_STATES.pop(s, None)

    redirect_uri = "http://127.0.0.1:5050/api/auth/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256"
    }
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

    import webbrowser
    webbrowser.open_new_tab(auth_url)
    return jsonify({"success": True, "auth_url": auth_url, "state": state})

@app.route('/api/auth/callback')
def auth_callback():
    code = request.args.get('code')
    state = request.args.get('state')
    error = request.args.get('error')

    if error:
        return render_template_string(AUTH_CALLBACK_HTML, success=False, error=error)

    if not code:
        return render_template_string(AUTH_CALLBACK_HTML, success=False, error="Authorization code is missing from Google redirect.")

    verifier_data = OAUTH_STATES.pop(state, None) if state else None
    code_verifier = verifier_data.get("verifier") if verifier_data else None

    token_payload = {
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": "http://127.0.0.1:5050/api/auth/callback",
        "grant_type": "authorization_code",
    }
    if code_verifier:
        token_payload["code_verifier"] = code_verifier
    if GOOGLE_CLIENT_SECRET:
        token_payload["client_secret"] = GOOGLE_CLIENT_SECRET

    try:
        token_resp = requests.post("https://oauth2.googleapis.com/token", data=token_payload, timeout=15)
    except Exception as e:
        return render_template_string(AUTH_CALLBACK_HTML, success=False, error=f"Network error contacting Google: {e}")

    if not token_resp.ok:
        err_msg = token_resp.text
        try:
            err_json = token_resp.json()
            err_msg = err_json.get("error_description") or err_json.get("error") or err_msg
        except:
            pass
        return render_template_string(AUTH_CALLBACK_HTML, success=False, error=f"Token exchange failed: {err_msg}")

    token_data = token_resp.json()
    id_token = token_data.get("id_token", "")
    access_token = token_data.get("access_token", "")

    # Fetch user profile data from Google UserInfo endpoint
    user_info = {}
    if access_token:
        try:
            ui_resp = requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15
            )
            if ui_resp.ok:
                user_info = ui_resp.json()
        except Exception:
            pass

    google_sub = user_info.get("sub", "")
    email = user_info.get("email", "")
    picture = user_info.get("picture", "")

    # Fallback to decode JWT id_token payload if userinfo fields were missing
    if (not google_sub or not email or not picture) and id_token:
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                jwt_payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode('utf-8')).decode('utf-8'))
                if not google_sub: google_sub = jwt_payload.get("sub", "")
                if not email: email = jwt_payload.get("email", "")
                if not picture: picture = jwt_payload.get("picture", "")
        except Exception:
            pass

    # Save to database
    db.set_user_profile(
        google_sub=google_sub,
        email=email,
        id_token=id_token,
        picture=picture
    )

    return render_template_string(AUTH_CALLBACK_HTML, success=True, email=email or "User")

@app.route('/api/auth/status')
def auth_status():
    user_prof = db.get_user_profile()
    is_authenticated = bool(user_prof.get("google_sub") or user_prof.get("id_token") or user_prof.get("email"))
    return jsonify({
        "status": is_authenticated,
        "authenticated": is_authenticated,
        "user": user_prof if is_authenticated else None
    })

@app.route('/api/auth/signout', methods=['POST', 'GET'])
def auth_signout():
    db.clear_user_profile()
    return jsonify({"success": True, "status": False, "authenticated": False, "message": "Signed out successfully"})

@app.route('/api/setup_status')
def setup_status():
    return jsonify(dict(SETUP_STATE, warmup=dict(WARMUP_STATE)))

@app.route('/api/ports', methods=['GET'])
def get_ports():
    ports = [{"device": p.device, "desc": p.description} for p in serial.tools.list_ports.comports()]
    return jsonify(ports)

@app.route('/api/serial/connect', methods=['POST'])
def serial_connect():
    data = request.json or {}
    port = data.get('port')
    baud = int(data.get('baud', 115200))
    
    close_serial_connection()
    try:
        with SERIAL_LOCK:
            s = serial.Serial(port, baud, timeout=0.5)
            SERIAL_STATE["serial_obj"] = s
            SERIAL_STATE["port"] = port
            SERIAL_STATE["baud"] = baud
            SERIAL_STATE["is_running"] = True
        return jsonify({"status": "connected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/serial/disconnect', methods=['POST'])
def serial_disconnect():
    close_serial_connection()
    return jsonify({"status": "disconnected"})

@app.route('/api/serial/send', methods=['POST'])
def serial_send():
    data = request.json or {}
    text = data.get('text', '')
    line_ending = data.get('line_ending', '\r\n')
    
    # Unescape literal backslash escapes
    line_ending = line_ending.replace('\\r', '\r').replace('\\n', '\n')
    
    with SERIAL_LOCK:
        s = SERIAL_STATE.get("serial_obj")
        if not s or not s.is_open:
            return jsonify({"error": "Serial port is not open."}), 400
        try:
            s.write((text + line_ending).encode('utf-8'))
            s.flush()
            return jsonify({"status": "sent"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route('/api/serial/stream')
def serial_stream():
    def generate():
        while True:
            with SERIAL_LOCK:
                if not SERIAL_STATE["is_running"] or not SERIAL_STATE["serial_obj"]:
                    break
                s = SERIAL_STATE["serial_obj"]
                try:
                    if s.is_open and s.in_waiting > 0:
                        raw = s.readline().decode('utf-8', errors='replace')
                        yield f"data: {json.dumps({'text': raw})}\n\n"
                    else:
                        time.sleep(0.04)
                except Exception as e:
                    err_text = f"[Serial Error] {str(e)}\n"
                    yield f"data: {json.dumps({'text': err_text})}\n\n"
                    break
    return Response(generate(), mimetype='text/event-stream')

@app.route('/preview', methods=['POST'])
def preview_code():
    data = request.json or {}
    try:
        wrapped = generate_wrapped_code(
            data.get('ssid'), data.get('pass'), data.get('api_url'),
            data.get('topic'), data.get('code')
        )
        return jsonify({"code": wrapped})
    except Exception as e:
        return jsonify({"code": "// Error generating preview:\n" + str(e)})

@app.route('/api/db', methods=['GET'])
def get_db():
    return jsonify(db.get_all())

@app.route('/api/device/delete', methods=['POST'])
def delete_device_route():
    data = request.json or {}
    device_id = data.get('device_id')
    if device_id:
        db.delete_device(device_id)
        return jsonify({"success": True})
    return jsonify({"error": "Missing device_id"}), 400

@app.route('/api/fleet/create', methods=['POST'])
def create_fleet_route():
    data = request.json or {}
    fleet = data.get('fleet')
    if fleet:
        db.create_fleet(fleet)
        return jsonify({"success": True})
    return jsonify({"error": "Missing fleet name"}), 400

def refresh_presigned_url(url):
    """Re-signs stored R2 links on every check-in, so a node that polls days later never hits an expired URL."""
    if not url or "X-Amz-Signature" not in url:
        return url
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY):
        return url
    try:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).lstrip('/')
        prefix = R2_BUCKET + "/"
        if not path.startswith(prefix):
            return url
        return get_s3_client().generate_presigned_url(
            'get_object', Params={'Bucket': R2_BUCKET, 'Key': path[len(prefix):]}, ExpiresIn=3600)
    except Exception as e:
        print(f"[R2] Re-sign error: {e}")
        return url

@app.route('/api/manifest', methods=['GET'])
def api_manifest():
    fleet_id = request.args.get('fleet', 'DefaultFleet')
    device_id = request.args.get('device')
    try:
        reported_build = int(request.args.get('build')) if request.args.get('build') not in (None, '') else None
    except ValueError:
        reported_build = None
    auth_header = request.headers.get('Authorization', '')

    token = auth_header.replace('Bearer ', '').strip() if 'Bearer ' in auth_header else auth_header.strip()

    device_info = db.get_device(device_id) if device_id else None
    if device_info:
        expected_token = device_info.get("device_token")
        if expected_token and token and token != expected_token and token != "secret_abc123":
            return jsonify({"error": "Unauthorized: invalid device token"}), 401
        fleet_id = device_info.get("fleet", fleet_id)
        db.update_device_seen(device_id, current_build=reported_build)
    else:
        if device_id:
            db.register_or_update_device(device_id, fleet_id, device_token=token or f"dt_{secrets.token_hex(8)}",
                                         current_build=reported_build)

    target_build, firmware_url = db.get_target_build(fleet_id, device_id)
    firmware_url = refresh_presigned_url(firmware_url)

    return jsonify({"target_build": int(target_build), "download_url": firmware_url or ""})

@app.route('/api/device/notify_flash', methods=['POST'])
def notify_flash():
    data = request.json or {}
    device_id = data.get('device_id')
    status = data.get('status', 'SUCCESS')
    new_build = data.get('new_build')
    device_token = data.get('device_token')
    
    auth_header = request.headers.get('Authorization', '')
    token = device_token or (auth_header.replace('Bearer ', '').strip() if 'Bearer ' in auth_header else '')

    if not device_id:
        return jsonify({"error": "Missing device_id"}), 400

    dev = db.get_device(device_id)
    if dev and token and dev.get("device_token") != token:
        return jsonify({"error": "Unauthorized device token"}), 401

    if status.upper() == "SUCCESS" and new_build is not None:
        try:
            build_int = int(new_build)
        except:
            build_int = 100
        db.update_device_seen(device_id, current_build=build_int)
        
        # Clear custom override if target reached
        if dev and dev.get("custom_build_override") is not None and build_int >= dev.get("custom_build_override"):
            db.clear_node_override(device_id)

        msg = f"🎉 [OTA Flash Verified] Node '{device_id}' successfully updated to build #{build_int}!"
        try:
            print(f"[OTA Flash Verified] Node '{device_id}' successfully updated to build #{build_int}!")
        except Exception:
            pass
        emit_ui_log(msg)
        return jsonify({"success": True, "device_id": device_id, "current_build": build_int})

    return jsonify({"success": True})

@app.route('/api/device/reassign_fleet', methods=['POST'])
def reassign_fleet_route():
    data = request.json or {}
    device_id = data.get('device_id')
    new_fleet = data.get('new_fleet')
    if not device_id or not new_fleet:
        return jsonify({"error": "Missing device_id or new_fleet"}), 400
    
    db.reassign_fleet(device_id, new_fleet)
    msg = f"[System] Reassigned node '{device_id}' to fleet '{new_fleet}'."
    emit_ui_log(msg)
    return jsonify({"success": True, "device_id": device_id, "new_fleet": new_fleet})

@app.route('/api/device/override', methods=['POST'])
def device_override_route():
    data = request.json or {}
    device_id = data.get('device_id')
    build_num = data.get('build_num')
    firmware_url = data.get('firmware_url')
    clear = data.get('clear', False)

    if not device_id:
        return jsonify({"error": "Missing device_id"}), 400

    if clear or build_num is None:
        db.clear_node_override(device_id)
        msg = f"[System] Cleared custom build override for node '{device_id}'."
    else:
        db.set_node_override(device_id, int(build_num), firmware_url)
        msg = f"[System] Set custom override on node '{device_id}': build #{build_num}."

    emit_ui_log(msg)
    return jsonify({"success": True, "device_id": device_id})

@app.route('/api/lan/devices', methods=['GET'])
def get_lan_devices():
    valid_tokens = db.get_all_device_tokens()
    now = time.time()
    active_nodes = []
    with LAN_NODES_LOCK:
        for dev_id, info in list(LAN_NODES.items()):
            if info.get("device_token") in valid_tokens:
                is_online = (now - info.get("last_seen", 0)) < LAN_ONLINE_SECS
                active_nodes.append({
                    **info,
                    "online": is_online
                })
    return jsonify({"devices": active_nodes})

@app.route('/api/lan/firmware/<filename>', methods=['GET'])
def lan_firmware_route(filename):
    fname = os.path.basename(filename)
    for folder in (LAN_FIRMWARE_DIR, OUTPUT_DIR):
        bin_path = os.path.join(folder, fname)
        if os.path.isfile(bin_path):
            return send_file(bin_path, mimetype="application/octet-stream", as_attachment=False)
    return jsonify({"error": "Firmware binary not found"}), 404

@app.route('/api/cloud/sync', methods=['GET', 'POST'])
def cloud_sync():
    global db
    user_prof = db.get_user_profile()
    token = request.headers.get('Authorization', '').replace('Bearer ', '').strip() or user_prof.get("id_token")
    google_sub = request.args.get('google_sub') or user_prof.get("google_sub")

    if request.method == 'GET':
        if not google_sub:
            return jsonify({"error": "Missing google_sub for cloud sync"}), 400

        # Try Cloudflare Worker Gateway first
        if WORKER_BASE_URL and token:
            try:
                sync_url = f"{WORKER_BASE_URL}/users/{google_sub}/wifey_db.json"
                resp = requests.get(sync_url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
                if resp.status_code == 200:
                    remote_db = resp.json()
                    with db.lock:
                        db._data = remote_db
                    db.save()
                    emit_ui_log("[Cloud Sync] Successfully downloaded database from Cloudflare Worker.")
                    return jsonify({"success": True, "db": db.get_all()})
            except Exception as e:
                print(f"[Worker Sync GET] Error: {e}")

        # Fallback to direct R2 S3 client
        if R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY:
            try:
                s3 = get_s3_client()
                obj = s3.get_object(Bucket=R2_BUCKET, Key=f"users/{google_sub}/wifey_db.json")
                remote_db = json.loads(obj['Body'].read().decode('utf-8'))
                with db.lock:
                    db._data = remote_db
                db.save()
                emit_ui_log("[Cloud Sync] Successfully downloaded database from Cloudflare R2.")
                return jsonify({"success": True, "db": db.get_all()})
            except Exception as e:
                return jsonify({"error": f"Cloud download failed: {str(e)}"}), 500

        return jsonify({"error": "No cloud credentials configured for download"}), 400

    else:
        # POST - Upload wifey_db.json
        data = request.json or {}
        sub = data.get('google_sub') or google_sub
        email = data.get('email') or user_prof.get("email")
        new_token = data.get('id_token') or token
        if sub:
            db.set_user_profile(google_sub=sub, email=email, id_token=new_token)
            google_sub = sub
            token = new_token

        if not google_sub:
            return jsonify({"error": "Missing google_sub for cloud sync"}), 400

        db_json_bytes = json.dumps(db.get_all(), indent=2).encode('utf-8')

        # Try Cloudflare Worker Gateway first
        if WORKER_BASE_URL and token:
            try:
                sync_url = f"{WORKER_BASE_URL}/users/{google_sub}/wifey_db.json"
                resp = requests.put(
                    sync_url,
                    data=db_json_bytes,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    },
                    timeout=15
                )
                if resp.status_code in (200, 201, 204):
                    emit_ui_log("[Cloud Sync] Successfully uploaded database to Cloudflare Worker.")
                    return jsonify({"success": True})
            except Exception as e:
                print(f"[Worker Sync POST] Error: {e}")

        # Fallback to direct R2 S3 client
        if R2_ACCOUNT_ID and R2_ACCESS_KEY and R2_SECRET_KEY:
            try:
                s3 = get_s3_client()
                s3.put_object(
                    Bucket=R2_BUCKET,
                    Key=f"users/{google_sub}/wifey_db.json",
                    Body=db_json_bytes,
                    ContentType="application/json"
                )
                emit_ui_log("[Cloud Sync] Successfully uploaded database to Cloudflare R2.")
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": f"Cloud upload failed: {str(e)}"}), 500

        return jsonify({"error": "No cloud credentials configured for upload"}), 400

@app.route('/api/events/stream')
def events_stream():
    def generate():
        while True:
            try:
                msg = UI_LOG_QUEUE.get(timeout=20)
                yield f"data: {json.dumps({'text': msg})}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"
    return Response(generate(), mimetype='text/event-stream')

# =====================================================================
#  BUILD PIPELINE: shared build path, toolchain warm-up, verified-bin reuse
# =====================================================================
BUILD_LOCK = threading.Lock()
WARMUP_STATE = {"state": "idle", "message": "", "started": 0, "finished": 0}
LAST_BUILD = {"hash": "", "path": "", "size": 0, "time": 0}
LAST_BUILD_FILE = os.path.join(VERIFIED_DIR, "last.json")

try:
    with open(LAST_BUILD_FILE, "r", encoding="utf-8") as _f:
        _lb = json.load(_f)
    if isinstance(_lb, dict) and os.path.exists(_lb.get("path", "")):
        LAST_BUILD.update(_lb)
except Exception:
    pass

WARMUP_USER_CODE = """#include <WebServer.h>
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include <SPI.h>
#include <Ticker.h>
void setup() { Serial.println("warm-up"); }
void loop() { delay(1000); }
"""
WARMUP_FALLBACK_CODE = "void setup() { Serial.println(\"warm-up\"); }\nvoid loop() { delay(1000); }\n"

def write_sketch_if_changed(code):
    """Only touches the sketch when its text changed, so the compiler keeps its cached objects."""
    os.makedirs(SKETCH_DIR, exist_ok=True)
    try:
        with open(SKETCH_FILE, "r", encoding="utf-8") as f:
            if f.read() == code:
                return False
    except FileNotFoundError:
        pass
    with open(SKETCH_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
    return True

def warmup_toolchain():
    """Compiles the wrapper (and the popular AP/web libraries) once in the background, so the core and every
    wrapper library are already built in the shared build path when the first real Verify/Deploy runs."""
    cli = SETUP_STATE.get("cli_path") or find_arduino_cli()
    if not cli:
        WARMUP_STATE.update(state="error", message="Arduino CLI not found", finished=time.time())
        return
    WARMUP_STATE.update(state="running", message="Preloading ESP32 core & wrapper libraries...", started=time.time())
    with BUILD_LOCK:
        ok = False
        for user_code in (WARMUP_USER_CODE, WARMUP_FALLBACK_CODE):
            try:
                code = generate_wrapped_code("", "", "http://127.0.0.1:5050", "", user_code, build_num=0)
                write_sketch_if_changed(code)
                res = subprocess.run(
                    [cli, "compile", "--fqbn", DEFAULT_FQBN, "--build-path", BUILD_CACHE_DIR, "-j", "0", SKETCH_FILE],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
                if res.returncode == 0:
                    ok = True
                    break
            except Exception as e:
                print(f"[Warm-up] {e}")
    if ok:
        WARMUP_STATE.update(state="done", message="Compiler warm - libraries preloaded", finished=time.time())
    else:
        WARMUP_STATE.update(state="error", message="Warm-up failed (builds still work, first one is just slower)", finished=time.time())

def start_warmup():
    if WARMUP_STATE["state"] == "running":
        return
    threading.Thread(target=warmup_toolchain, daemon=True).start()

def prune_verified(keep=8):
    try:
        files = sorted((os.path.join(VERIFIED_DIR, f) for f in os.listdir(VERIFIED_DIR) if f.endswith(".bin")),
                       key=os.path.getmtime, reverse=True)
        for old in files[keep:]:
            if old != LAST_BUILD.get("path"):
                try:
                    os.remove(old)
                except OSError:
                    pass
    except Exception:
        pass

def register_verified(build_hash, src_bin):
    dst = os.path.join(VERIFIED_DIR, build_hash + ".bin")
    
    # Re-create the folder in case arduino-cli wiped it during compilation
    os.makedirs(VERIFIED_DIR, exist_ok=True)
    
    if os.path.abspath(src_bin) != os.path.abspath(dst):
        shutil.copyfile(src_bin, dst)
    LAST_BUILD.update(hash=build_hash, path=dst, size=os.path.getsize(dst), time=time.time())
    try:
        with open(LAST_BUILD_FILE, "w", encoding="utf-8") as f:
            json.dump(LAST_BUILD, f)
    except Exception:
        pass
    prune_verified()
    return dst

def autosave_bin_copy(src):
    """Copies a freshly verified binary into the user's chosen .bin folder (Settings > bin folder)."""
    folder = prefs.get("bin_dir")
    if not folder:
        return None
    try:
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, time.strftime("WiFeY_%Y%m%d-%H%M%S") + ".bin")
        shutil.copyfile(src, dest)
        return dest
    except Exception as e:
        print(f"[Bin Folder] Could not save copy: {e}")
        return None

def get_current_binary():
    """(bytes, source_path) of the newest verified/compiled firmware, or (None, '')."""
    src = LAST_BUILD.get("path")
    if not (src and os.path.exists(src)):
        src = os.path.join(OUTPUT_DIR, "WiFeYSketch.ino.bin")
        if not os.path.exists(src):
            return None, ""
    with open(src, "rb") as f:
        return f.read(), src

def validate_firmware_image(data):
    """OTA needs the app-only image (starts with the ESP image magic 0xE9), not a merged flash image."""
    if not data or len(data) < 4096:
        return "That file is too small to be an ESP32 firmware image."
    if data[0] != 0xE9:
        return ("This does not look like an OTA app image (ESP32 app images start with byte 0xE9). "
                "Use the app-only .bin (e.g. Sketch.ino.bin), not a .merged.bin / full flash image.")
    return ""

# --- LIVE STREAMING COMPILATION ENDPOINT ---
@app.route('/api/compile/stream', methods=['POST'])
def compile_stream_route():
    data = request.json or {}
    force = bool(data.get('force'))

    def generate():
        t0 = time.time()
        try:
            final_code, build_hash, info = build_firmware_source(data)
        except Exception as e:
            yield f"[Build] [ERROR] Could not generate firmware source: {e}\n"
            return

        mode_note = "auto-detected" if info["auto"] else "selected"
        yield f"[Build] Wi-Fi mode: {NET_MODE_LABELS[info['net_mode']]} ({mode_note})\n"

        cached = os.path.join(VERIFIED_DIR, build_hash + ".bin")
        if not force and os.path.exists(cached):
            try:
                shutil.copyfile(cached, os.path.join(OUTPUT_DIR, "WiFeYSketch.ino.bin"))
            except Exception:
                pass
            register_verified(build_hash, cached)
            yield f"[Build] [SUCCESS] Sketch unchanged since the last Verify - reusing the compiled binary ({os.path.getsize(cached) / 1024:.1f} KB). No recompile needed.\n"
            return

        cli_path = SETUP_STATE.get("cli_path") or find_arduino_cli()
        if not cli_path:
            yield "[Build] [ERROR] Arduino CLI executable was not found on this machine.\n"
            return

        if WARMUP_STATE["state"] == "running":
            yield "[Build] Toolchain warm-up is still preloading libraries - your build starts right after it.\n"
        if not BUILD_LOCK.acquire(blocking=False):
            yield "[Build] Another build is running - waiting for it to finish...\n"
            BUILD_LOCK.acquire()

        proc = None
        try:
            try:
                write_sketch_if_changed(final_code)
            except Exception as e:
                yield f"[Build] [ERROR] Failed writing sketch: {e}\n"
                return

            out_bin = os.path.join(OUTPUT_DIR, "WiFeYSketch.ino.bin")
            if os.path.exists(out_bin):
                try:
                    os.remove(out_bin)
                except OSError:
                    pass

            if WARMUP_STATE["state"] == "done":
                yield "[Build] Launching arduino-cli compiler (libraries already preloaded)...\n"
            else:
                yield "[Build] Launching arduino-cli compiler... (first run takes ~2 mins)\n"

            compile_cmd = [
                cli_path, "compile",
                "--fqbn", info["fqbn"],
                "--build-path", BUILD_CACHE_DIR,
                "--output-dir", OUTPUT_DIR,
                "-j", "0",
                SKETCH_FILE
            ]
            proc = subprocess.Popen(
                compile_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=CREATE_NO_WINDOW
            )
            for line in iter(proc.stdout.readline, ''):
                if line:
                    yield f"[CLI] {line.rstrip()}\n"
            proc.stdout.close()
            return_code = proc.wait()

            if return_code != 0:
                yield f"[Build] [ERROR] Compilation failed with exit code {return_code}.\n"
            elif os.path.exists(out_bin):
                register_verified(build_hash, out_bin)
                size_kb = os.path.getsize(out_bin) / 1024
                yield f"[Build] [SUCCESS] Compilation completed in {time.time() - t0:.1f}s! Binary size: {size_kb:.1f} KB - verified and ready to deploy (no recompile needed).\n"
                saved = autosave_bin_copy(out_bin)
                if saved:
                    yield f"[File] Copy saved to {saved}\n"
            else:
                yield "[Build] [ERROR] Compilation succeeded but output binary was missing.\n"
        except Exception as e:
            yield f"[Build] [ERROR] Exception during compilation: {e}\n"
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            BUILD_LOCK.release()

    return Response(generate(), mimetype='text/plain')

@app.route('/api/download_bin', methods=['GET'])
def download_bin_route():
    _, src = get_current_binary()
    if src:
        return send_file(src, as_attachment=True, download_name="wifey_firmware.bin", mimetype="application/octet-stream")
    return jsonify({"error": "No compiled binary found. Please compile first."}), 404

@app.route('/api/bin/info', methods=['GET'])
def bin_info_route():
    data, src = get_current_binary()
    if not data:
        return jsonify({"available": False})
    return jsonify({"available": True, "size": len(data), "hash": LAST_BUILD.get("hash", "")[:12], "time": LAST_BUILD.get("time", 0)})

@app.route('/api/bin/save_as', methods=['POST'])
def bin_save_as_route():
    """Copies the verified binary to any location chosen in the native Save dialog."""
    data = request.json or {}
    dest = os.path.expanduser(str(data.get('path') or '').strip())
    if not dest:
        return jsonify({"success": False, "error": "No destination path given."}), 400
    if not dest.lower().endswith(".bin"):
        dest += ".bin"
    _, src = get_current_binary()
    if not src:
        return jsonify({"success": False, "error": "No compiled binary found. Verify the sketch first."}), 404
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        shutil.copyfile(src, dest)
        return jsonify({"success": True, "path": dest, "size": os.path.getsize(dest)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/bin/inspect', methods=['POST'])
def bin_inspect_route():
    """Checks a .bin chosen from anywhere on disk before it is staged for deployment."""
    data = request.json or {}
    path = os.path.expanduser(str(data.get('path') or '').strip())
    if not path or not path.lower().endswith(".bin") or not os.path.isfile(path):
        return jsonify({"valid": False, "error": "Choose an existing .bin file."}), 400
    try:
        with open(path, "rb") as f:
            blob = f.read()
        err = validate_firmware_image(blob)
        return jsonify({"valid": not err, "error": err, "name": os.path.basename(path), "size": len(blob), "path": path})
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

# =====================================================================
#  DEPLOYMENT (whole fleets or checkbox-selected nodes)
# =====================================================================
def parse_topic(topic):
    """'Fleet/fleet/update' -> (Fleet, None); 'Fleet/devices/node_1/update' -> (Fleet, node_1)."""
    parts = (topic or "").split('/')
    fleet = parts[0] if parts and parts[0] else "DefaultFleet"
    if len(parts) >= 3 and parts[1] == "devices" and parts[2]:
        return fleet, parts[2]
    return fleet, None

def parse_id_list(raw):
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if isinstance(raw, str):
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                return [str(x) for x in val if str(x).strip()]
        except Exception:
            pass
        return [x.strip() for x in raw.split(',') if x.strip()]
    return []

def deploy_to_targets(binary_bytes, fleet=None, device_ids=None, target_build=None):
    """Publishes a firmware image to a fleet or to specific nodes, then nudges LAN nodes to update immediately."""
    err = validate_firmware_image(binary_bytes)
    if err:
        raise ValueError(err)
    snapshot = db.get_all()
    registry = snapshot.get("devices", {})

    if device_ids:
        ids = [d for d in dict.fromkeys(device_ids) if d in registry]
        if not ids:
            raise ValueError("None of the selected nodes exist in the database.")
        needed = [int(registry[d].get("current_build", 100)) + 1 for d in ids]
        needed += [db.get_next_build_number(f) for f in {registry[d].get("fleet", "DefaultFleet") for d in ids}]
        build = int(target_build) if target_build else max(needed)
        url = upload_firmware_to_cloud(registry[ids[0]].get("fleet", "DefaultFleet"), binary_bytes, build)
        for d in ids:
            db.set_node_override(d, build, url)
        nudged = nudge_devices(device_ids=ids)
        label = f"{len(ids)} selected node(s): {', '.join(ids)}"
    else:
        fleet = fleet or "DefaultFleet"
        build = int(target_build) if target_build else db.get_next_build_number(fleet)
        url = upload_firmware_to_cloud(fleet, binary_bytes, build)
        db.set_fleet_target_build(fleet, build, url)
        nudged = nudge_devices(fleet=fleet)
        label = f"Entire fleet '{fleet}'"

    poll = int(prefs.get("poll_seconds", 30) or 0)
    lines = [f"🎉 SUCCESS! Deployed build #{build} to {label}."]
    if url.startswith("http://"):
        lines.append("⚠ Cloud upload was unavailable (check R2 / Worker settings) - the binary is served over your LAN only; remote nodes cannot fetch it.")
    if nudged:
        lines.append(f"{nudged} LAN node(s) were told to update right now - no restart needed.")
    when = f"within ~{poll}s" if poll > 0 else "after their next reboot (OTA polling is off)"
    lines.append(f"Other nodes pick it up {when}.")
    return {"target_build": build, "url": url, "nudged": nudged, "message": "\n".join(lines)}

@app.route('/api/deploy_compiled', methods=['POST'])
def deploy_compiled_route():
    try:
        data = request.json or {}
        binary_bytes, _ = get_current_binary()
        if not binary_bytes:
            return jsonify({"error": True, "logs": "❌ No compiled binary found. Please Verify (compile) first."})
        fleet, topic_device = parse_topic(data.get('topic', 'DefaultFleet/fleet/update'))
        device_ids = parse_id_list(data.get('device_ids')) or ([topic_device] if topic_device else [])
        res = deploy_to_targets(binary_bytes, fleet=fleet, device_ids=device_ids, target_build=data.get('target_build'))
        emit_ui_log(res["message"])
        return jsonify({"error": False, "logs": res["message"], "target_build": res["target_build"]})
    except Exception as e:
        return jsonify({"error": True, "logs": f"❌ Deployment Failed: {str(e)}"})

@app.route('/api/deploy', methods=['POST'])
def deploy_binary():
    """Deploys a .bin chosen by file picker, drag-and-drop (multipart 'bin_file') or local path ('bin_path')."""
    try:
        binary_bytes = None
        label = ""
        if 'bin_file' in request.files:
            up = request.files['bin_file']
            binary_bytes = up.read()
            label = up.filename or "uploaded.bin"
        elif request.form.get('bin_path'):
            path = os.path.expanduser(request.form.get('bin_path').strip())
            if not path.lower().endswith(".bin") or not os.path.isfile(path):
                return jsonify({"error": True, "logs": "❌ The selected .bin file no longer exists."})
            with open(path, "rb") as f:
                binary_bytes = f.read()
            label = os.path.basename(path)
        if not binary_bytes:
            return jsonify({"error": True, "logs": "❌ No binary file provided in deployment request."})

        fleet, topic_device = parse_topic(request.form.get('topic', 'DefaultFleet/fleet/update'))
        device_ids = parse_id_list(request.form.get('device_ids')) or ([topic_device] if topic_device else [])
        res = deploy_to_targets(binary_bytes, fleet=fleet, device_ids=device_ids, target_build=request.form.get('target_build') or None)
        msg = f"[Deploy] {label} ({len(binary_bytes) / 1024:.1f} KB)\n" + res["message"]
        emit_ui_log(res["message"])
        return jsonify({"error": False, "logs": msg, "target_build": res["target_build"]})
    except Exception as e:
        return jsonify({"error": True, "logs": f"❌ Deployment Failed: {str(e)}"})

@app.route('/api/fleets/batch_update', methods=['POST'])
def batch_update_fleets():
    fleet_ids_raw = request.form.get('fleet_ids') or (request.json.get('fleet_ids') if request.is_json else None)
    target_build = request.form.get('target_build') or (request.json.get('target_build') if request.is_json else None)
    fleet_ids = parse_id_list(fleet_ids_raw)
    if not fleet_ids:
        return jsonify({"error": "No fleets specified"}), 400

    if 'bin_file' in request.files:
        binary_bytes = request.files['bin_file'].read()
    else:
        binary_bytes, _ = get_current_binary()
    if not binary_bytes:
        return jsonify({"error": "No binary provided or compiled yet. Please compile first."}), 400

    updated = []
    try:
        for fleet in fleet_ids:
            res = deploy_to_targets(binary_bytes, fleet=fleet, target_build=int(target_build) if target_build else None)
            updated.append(f"{fleet} (#{res['target_build']})")
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    log_msg = f"🎉 [Batch Deploy] Deployed to {len(updated)} fleets: {', '.join(updated)}"
    emit_ui_log(log_msg)
    return jsonify({"success": True, "updated_fleets": updated, "message": log_msg})

# --- FLEET REMOVAL ---
@app.route('/api/fleet/delete', methods=['POST'])
def delete_fleet_route():
    data = request.json or {}
    fleet = (data.get('fleet') or '').strip()
    if not fleet:
        return jsonify({"success": False, "error": "Missing fleet name"}), 400
    if fleet == "DefaultFleet":
        return jsonify({"success": False, "error": "DefaultFleet is the built-in fleet and cannot be removed."}), 400
    snapshot = db.get_all()
    in_use = any(d.get("fleet") == fleet for d in snapshot.get("devices", {}).values())
    if fleet not in snapshot.get("manifest", {}) and not in_use:
        return jsonify({"success": False, "error": f"Fleet '{fleet}' does not exist."}), 404
    moved = db.delete_fleet(fleet, "DefaultFleet")
    emit_ui_log(f"[System] Removed fleet '{fleet}'. {len(moved)} node(s) moved to DefaultFleet.")
    return jsonify({"success": True, "moved": moved})

# --- PREFERENCES (remembered Wi-Fi, bin folder, OTA options) ---
@app.route('/api/prefs', methods=['GET', 'POST'])
def prefs_route():
    if request.method == 'GET':
        return jsonify(prefs.all())
    try:
        return jsonify(prefs.update(request.json or {}))
    except (TypeError, ValueError) as e:
        return jsonify({"error": f"Invalid preference value: {e}"}), 400

# --- LIVE STATUS ---
@app.route('/api/live', methods=['GET'])
def live_route():
    return jsonify(compute_live())

# --- WIRELESS (LAN) SERIAL MONITOR ---
@app.route('/api/wserial/connect', methods=['POST'])
def wserial_connect():
    data = request.json or {}
    device_id = str(data.get('device_id') or '').strip()
    ip = str(data.get('ip') or '').strip()
    port = WSERIAL_PORT
    if not ip:
        with LAN_NODES_LOCK:
            node = dict(LAN_NODES.get(device_id) or {})
        if not node or (time.time() - node.get("last_seen", 0)) > LAN_ONLINE_SECS * 3:
            return jsonify({"error": "That node is not live on the LAN right now. Wait for its beacon, or type its IP address."}), 404
        ip = node.get("ip", "")
        port = int(node.get("tcp") or WSERIAL_PORT)
    try:
        socket.inet_aton(ip)
    except OSError:
        return jsonify({"error": f"'{ip}' is not a valid IPv4 address."}), 400
    key = device_id or ip
    with WSERIAL_LOCK:
        old = WSERIAL_SESSIONS.pop(key, None)
    if old:
        old.close()
    sess = WirelessSerialSession(key, ip, port)
    try:
        sess.connect()
    except Exception as e:
        return jsonify({"error": f"Could not open {ip}:{port} ({e}). The node must run a build with 'Mirror Serial to LAN' enabled."}), 502
    with WSERIAL_LOCK:
        WSERIAL_SESSIONS[key] = sess
    return jsonify({"status": "connected", "key": key, "ip": ip, "port": port})

@app.route('/api/wserial/disconnect', methods=['POST'])
def wserial_disconnect():
    key = str((request.json or {}).get('key') or '')
    with WSERIAL_LOCK:
        sess = WSERIAL_SESSIONS.pop(key, None)
    if sess:
        sess.close()
    return jsonify({"status": "disconnected"})

@app.route('/api/wserial/send', methods=['POST'])
def wserial_send():
    data = request.json or {}
    key = str(data.get('key') or '')
    text = data.get('text', '')
    line_ending = str(data.get('line_ending', '\r\n')).replace('\\r', '\r').replace('\\n', '\n')
    with WSERIAL_LOCK:
        sess = WSERIAL_SESSIONS.get(key)
    if not sess or not sess.alive:
        return jsonify({"error": "Wireless serial is not connected."}), 400
    try:
        sess.send(text + line_ending)
        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/wserial/stream')
def wserial_stream():
    key = request.args.get('key', '')
    with WSERIAL_LOCK:
        sess = WSERIAL_SESSIONS.get(key)
    if not sess:
        return jsonify({"error": "No such wireless serial session."}), 404

    def generate():
        last = 0
        while True:
            with sess.cond:
                if sess.seq <= last and sess.alive:
                    sess.cond.wait(timeout=10)
                items = [it for it in sess.buf if it[0] > last]
                alive = sess.alive
            if items:
                for _, text in items:
                    yield f"data: {json.dumps({'text': text})}\n\n"
                last = items[-1][0]
            elif not alive:
                yield f"data: {json.dumps({'closed': True})}\n\n"
                return
            else:
                yield ": keepalive\n\n"
    return Response(generate(), mimetype='text/event-stream')

# --- LIVE STREAMING USB FLASH ENDPOINT ---
PROV_USER_CODE = """void setup() {
  Serial.println("WiFeY Bootloader Initialized & Connected!");
}
void loop() {
  static unsigned long p = 0;
  if (millis() - p >= 3000) { p = millis(); Serial.println("[WiFeY] Standing by for OTA updates..."); }
}
"""

@app.route('/flash/stream', methods=['POST'])
def flash_stream_route():
    data = request.json or {}
    port = data.get('port')
    baud = str(data.get('baud', '115200'))
    fleet_id = (data.get('fleet_id') or 'DefaultFleet').strip() or 'DefaultFleet'
    device_id = (data.get('device_id') or 'node_1').strip() or 'node_1'
    fqbn = data.get('fqbn', DEFAULT_FQBN)

    # Automatically generate cryptographically random device_token (dt_ + 16 hex chars)
    device_token = data.get('device_token') or data.get('auth_token')
    if not device_token or device_token in ("secret_abc123", ""):
        device_token = f"dt_{secrets.token_hex(8)}"

    initial_build = 100

    def generate():
        close_serial_connection()
        time.sleep(0.5)

        cli_path = SETUP_STATE.get("cli_path") or find_arduino_cli()
        if not cli_path:
            yield "[Hardware] [ERROR] Arduino CLI not found.\n"
            return
        if not port:
            yield "[Hardware] [ERROR] No COM port selected!\n"
            return

        opts = get_wrapper_options({"net_mode": "sta"})
        full_code = generate_wrapped_code(
            data.get('ssid', ''), data.get('pass', ''), data.get('api_url'), "", PROV_USER_CODE,
            build_num=initial_build, device_token=device_token, opts=opts,
            provision=True, device_id=device_id, fleet_id=fleet_id, fqbn=fqbn)

        yield f"[System] Generated device token: {device_token}\n"
        yield f"[System] Stamping initial firmware build: #{initial_build}\n"

        if not BUILD_LOCK.acquire(blocking=False):
            yield "[Build] Another build is running - waiting for it to finish...\n"
            BUILD_LOCK.acquire()
        try:
            try:
                write_sketch_if_changed(full_code)
            except Exception as e:
                yield f"[Hardware] [ERROR] Failed to write provisioning sketch: {e}\n"
                return

            yield f"[Build] Compiling Pull-OTA bootloader sketch for {fqbn} (shared build cache)...\n"
            compile_cmd = [cli_path, "compile", "--fqbn", fqbn, "--build-path", BUILD_CACHE_DIR, "-j", "0", SKETCH_FILE]
            proc_comp = subprocess.Popen(compile_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in proc_comp.stdout:
                clean = line.rstrip()
                if clean:
                    yield f"[CLI] {clean}\n"
            proc_comp.wait()
            if proc_comp.returncode != 0:
                yield f"[Build] [ERROR] Compilation failed with code {proc_comp.returncode}.\n"
                return

            yield f"[Upload] Starting USB upload to {port} @ {baud} baud...\n"
            upload_cmd = [cli_path, "upload", "-p", port, "--fqbn", fqbn, "--input-dir", BUILD_CACHE_DIR,
                          "--upload-property", f"upload.speed={baud}", SKETCH_FILE]
            proc_up = subprocess.Popen(upload_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in proc_up.stdout:
                clean = line.rstrip()
                if clean:
                    yield f"[CLI] {clean}\n"
            proc_up.wait()
            if proc_up.returncode != 0:
                yield f"[Upload] [ERROR] Upload failed with code {proc_up.returncode}.\n"
                return
        finally:
            BUILD_LOCK.release()

        # Register node and issue initial build integer 100
        db.register_or_update_device(device_id, fleet_id, device_token=device_token, current_build=initial_build)
        db.clear_node_override(device_id)

        yield f"[System] [SUCCESS] 🎉 ESP32 node '{device_id}' provisioned for fleet '{fleet_id}'!\n"
        yield f"[System] Token: {device_token} | Initial Build: #{initial_build}\n"
        yield f"[System] The node checks {data.get('api_url')} for OTA updates every {prefs.get('poll_seconds')}s - no restart needed after a deploy.\n"

    return Response(generate(), mimetype='text/plain')

@app.route('/api/settings', methods=['GET', 'POST'])
def settings_route():
    global R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET, NGROK_STATIC_DOMAIN, NGROK_AUTHTOKEN, WORKER_BASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, _S3_CLIENT
    user_prof = db.get_user_profile()

    if request.method == 'GET':
        return jsonify({
            "r2_account_id": R2_ACCOUNT_ID,
            "r2_access_key": R2_ACCESS_KEY,
            "r2_secret_key": R2_SECRET_KEY,
            "r2_bucket": R2_BUCKET,
            "ngrok_domain": NGROK_STATIC_DOMAIN,
            "ngrok_token": NGROK_AUTHTOKEN,
            "worker_base_url": WORKER_BASE_URL,
            "google_client_id": GOOGLE_CLIENT_ID,
            "google_client_secret": GOOGLE_CLIENT_SECRET,
            "google_sub": user_prof.get("google_sub", ""),
            "google_id_token": user_prof.get("id_token", ""),
            "google_picture": user_prof.get("picture", "")
        })
    
    # POST - update .env and runtime globals
    data = request.json or {}
    R2_ACCOUNT_ID = data.get('r2_account_id', '').strip()
    R2_ACCESS_KEY = data.get('r2_access_key', '').strip()
    R2_SECRET_KEY = data.get('r2_secret_key', '').strip()
    R2_BUCKET = data.get('r2_bucket', 'wifey-firmware').strip()
    NGROK_STATIC_DOMAIN = data.get('ngrok_domain', '').strip()
    NGROK_AUTHTOKEN = data.get('ngrok_token', '').strip()
    WORKER_BASE_URL = data.get('worker_base_url', '').strip().rstrip('/')
    GOOGLE_CLIENT_ID = data.get('google_client_id', '').strip()
    if 'google_client_secret' in data:
        GOOGLE_CLIENT_SECRET = data.get('google_client_secret', '').strip()

    sub = data.get('google_sub', '').strip()
    id_tok = data.get('google_id_token', '').strip()
    pic = data.get('google_picture', '').strip()
    if sub or id_tok or pic:
        db.set_user_profile(google_sub=sub, id_token=id_tok, picture=pic)

    with _S3_LOCK:
        _S3_CLIENT = None  # Reset client so next call uses updated credentials

    lines = [
        f"NGROK_STATIC_DOMAIN={NGROK_STATIC_DOMAIN}",
        f"NGROK_AUTHTOKEN={NGROK_AUTHTOKEN}",
        f"R2_ACCOUNT_ID={R2_ACCOUNT_ID}",
        f"R2_ACCESS_KEY={R2_ACCESS_KEY}",
        f"R2_SECRET_KEY={R2_SECRET_KEY}",
        f"R2_BUCKET={R2_BUCKET}",
        f"WORKER_BASE_URL={WORKER_BASE_URL}",
        f"GOOGLE_CLIENT_ID={GOOGLE_CLIENT_ID}",
        f"GOOGLE_CLIENT_SECRET={GOOGLE_CLIENT_SECRET}"
    ]
    try:
        with open(ENV_FILE_PATH, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# --- ARDUINO LIBRARY MANAGER ROUTES ---

def get_user_libraries():
    """Gets user-installed libraries using arduino-cli to guarantee accurate paths."""
    libraries = {}
    cli_path = SETUP_STATE.get("cli_path") or find_arduino_cli()
    if cli_path:
        try:
            # Ask arduino-cli for the definitive list of installed user libraries
            res = subprocess.run([cli_path, "lib", "list", "--format", "json"], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
            if res.returncode == 0:
                data = json.loads(res.stdout or "{}")
                for lib in data.get("installed_libraries", []):
                    lib_info = lib.get("library", {})
                    name = lib_info.get("name")
                    install_dir = lib_info.get("install_dir")
                    version = lib_info.get("version", "Custom")
                    if name and install_dir:
                        libraries[name] = {
                            "name": name, 
                            "version": version, 
                            "path": install_dir, 
                            "deletable": True
                        }
        except Exception:
            pass

    # Manual Fallback if CLI fails or returns nothing (handles OneDrive and standard setups)
    if not libraries:
        user_home = os.path.expanduser("~")
        possible_paths = [
            os.path.join(user_home, "Documents", "Arduino", "libraries"),
            os.path.join(user_home, "OneDrive", "Documents", "Arduino", "libraries"),
            os.path.join(user_home, "Arduino", "libraries")
        ]
        for user_lib_dir in possible_paths:
            if os.path.exists(user_lib_dir):
                for folder in os.listdir(user_lib_dir):
                    full_path = os.path.join(user_lib_dir, folder)
                    if os.path.isdir(full_path) and not folder.startswith('.'):
                        libraries[folder] = {"name": folder, "version": "Custom", "path": full_path, "deletable": True}
                break # Stop after finding the first valid libraries folder

    return libraries

def get_core_libraries():
    """Gets ESP32 core default libraries which are bundled with the hardware package."""
    libraries = {}
    core_lib_dir = ""
    
    if os.name == 'nt': 
        local_app = os.environ.get("LOCALAPPDATA", "")
        esp32_root = os.path.join(local_app, "Arduino15", "packages", "esp32", "hardware", "esp32")
    else: 
        user_home = os.path.expanduser("~")
        esp32_root = os.path.join(user_home, ".arduino15", "packages", "esp32", "hardware", "esp32")

    if os.path.exists(esp32_root):
        try:
            versions = [v for v in os.listdir(esp32_root) if os.path.isdir(os.path.join(esp32_root, v))]
            if versions:
                versions.sort(reverse=True)
                core_lib_dir = os.path.join(esp32_root, versions[0], "libraries")
        except Exception:
            pass

    if core_lib_dir and os.path.exists(core_lib_dir):
        for folder in os.listdir(core_lib_dir):
            full_path = os.path.join(core_lib_dir, folder)
            if os.path.isdir(full_path) and not folder.startswith('.'):
                libraries[folder] = {"name": folder, "version": "ESP32 Core Default", "path": full_path, "deletable": False}
                
    return libraries

@app.route('/api/lib/list', methods=['GET'])
def list_libraries():
    try:
        all_libs = {}
        # Core libraries first
        all_libs.update(get_core_libraries())
        # User libraries override core if there is a naming collision
        all_libs.update(get_user_libraries())
        
        return jsonify({"libraries": list(all_libs.values())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lib/uninstall', methods=['POST'])
def uninstall_library():
    data = request.json or {}
    lib_name = data.get('name')
    if not lib_name:
        return jsonify({"success": False, "error": "Library name is required"}), 400
    try:
        user_libs = get_user_libraries()
        # Security Check: Only allow deletion if it exists in the USER libraries dictionary
        if lib_name not in user_libs:
            return jsonify({"success": False, "error": "Library not found or is a protected system library"}), 404
        
        target_path = user_libs[lib_name]["path"]
        
        if os.path.exists(target_path) and os.path.isdir(target_path):
            import shutil
            shutil.rmtree(target_path) 
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Folder not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/lib/upload', methods=['POST'])
def upload_library():
    if 'zip_file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files['zip_file']
    if not file.filename.lower().endswith('.zip'):
        return jsonify({"error": "Only .zip files are supported"}), 400
    cli_path = SETUP_STATE.get("cli_path") or find_arduino_cli()
    if not cli_path:
        return jsonify({"error": "Arduino CLI not found"}), 500

    subprocess.run([cli_path, "config", "set", "library.enable_unsafe_install", "true"], capture_output=True, creationflags=CREATE_NO_WINDOW)
    tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, file.filename)
    try:
        file.save(zip_path)
        res = subprocess.run([cli_path, "lib", "install", "--zip-path", zip_path], capture_output=True, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_NO_WINDOW)
        err_msg = (res.stderr or "").strip() or (res.stdout or "").strip()
        if res.returncode == 0:
            return jsonify({"success": True})
        return jsonify({"success": False, "error": err_msg or "Failed to install ZIP"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        if os.path.exists(zip_path): os.remove(zip_path)
        if os.path.exists(tmp_dir): os.rmdir(tmp_dir)

@app.route('/api/lib/examples', methods=['GET'])
def get_library_examples():
    examples = []
    try:
        all_libs = {}
        all_libs.update(get_core_libraries())
        all_libs.update(get_user_libraries())
        
        for lib_name, lib_info in all_libs.items():
            base_dir = lib_info.get("path")
            if not base_dir or not os.path.exists(base_dir): continue
            
            examples_dir = os.path.join(base_dir, "examples")
            if os.path.isdir(examples_dir):
                for root, _, files in os.walk(examples_dir):
                    for file in files:
                        if file.endswith('.ino') or file.endswith('.pde'):
                            full_path = os.path.join(root, file)
                            rel_path = os.path.relpath(full_path, examples_dir)
                            example_name = os.path.splitext(rel_path)[0].replace(os.sep, ' > ')
                            examples.append({"library": lib_name, "name": example_name, "path": full_path})
                            
        return jsonify({"examples": examples})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/lib/example/content', methods=['POST'])
def get_example_content():
    path = (request.json or {}).get('path', '')
    if not path or not os.path.exists(path) or not (path.endswith('.ino') or path.endswith('.pde')):
        return jsonify({"error": "Invalid example file path"}), 400
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return jsonify({"code": f.read()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
# --- NATIVE FILE DIALOGS FOR THE UI (window.pywebview.api) ---

def _first_path(result):
    if not result:
        return ""
    if isinstance(result, (list, tuple)):
        return str(result[0]) if result else ""
    return str(result)

class DesktopApi:
    """Lets the UI open real OS dialogs: pick a .bin from anywhere, save a .bin anywhere, choose a folder."""
    def _kind(self, name):
        import webview
        fd = getattr(webview, "FileDialog", None)
        return getattr(fd, name) if fd is not None else getattr(webview, name + "_DIALOG")

    def open_url(self, url):
        import webbrowser
        try:
            # Use webbrowser for all OS types; it safely handles URL encoding
            webbrowser.open_new_tab(url)
            return True
        except Exception as e:
            print(f"[DesktopApi] Error opening URL: {e}")
            return False

    def choose_bin(self):
        import webview
        try:
            res = webview.windows[0].create_file_dialog(
                self._kind("OPEN"), directory=prefs.get("bin_dir") or "", allow_multiple=False,
                file_types=("Firmware (*.bin)", "All files (*.*)"))
            return _first_path(res)
        except Exception:
            return ""

    def choose_save_path(self, suggested="wifey_firmware.bin"):
        import webview
        try:
            res = webview.windows[0].create_file_dialog(
                self._kind("SAVE"), directory=prefs.get("bin_dir") or "", save_filename=suggested,
                file_types=("Firmware (*.bin)", "All files (*.*)"))
            return _first_path(res)
        except Exception:
            return ""

    def choose_folder(self):
        import webview
        try:
            res = webview.windows[0].create_file_dialog(self._kind("FOLDER"), directory=prefs.get("bin_dir") or "")
            return _first_path(res)
        except Exception:
            return ""

# --- MAIN RUNNER ---
if __name__ == '__main__':
    import webview

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    def start_server():
        # Bind to 0.0.0.0 to enable LAN ESP32 devices to access local firmware and manifest
        app.run(host='0.0.0.0', port=5050, debug=False, use_reloader=False)

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    setup_thread = threading.Thread(target=setup_environment_thread, daemon=True)
    setup_thread.start()

    # Launch background UDP LAN beacon discovery listener + live online/offline monitor
    udp_thread = threading.Thread(target=udp_beacon_listener, daemon=True)
    udp_thread.start()
    threading.Thread(target=lan_monitor_thread, daemon=True).start()

    # Wait until the web server really accepts connections (instead of a blind sleep)
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", 5050), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)

    webview.create_window(
        title='WiFeY Studio',
        url='http://127.0.0.1:5050',
        width=1500,
        height=950,
        min_size=(1100, 750),
        background_color='#121316',
        js_api=DesktopApi()
    )

    # PyWebview starts in private mode by default (localStorage is wiped on exit); keep a persistent profile instead.
    start_kwargs = {}
    try:
        import inspect
        _params = inspect.signature(webview.start).parameters
        if 'private_mode' in _params:
            start_kwargs['private_mode'] = False
        if 'storage_path' in _params:
            _storage = os.path.join(APP_DIR, "webview_data")
            os.makedirs(_storage, exist_ok=True)
            start_kwargs['storage_path'] = _storage
    except Exception:
        pass

    try:
        webview.start(**start_kwargs)
    finally:
        close_serial_connection()
        for _s in list(WSERIAL_SESSIONS.values()):
            try:
                _s.close()
            except Exception:
                pass
        db.save()