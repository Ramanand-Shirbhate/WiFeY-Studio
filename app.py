import os
import sys
import re
import time
import json
import tempfile
import subprocess
import shutil
import threading
import webbrowser
import hashlib
import zipfile
import urllib.request
import ssl
import serial
import serial.tools.list_ports
from flask import Flask, render_template_string, request, jsonify, Response
import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

# --- WINDOWS SUBPROCESS NO-CONSOLE FLAG ---
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

# --- AUTOMATED DEPENDENCY SETUP STATE ---
SETUP_STATE = {
    "stage": "Initializing...",
    "progress": 0,
    "log": "Starting environment checks...\n",
    "done": False,
    "error": False,
    "cli_path": None
}

def create_windows_shortcuts():
    """Silently creates Desktop and Start Menu shortcuts on Windows."""
    if os.name != 'nt':
        return 
        
    # Only create shortcuts if running as a compiled .exe
    if not getattr(sys, 'frozen', False):
        return 

    exe_path = sys.executable
    
    # Use VBScript to reliably find the Desktop and Start Menu across all Windows languages
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
        with open(vbs_path, 'w') as f:
            f.write(vbs_script)
        # Run the script silently
        subprocess.run(['cscript.exe', '//Nologo', vbs_path], creationflags=CREATE_NO_WINDOW)
        os.remove(vbs_path)
    except Exception as e:
        pass # Fail silently to prevent crashing the installer

def download_file_with_progress(url, dest):
    def hook(count, block_size, total_size):
        if total_size > 0:
            percent = int(count * block_size * 100 / total_size)
            SETUP_STATE["progress"] = min(100, percent)
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    urllib.request.urlretrieve(url, dest, reporthook=hook, context=ctx)

def setup_environment_thread():
    try:
        SETUP_STATE["stage"] = "Checking for Arduino CLI..."
        cli_global = shutil.which("arduino-cli")
        
        LOCAL_APP_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'WiFeY_Studio')
        os.makedirs(LOCAL_APP_DIR, exist_ok=True)
        LOCAL_CLI_PATH = os.path.join(LOCAL_APP_DIR, 'arduino-cli.exe')
        
        if cli_global:
            SETUP_STATE["log"] += f"Found global Arduino CLI: {cli_global}\n"
            cli_executable = cli_global
        elif os.path.exists(LOCAL_CLI_PATH):
            SETUP_STATE["log"] += f"Found local Arduino CLI: {LOCAL_CLI_PATH}\n"
            cli_executable = LOCAL_CLI_PATH
        else:
            SETUP_STATE["stage"] = "Downloading Arduino CLI (20MB)..."
            SETUP_STATE["log"] += "Downloading official Windows 64-bit release...\n"
            zip_path = os.path.join(LOCAL_APP_DIR, "cli.zip")
            download_file_with_progress("https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip", zip_path)
            
            SETUP_STATE["stage"] = "Extracting files..."
            SETUP_STATE["progress"] = 0
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(LOCAL_APP_DIR)
            os.remove(zip_path)
            cli_executable = LOCAL_CLI_PATH
            SETUP_STATE["log"] += "Extraction complete.\n"
            
        SETUP_STATE["cli_path"] = cli_executable
        SETUP_STATE["progress"] = 100
        
        SETUP_STATE["stage"] = "Configuring ESP32 Board Manager..."
        subprocess.run([cli_executable, "config", "init"], capture_output=True, creationflags=CREATE_NO_WINDOW) 
        subprocess.run([cli_executable, "config", "set", "board_manager.additional_urls", "https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        
        SETUP_STATE["stage"] = "Updating Arduino Core Index..."
        subprocess.run([cli_executable, "core", "update-index"], capture_output=True, creationflags=CREATE_NO_WINDOW)
        
        check_core = subprocess.run([cli_executable, "core", "list"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if "esp32:esp32" not in check_core.stdout:
            SETUP_STATE["stage"] = "Installing ESP32 Core (This takes a few minutes!)..."
            SETUP_STATE["log"] += "Downloading Xtensa toolchains and ESP32 board files...\n"
            process = subprocess.Popen([cli_executable, "core", "install", "esp32:esp32"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=CREATE_NO_WINDOW)
            for line in process.stdout:
                SETUP_STATE["log"] += line
            process.wait()
            
        check_lib = subprocess.run([cli_executable, "lib", "list"], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        if "ArduinoJson" not in check_lib.stdout:
            SETUP_STATE["stage"] = "Installing ArduinoJson..."
            SETUP_STATE["log"] += "Fetching ArduinoJson library...\n"
            process = subprocess.Popen([cli_executable, "lib", "install", "ArduinoJson"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=CREATE_NO_WINDOW)
            for line in process.stdout:
                SETUP_STATE["log"] += line
            process.wait()
            
        # --- NEW SHORTCUT CREATION CALL ---
        SETUP_STATE["stage"] = "Creating App Shortcuts..."
        create_windows_shortcuts()
        
        SETUP_STATE["stage"] = "Setup Complete!"
        SETUP_STATE["progress"] = 100
        SETUP_STATE["log"] += "\nAll dependencies verified! Launching WiFeY Studio...\n"
        time.sleep(1.5)
        SETUP_STATE["done"] = True
        
    except Exception as e:
        SETUP_STATE["error"] = True
        SETUP_STATE["log"] += f"\nERROR: {str(e)}"

# --- PERMANENT NGROK DOMAIN ---
NGROK_STATIC_DOMAIN = os.getenv("NGROK_STATIC_DOMAIN", "christie-noncarnivorous-pulvinately.ngrok-free.dev")

try:
    from pyngrok import ngrok
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true" and not getattr(sys, 'frozen', False):
        ngrok_tunnel = ngrok.connect(5000, domain=NGROK_STATIC_DOMAIN, bind_tls=True)
        NGROK_URL = ngrok_tunnel.public_url
    else:
        NGROK_URL = f"https://{NGROK_STATIC_DOMAIN}"
except Exception as e:
    NGROK_URL = "http://127.0.0.1:5000"

app = Flask(__name__)

# --- CONFIGURATION ---
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY = os.getenv("R2_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("R2_SECRET_KEY")
R2_BUCKET = os.getenv("R2_BUCKET", "wifey-firmware")

CACHE_DIR = os.path.join(os.getcwd(), "build_cache")
CORE_CACHE_DIR = os.path.join(os.getcwd(), "core_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(CORE_CACHE_DIR, exist_ok=True)

COMPILED_CACHE = {}
for filename in os.listdir(CACHE_DIR):
    if filename.endswith(".bin"):
        v_id = filename.replace(".bin", "")
        COMPILED_CACHE[v_id] = os.path.join(CACHE_DIR, filename)

DEVICE_DATABASE = {
    "node_1": {"auth_token": "secret_abc123"},
    "node_2": {"auth_token": "secret_abc123"}
}
VERSION_MANIFEST = {}

SERIAL_STATE = {
    "port": None,
    "serial_obj": None,
    "is_running": False
}

RE_INCLUDES = re.compile(r'#include\s*[<"](WiFi\.h|WiFiClientSecure\.h|PubSubClient\.h|HTTPClient\.h|HTTPUpdate\.h|ArduinoJson\.h)[>"]')
RE_CONFIGS = re.compile(r'^\s*(const\s+)?char\s*\*\s*(ssid|password|api_url|auth_token|current_version)\s*=.*;', re.MULTILINE)
RE_WIFI_BEGIN = re.compile(r'^\s*WiFi\.begin\(.*\);', re.MULTILINE)
RE_WIFI_MODE = re.compile(r'^\s*WiFi\.mode\(.*\);', re.MULTILINE)
RE_SERIAL_BEGIN = re.compile(r'^\s*Serial\.begin\(.*\);', re.MULTILINE)
RE_DELAY = re.compile(r'\bdelay\s*\(')

def get_s3_client():
    return boto3.client(
        "s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version="s3v4")
    )

def close_serial_connection():
    SERIAL_STATE["is_running"] = False
    if SERIAL_STATE["serial_obj"] and SERIAL_STATE["serial_obj"].is_open:
        try:
            SERIAL_STATE["serial_obj"].setDTR(False)
            SERIAL_STATE["serial_obj"].setRTS(False)
            SERIAL_STATE["serial_obj"].close()
        except:
            pass
    SERIAL_STATE["serial_obj"] = None

MASTER_CPP_TEMPLATE = """#include <WiFi.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>

// --- User Globals & Includes ---
{USER_GLOBALS}
// -------------------------------

const char* ssid = "{WIFI_SSID}";
const char* password = "{WIFI_PASS}";
const char* api_url = "{API_BASE_URL}/api/manifest?device={DEVICE_ID}";
const char* auth_token = "{AUTH_TOKEN}";
const char* current_version = "{CURRENT_VERSION}";

void success_flash() {
  pinMode(2, OUTPUT);
  for(int i=0; i<10; i++) {
    digitalWrite(2, HIGH); delay(40);
    digitalWrite(2, LOW); delay(40);
  }
}

void upgrade_detected_flash() {
  pinMode(2, OUTPUT);
  for(int i=0; i<3; i++) {
    digitalWrite(2, HIGH); delay(150);
    digitalWrite(2, LOW); delay(150);
  }
}

void smartDelay(unsigned long ms) {
  unsigned long start = millis();
  while (millis() - start < ms) { yield(); }
}

void setup_wifi() {
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);

  WiFi.disconnect(true);
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, password);

  pinMode(2, OUTPUT);
  int attempts = 0;
  
  while (WiFi.status() != WL_CONNECTED) {
    for(int i=0; i<33; i++) {
      digitalWrite(2, HIGH); delay(3);
      digitalWrite(2, LOW); delay(12);
    }
    Serial.print(".");
    attempts++;
    if (attempts > 40) { 
      Serial.println("\\nFailed to connect. Rebooting...");
      {WIFI_FAIL_ACTION}
    }
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\\nWiFi connected!");
    digitalWrite(2, HIGH); delay(80);
    digitalWrite(2, LOW); delay(80);
    digitalWrite(2, HIGH); delay(80);
    digitalWrite(2, LOW);
  }
}

void check_for_updates() {
  if (WiFi.status() != WL_CONNECTED) return;
  delay(1000); 
  
  Serial.println("[*] Checking WiFeY API over the Internet...");
  WiFiClientSecure secureClient;
  secureClient.setInsecure(); 
  secureClient.setTimeout(15000);
  
  HTTPClient http;
  http.setTimeout(15000);
  http.begin(secureClient, api_url);
  http.addHeader("ngrok-skip-browser-warning", "true");
  http.addHeader("Authorization", "Bearer " + String(auth_token));
  
  int httpCode = http.GET();
  if (httpCode == 200) {
    String payload = http.getString();
    StaticJsonDocument<512> doc;
    DeserializationError error = deserializeJson(doc, payload);
    
    if (!error) {
      String target_version = doc["target_version"].as<String>();
      String download_url = doc["download_url"].as<String>();
      
      Serial.printf("Current Version: %s\\n", current_version);
      Serial.printf("Target Version: %s\\n", target_version.c_str());
      
      if (target_version != current_version && target_version != "null") {
        Serial.println("[*] New version found! Starting secure OTA download...");
        upgrade_detected_flash(); 
        
        httpUpdate.rebootOnUpdate(false); 
        t_httpUpdate_return ret = httpUpdate.update(secureClient, download_url);
        
        if (ret == HTTP_UPDATE_FAILED) {
          Serial.printf("❌ Update Failed. Error: %s\\n", httpUpdate.getLastErrorString().c_str());
        } else if (ret == HTTP_UPDATE_OK) {
          Serial.println("[✓] Update Successful! Rebooting...");
          success_flash(); 
          ESP.restart();   
        }
      } else {
        Serial.println("[✓] Firmware is up to date.");
      }
    } else {
      Serial.println("❌ JSON parsing failed.");
    }
  } else {
    Serial.printf("❌ API Check failed, HTTP Code: %d\\n", httpCode);
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  setup_wifi();
  check_for_updates(); 
  {USER_SETUP}
}

void loop() {
  {USER_LOOP}
}
"""

def extract_block(code, func_name):
    pattern = r'void\s+' + func_name + r'\s*\(\s*\)\s*\{'
    match = re.search(pattern, code)
    if not match: return "", code
    start_idx = match.end() - 1
    brace_count = 0
    end_idx = -1
    for i in range(start_idx, len(code)):
        if code[i] == '{': brace_count += 1
        elif code[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                end_idx = i
                break
    if end_idx != -1:
        body = code[start_idx+1:end_idx].strip()
        remaining = code[:match.start()] + code[end_idx+1:]
        return body, remaining
    return "", code

def clean_duplicates_and_delays(code):
    code = RE_INCLUDES.sub('', code)
    code = RE_CONFIGS.sub('// Removed duplicate config', code)
    code = RE_WIFI_BEGIN.sub('// WiFi.begin handled by template', code)
    code = RE_WIFI_MODE.sub('// WiFi.mode handled by template', code)
    code = RE_SERIAL_BEGIN.sub('// Serial.begin handled by template', code)
    code = RE_DELAY.sub('smartDelay(', code)
    return code.strip()

def generate_wrapped_code(ssid, passw, api_url, topic, user_code, version_id="v_base_001"):
    uses_wifi = bool(re.search(r'WiFi\.', user_code) or re.search(r'#include\s*[<"]WiFi\.h[>"]', user_code))
    user_loop, remaining = extract_block(user_code, "loop")
    user_setup, user_globals = extract_block(remaining, "setup")
    
    user_globals = clean_duplicates_and_delays(user_globals)
    user_setup = clean_duplicates_and_delays(user_setup)
    user_loop = clean_duplicates_and_delays(user_loop)
    
    wifi_fail_action = "ESP.restart();" if uses_wifi else "break; // Offline fallback"
    
    parts = topic.split('/')
    device_id = parts[2] if len(parts) >= 3 else "node_1"
    
    final_code = MASTER_CPP_TEMPLATE
    final_code = final_code.replace('{WIFI_SSID}', ssid)
    final_code = final_code.replace('{WIFI_PASS}', passw)
    final_code = final_code.replace('{API_BASE_URL}', api_url)
    final_code = final_code.replace('{DEVICE_ID}', device_id)
    final_code = final_code.replace('{AUTH_TOKEN}', "secret_abc123")
    final_code = final_code.replace('{CURRENT_VERSION}', version_id)
    final_code = final_code.replace('{USER_GLOBALS}', user_globals)
    final_code = final_code.replace('{USER_SETUP}', user_setup)
    final_code = final_code.replace('{USER_LOOP}', user_loop)
    final_code = final_code.replace('{WIFI_FAIL_ACTION}', wifi_fail_action)
    return final_code

# --- SPLASH SCREEN LOADER HTML ---
SETUP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>WiFeY Studio Setup</title>
    <style>
        body { background: #0f172a; color: white; font-family: 'Segoe UI', sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100vh; margin: 0; zoom: 1.3;}
        .loader { border: 4px solid #334155; border-top: 4px solid #3b82f6; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin-bottom: 20px;}
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .progress-container { width: 80%; max-width: 400px; background: #1e293b; border-radius: 8px; overflow: hidden; margin-bottom: 10px; border: 1px solid #334155;}
        .progress-bar { height: 10px; background: #3b82f6; width: 0%; transition: 0.3s; }
        .log-box { background: #000; color: #10b981; padding: 15px; font-family: monospace; width: 80%; max-width: 600px; height: 180px; overflow-y: auto; font-size: 12px; border-radius: 6px; border: 1px solid #334155; white-space: pre-wrap; margin-bottom: 30px;}
        .glow { text-shadow: 0 0 10px rgba(59, 130, 246, 0.5); }
    </style>
</head>
<body>
    <h2 style="margin-bottom: 5px; color: #3b82f6;" class="glow">WiFeY Studio Setup</h2>
    <p style="color: #94a3b8; font-size: 13px; margin-top: 0; margin-bottom: 30px;">Downloading core compilers. This only happens on the very first launch.</p>
    
    <div class="loader" id="spinner"></div>
    <div id="status-text" style="font-weight: bold; margin-bottom: 10px;">Initializing...</div>
    
    <div class="progress-container"><div class="progress-bar" id="pbar"></div></div>
    
    <div class="log-box" id="logs">Starting...</div>

    <script>
        setInterval(() => {
            fetch('/api/setup_status').then(r=>r.json()).then(data => {
                document.getElementById('status-text').innerText = data.stage;
                document.getElementById('pbar').style.width = data.progress + '%';
                
                let lg = document.getElementById('logs');
                if (lg.innerText.length !== data.log.length) {
                    lg.innerText = data.log;
                    lg.scrollTop = lg.scrollHeight;
                }

                if (data.done) {
                    window.location.reload(); 
                }
                if (data.error) {
                    document.getElementById('status-text').innerText = "Setup Failed! Check Logs.";
                    document.getElementById('status-text').style.color = "#ef4444";
                    document.getElementById('spinner').style.display = "none";
                    document.getElementById('pbar').style.background = "#ef4444";
                }
            });
        }, 800);
    </script>
</body>
</html>
"""

# --- IDE HTML ---
IDE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>WiFeY Studio | Cloud OTA Compiler</title>
    <style>
        :root { --bg: #0f172a; --panel: #1e293b; --text: #f8fafc; --accent: #3b82f6; --accent-hover: #2563eb; --warning: #ef4444; --success: #10b981;}
        body { font-family: 'Segoe UI', sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 20px; display: flex; justify-content: center; zoom: 1.3; }
        .dashboard { display: grid; grid-template-columns: 320px 1fr; gap: 20px; max-width: 1400px; width: 100%; }
        .panel { background: var(--panel); padding: 25px; border-radius: 12px; box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5); }
        h2 { margin-top: 0; font-size: 1.5rem; border-bottom: 1px solid #334155; padding-bottom: 10px; color: var(--accent); }
        label { display: block; font-size: 0.85rem; font-weight: bold; margin: 15px 0 5px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px;}
        input, select { width: 100%; padding: 10px; background: #0f172a; border: 1px solid #334155; color: white; border-radius: 6px; box-sizing: border-box; font-size: 14px; transition: 0.3s;}
        input:focus, select:focus { border-color: var(--accent); outline: none; }
        .flex-row { display: flex; gap: 8px; align-items: center; }
        .btn-icon { background: #334155; color: white; border: none; border-radius: 6px; cursor: pointer; padding: 0 12px; height: 38px; font-size: 16px; display: flex; align-items: center; justify-content: center;}
        .btn-icon:hover { background: #475569; }
        
        .custom-select-container { position: relative; width: 100%; font-size: 14px; }
        .custom-select-trigger { background: #0f172a; border: 1px solid #334155; color: white; padding: 10px; border-radius: 6px; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
        .custom-select-options { position: absolute; top: 100%; left: 0; right: 0; background: #1e293b; border: 1px solid #334155; border-radius: 6px; margin-top: 5px; z-index: 10; display: none; max-height: 250px; overflow-y: auto; box-shadow: 0 10px 15px rgba(0,0,0,0.5); }
        .custom-select-options.open { display: block; }
        .custom-option { display: flex; justify-content: space-between; align-items: center; transition: background 0.2s; border-bottom: 1px solid #334155; }
        .option-text { flex: 1; padding: 12px 10px; cursor: pointer; }
        .option-text:hover { background: #334155; }
        .delete-icon { color: var(--warning); font-size: 18px; padding: 12px 15px; cursor: pointer; font-weight: bold; }
        .add-new { color: var(--accent); font-weight: bold; justify-content: center; padding: 12px; cursor: pointer; }
        
        .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); backdrop-filter: blur(3px); display: none; justify-content: center; align-items: center; z-index: 100; }
        .modal-box { background: var(--panel); padding: 25px; border-radius: 10px; width: 90%; max-width: 500px; border: 1px solid #334155; }
        .modal-box h3 { margin-top: 0; color: white; }
        .modal-buttons { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
        
        .tabs { display: flex; border-bottom: 2px solid #334155; margin-bottom: 15px; gap: 5px; }
        .tab-btn { background: transparent; color: #94a3b8; border: none; padding: 10px 20px; cursor: pointer; font-weight: bold; border-radius: 6px 6px 0 0; }
        .tab-btn.active { background: var(--accent); color: white; }
        .editor-header { display: flex; justify-content: space-between; align-items: flex-end; }
        .btn-small { background: #334155; color: white; border: none; padding: 8px 15px; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: bold; margin-bottom: 15px;}
        textarea { width: 100%; height: 500px; padding: 15px; background: #0f172a; border: 1px solid #334155; color: #ffffff; border-radius: 6px; box-sizing: border-box; font-family: 'Courier New', monospace; font-size: 14px; resize: vertical; }
        textarea:focus { border-color: var(--accent); outline: none; }
        
        .btn-group { display: flex; flex-direction: column; gap: 10px; margin-top: 30px; }
        button.action { padding: 12px; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 14px; display: flex; justify-content: center; gap: 8px;}
        .btn-primary { background: var(--accent); color: white; }
        .btn-outline { background: transparent; border: 1px solid #10b981; color: #10b981; text-decoration: none; align-items: center; }
        .btn-outline:hover { background: rgba(16, 185, 129, 0.1); }
        
        #terminal { margin-top: 20px; background: #000; color: var(--success); padding: 15px; border-radius: 6px; font-family: monospace; white-space: pre-wrap; font-size: 13px; min-height: 120px; max-height: 300px; overflow-y: auto; border: 1px solid #334155;}
        .ngrok-badge { background: #10b981; color: #000; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; margin-left: 10px; }
    </style>
</head>
<body>
    <div class="dashboard">
        <div class="panel">
            <h2>WiFeY Studio <span class="ngrok-badge">ONLINE</span></h2>
            <label>Wi-Fi SSID</label>
            <input type="text" id="ssid" placeholder="Network Name">
            <label>Wi-Fi Password</label>
            <div class="flex-row">
                <input type="password" id="pass" placeholder="********">
                <button class="btn-icon" onclick="togglePass()">👁️</button>
            </div>
            
            <label>Auto-Ngrok URL (Don't Change)</label>
            <input type="text" id="api_url" value="{{ ngrok_url }}" readonly style="color: #94a3b8; background: #1e293b;">
            
            <label>Target Fleet / Device Topic</label>
            <div class="custom-select-container" id="custom-select-container">
                <div class="custom-select-trigger" onclick="toggleDropdown(event)">
                    <span id="selected_topic_text">Select a topic...</span>
                    <span>▼</span>
                </div>
                <div class="custom-select-options" id="custom_options_list"></div>
            </div>
            <input type="hidden" id="topic_val">
            
            <div class="btn-group">
                <button class="action btn-primary" id="btn-compile" onclick="compileAndPublish()">🚀 OTA Compile & Update</button>
                <a href="/provision" class="action btn-outline">🔌 Open USB Provisioning Tool</a>
            </div>
        </div>
        
        <div class="panel">
            <div class="editor-header">
                <div class="tabs">
                    <button class="tab-btn active" id="tab-input" onclick="switchTab('input')">Raw Input (.ino)</button>
                    <button class="tab-btn" id="tab-wrapped" onclick="switchTab('wrapped')">Preview Wrapped Code</button>
                    <button class="tab-btn" id="tab-serial" onclick="switchTab('serial')" style="color: #3b82f6;">🔌 Serial Monitor</button>
                </div>
                <button class="btn-small" onclick="stripComments()" id="btn-strip">🧹 Strip Comments</button>
            </div>
            
            <textarea id="input_code" spellcheck="false">
int ledPin = 2;
void setup() {
  pinMode(ledPin, OUTPUT);
  Serial.println("Hello from WiFeY OTA Architecture!");
}
void loop() {
  digitalWrite(ledPin, HIGH);
  delay(1000);
  digitalWrite(ledPin, LOW);
  delay(1000);
}
</textarea>
            <textarea id="wrapped_code" spellcheck="false" style="display: none;"></textarea>
            
            <div id="view-serial" style="display: none;">
                <div style="display: flex; gap: 10px; margin-bottom: 15px;">
                    <button class="btn-icon" onclick="loadSerialPorts()" title="Refresh Ports">🔄</button>
                    <select id="serial_port"><option value="">Loading...</option></select>
                    <select id="serial_baud" style="width: 120px;">
                        <option value="9600">9600</option>
                        <option value="115200" selected>115200</option>
                    </select>
                    <button class="btn-small btn-primary" id="btn-serial-connect" onclick="connectSerial()" style="margin-bottom:0;">Connect</button>
                    <button class="btn-small" id="btn-serial-disconnect" onclick="disconnectSerial()" style="display:none; background:#ef4444; margin-bottom:0;">Disconnect</button>
                    <button class="btn-small" onclick="document.getElementById('serial_out').innerHTML=''" style="margin-bottom:0;">Clear Log</button>
                </div>
                <div id="serial_out" style="background:#000; color:#10b981; padding:15px; border-radius:6px; font-family:monospace; height:435px; overflow-y:auto; white-space:pre-wrap; border:1px solid #334155;"></div>
            </div>

            <div id="terminal">Waiting for commands... Smart Build Cache is active.</div>
        </div>
    </div>
    
    <div class="modal-overlay" id="modal-add" onclick="closeModalOnOutsideClick(event, 'modal-add')">
        <div class="modal-box">
            <h3>Add New Topic</h3>
            <input type="text" id="new_topic_input" placeholder="e.g. Ramanand/devices/node_2/update">
            <div class="modal-buttons">
                <button class="btn-small" style="background: transparent; border: 1px solid #334155; margin-bottom: 0;" onclick="closeModal('modal-add')">Cancel</button>
                <button class="btn-small btn-primary" style="margin-bottom: 0;" onclick="confirmAddTopic()">Add Topic</button>
            </div>
        </div>
    </div>
    <div class="modal-overlay" id="modal-delete" onclick="closeModalOnOutsideClick(event, 'modal-delete')">
        <div class="modal-box">
            <h3>Remove Topic</h3>
            <p style="color: #94a3b8; font-size: 14px;">Are you sure you want to remove <strong id="delete_topic_name" style="color: white;"></strong> from the list?</p>
            <div class="modal-buttons">
                <button class="btn-small" style="background: transparent; border: 1px solid #334155; margin-bottom: 0;" onclick="closeModal('modal-delete')">Cancel</button>
                <button class="btn-small" style="background: #ef4444; margin-bottom: 0;" onclick="confirmDeleteTopic()">Remove</button>
            </div>
        </div>
    </div>

    <script>
        let activeTab = 'input';
        let topicToDelete = "";
        let serialEventSource = null;
        
        window.onload = () => {
            if(localStorage.getItem('wifi_ssid')) document.getElementById('ssid').value = localStorage.getItem('wifi_ssid');
            if(localStorage.getItem('wifi_pass')) document.getElementById('pass').value = localStorage.getItem('wifi_pass');
            loadTopics();
            loadSerialPorts();
        }
        
        function switchTab(tab) {
            activeTab = tab;
            document.getElementById('tab-input').classList.toggle('active', tab === 'input');
            document.getElementById('tab-wrapped').classList.toggle('active', tab === 'wrapped');
            document.getElementById('tab-serial').classList.toggle('active', tab === 'serial');
            
            document.getElementById('input_code').style.display = tab === 'input' ? 'block' : 'none';
            document.getElementById('wrapped_code').style.display = tab === 'wrapped' ? 'block' : 'none';
            document.getElementById('view-serial').style.display = tab === 'serial' ? 'block' : 'none';
            document.getElementById('btn-strip').style.display = tab === 'serial' ? 'none' : 'block';
            
            if (tab === 'wrapped') fetchPreview();
            if (tab === 'serial') loadSerialPorts();
        }
        
        function loadSerialPorts() {
            fetch('/api/ports').then(r=>r.json()).then(ports => {
                let sel = document.getElementById('serial_port');
                sel.innerHTML = ports.length ? '' : '<option value="">No USB Devices Found</option>';
                ports.forEach(p => { sel.innerHTML += `<option value="${p.device}">${p.device} - ${p.desc}</option>`; });
            });
        }
        
        function connectSerial() {
            const port = document.getElementById('serial_port').value;
            const baud = document.getElementById('serial_baud').value;
            if(!port) return alert("Please select a COM port first.");
            
            fetch('/api/serial/connect', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({port: port, baud: baud})
            }).then(r=>r.json()).then(res => {
                if(res.error) { alert("Failed to connect: " + res.error); return; }
                
                document.getElementById('btn-serial-connect').style.display = 'none';
                document.getElementById('btn-serial-disconnect').style.display = 'inline-block';
                const out = document.getElementById('serial_out');
                out.innerHTML += `\\n[*] -------------------------------------\\n[*] Connected to ${port} at ${baud} baud\\n[*] -------------------------------------\\n`;
                
                serialEventSource = new EventSource('/api/serial/stream');
                serialEventSource.onmessage = function(e) {
                    const data = JSON.parse(e.data);
                    out.appendChild(document.createTextNode(data.text));
                    out.scrollTop = out.scrollHeight;
                };
            });
        }
        
        function disconnectSerial() {
            fetch('/api/serial/disconnect', {method: 'POST'}).then(() => {
                if(serialEventSource) { serialEventSource.close(); serialEventSource = null; }
                document.getElementById('btn-serial-connect').style.display = 'inline-block';
                document.getElementById('btn-serial-disconnect').style.display = 'none';
                document.getElementById('serial_out').innerHTML += `\\n[*] Connection Closed.\\n`;
            });
        }

        function loadTopics() {
            const list = document.getElementById('custom_options_list');
            let topics = JSON.parse(localStorage.getItem('wifey_topics')) || ["Ramanand/devices/node_1/update", "Ramanand/fleet/update"];
            list.innerHTML = '';
            topics.forEach(t => {
                let div = document.createElement('div');
                div.className = 'custom-option';
                let deleteHTML = topics.length > 1 ? `<div class="delete-icon" onclick="openDeleteModal(event, '${t}')">&times;</div>` : '';
                div.innerHTML = `<div class="option-text" onclick="selectTopic('${t}')">${t}</div> ${deleteHTML}`;
                list.appendChild(div);
            });
            let addDiv = document.createElement('div');
            addDiv.className = 'custom-option add-new';
            addDiv.innerText = '+ Add New Topic';
            addDiv.onclick = openAddModal;
            list.appendChild(addDiv);
            let lastTopic = localStorage.getItem('last_topic');
            if(!lastTopic || !topics.includes(lastTopic)) lastTopic = topics[0];
            if(lastTopic) selectTopic(lastTopic);
        }
        function toggleDropdown(e) { document.getElementById('custom_options_list').classList.toggle('open'); e.stopPropagation(); }
        document.addEventListener('click', function(e) {
            let container = document.getElementById('custom-select-container');
            if (!container.contains(e.target)) document.getElementById('custom_options_list').classList.remove('open');
        });
        function selectTopic(t) {
            document.getElementById('selected_topic_text').innerText = t;
            document.getElementById('topic_val').value = t;
            localStorage.setItem('last_topic', t);
            document.getElementById('custom_options_list').classList.remove('open');
        }
        function openAddModal() { document.getElementById('custom_options_list').classList.remove('open'); document.getElementById('modal-add').style.display = 'flex'; }
        function confirmAddTopic() { let nt = document.getElementById('new_topic_input').value.trim(); if(nt !== "") { let topics = JSON.parse(localStorage.getItem('wifey_topics')) || []; if(!topics.includes(nt)) { topics.push(nt); localStorage.setItem('wifey_topics', JSON.stringify(topics)); loadTopics(); selectTopic(nt); } } closeModal('modal-add'); }
        function openDeleteModal(e, t) { e.stopPropagation(); topicToDelete = t; document.getElementById('custom_options_list').classList.remove('open'); document.getElementById('delete_topic_name').innerText = t; document.getElementById('modal-delete').style.display = 'flex'; }
        function confirmDeleteTopic() { let topics = JSON.parse(localStorage.getItem('wifey_topics')); topics = topics.filter(t => t !== topicToDelete); localStorage.setItem('wifey_topics', JSON.stringify(topics)); loadTopics(); closeModal('modal-delete'); }
        function closeModal(id) { document.getElementById(id).style.display = 'none'; }
        function closeModalOnOutsideClick(e, id) { if (e.target.id === id) closeModal(id); }
        function togglePass() { const pwd = document.getElementById('pass'); pwd.type = pwd.type === "password" ? "text" : "password"; }
        
        function stripComments() {
            const ta = activeTab === 'input' ? document.getElementById('input_code') : document.getElementById('wrapped_code');
            let code = ta.value;
            code = code.replace(/\\/\\*[\\s\\S]*?\\*\\//g, ''); 
            code = code.replace(/([^:])\\/\\/.*/g, '$1'); 
            code = code.replace(/^\\/\\/.*/gm, ''); 
            code = code.replace(/^\\s*[\\r\\n]/gm, '');
            ta.value = code;
        }
        
        function fetchPreview() {
            document.getElementById('wrapped_code').value = "Generating smart preview...";
            const data = {
                ssid: document.getElementById('ssid').value, pass: document.getElementById('pass').value,
                api_url: document.getElementById('api_url').value.replace(/\\/$/, ''), topic: document.getElementById('topic_val').value,
                code: document.getElementById('input_code').value
            };
            fetch('/preview', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }, 
                body: JSON.stringify(data) 
            })
            .then(res => res.json()).then(data => document.getElementById('wrapped_code').value = data.code)
            .catch(err => document.getElementById('wrapped_code').value = "// Preview failed: " + err);
        }
        
        function compileAndPublish() {
            const topicVal = document.getElementById('topic_val').value;
            localStorage.setItem('wifi_ssid', document.getElementById('ssid').value);
            localStorage.setItem('wifi_pass', document.getElementById('pass').value);
            localStorage.setItem('last_topic', topicVal);

            const data = {
                ssid: document.getElementById('ssid').value, pass: document.getElementById('pass').value,
                api_url: document.getElementById('api_url').value.replace(/\\/$/, ''), topic: topicVal,
                code: activeTab === 'wrapped' ? document.getElementById('wrapped_code').value : document.getElementById('input_code').value, 
                is_wrapped: activeTab === 'wrapped'
            };
            const term = document.getElementById('terminal');
            term.innerText = "[*] Analyzing code hash for cache hit...";
            document.getElementById('btn-compile').style.opacity = '0.5';
            document.getElementById('btn-compile').style.pointerEvents = 'none';
            
            fetch('/compile', { 
                method: 'POST', 
                headers: { 'Content-Type': 'application/json', 'ngrok-skip-browser-warning': 'true' }, 
                body: JSON.stringify(data) 
            })
            .then(res => res.json()).then(resData => { term.innerText = resData.logs; document.getElementById('btn-compile').style.opacity = '1'; document.getElementById('btn-compile').style.pointerEvents = 'auto'; })
            .catch(err => { term.innerText = "❌ System Error: " + err; document.getElementById('btn-compile').style.opacity = '1'; document.getElementById('btn-compile').style.pointerEvents = 'auto'; });
        }
    </script>
</body>
</html>
"""

# --- ISOLATED PROVISIONING HTML ---
PROVISION_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>WiFeY Global USB Provisioning</title>
    <style>
        body { font-family: Arial, sans-serif; background: #0f172a; margin: 0; padding: 20px; color: #f8fafc; zoom: 1.3; }
        .container { max-width: 600px; margin: auto; background: #1e293b; padding: 30px; border-radius: 8px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); border: 1px solid #334155; }
        h2 { border-bottom: 1px solid #334155; padding-bottom: 10px; color: #3b82f6; margin-top: 0; }
        label { font-weight: bold; display: block; margin-top: 15px; font-size: 14px; color: #94a3b8; }
        input, select { width: 100%; padding: 10px; margin-top: 5px; background: #0f172a; border: 1px solid #334155; color: white; border-radius: 4px; box-sizing: border-box; }
        button { background: #3b82f6; color: white; border: none; padding: 12px 20px; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; margin-top: 20px; font-weight: bold; }
        button:hover { background: #2563eb; }
        #output { margin-top: 20px; background: #000; color: #10b981; padding: 15px; border-radius: 4px; font-family: monospace; white-space: pre-wrap; display: none; border: 1px solid #334155;}
        .btn-secondary { background: transparent; border: 1px solid #3b82f6; color: #3b82f6; text-decoration: none; display: block; text-align: center; padding: 12px 20px; border-radius: 4px; margin-top: 10px; font-weight: bold; font-size: 16px; transition: 0.2s; box-sizing: border-box; }
        .btn-secondary:hover { background: rgba(59, 130, 246, 0.1); }
    </style>
</head>
<body>
    <div class="container">
        <h2>🌐 WiFeY USB Provisioning Tool</h2>
        <p style="font-size: 13px; color: #94a3b8; margin-top: -5px;">Uploads the secure bootloader to new devices.</p>
        
        <label>Select COM Port:</label>
        <select id="port">
            {% for p in ports %}
            <option value="{{ p.device }}">{{ p.device }} - {{ p.description }}</option>
            {% endfor %}
        </select>

        <label>Wi-Fi SSID (Use Hotspot to bypass Captive Portals):</label>
        <input type="text" id="ssid" placeholder="Network Name">

        <label>Wi-Fi Password:</label>
        <input type="password" id="pass" placeholder="Password">

        <label>Public API URL (From Ngrok):</label>
        <input type="text" id="api_url" value="{{ ngrok_url }}" readonly style="color: #94a3b8;">

        <label>Device ID (e.g., node_1, node_2):</label>
        <input type="text" id="device_id" placeholder="node_1">
        
        <label>Secret Auth Token (Gatekeeper Key):</label>
        <input type="text" id="auth_token" value="secret_abc123" readonly style="color: #94a3b8;">

        <button id="btn-flash" onclick="flashDevice()">Compile & Provision via USB</button>
        <a href="/" class="btn-secondary">⬅️ Back to Main IDE</a>
        <div id="output"></div>
    </div>

    <script>
        window.onload = () => {
            ['ssid', 'pass', 'device_id'].forEach(id => {
                if(localStorage.getItem('prov_v3_' + id)) {
                    document.getElementById(id).value = localStorage.getItem('prov_v3_' + id);
                }
            });
        }

        function flashDevice() {
            const data = {
                port: document.getElementById('port').value,
                ssid: document.getElementById('ssid').value,
                pass: document.getElementById('pass').value,
                api_url: document.getElementById('api_url').value.replace(/\\/$/, ''),
                device_id: document.getElementById('device_id').value,
                auth_token: document.getElementById('auth_token').value
            };

            ['ssid', 'pass', 'device_id'].forEach(key => localStorage.setItem('prov_v3_' + key, data[key]));

            const out = document.getElementById('output');
            const btn = document.getElementById('btn-flash');
            out.style.display = 'block';
            out.innerText = "Flashing in progress... Applying Upload Speed fix. Check terminal logs.";
            btn.style.opacity = '0.5'; btn.style.pointerEvents = 'none';

            fetch('/flash', {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'ngrok-skip-browser-warning': 'true'
                },
                body: JSON.stringify(data)
            }).then(res => res.json()).then(result => {
                out.innerText = result.logs;
                btn.style.opacity = '1'; btn.style.pointerEvents = 'auto';
            }).catch(err => {
                out.innerText = "Error: " + err;
                btn.style.opacity = '1'; btn.style.pointerEvents = 'auto';
            });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    if not SETUP_STATE["done"]:
        return render_template_string(SETUP_HTML)
    return render_template_string(IDE_HTML, ngrok_url=NGROK_URL)

@app.route('/api/setup_status')
def setup_status():
    return jsonify(SETUP_STATE)

@app.route('/provision')
def provision_page():
    ports = serial.tools.list_ports.comports()
    return render_template_string(PROVISION_HTML, ports=ports, ngrok_url=NGROK_URL)

@app.route('/api/ports', methods=['GET'])
def get_ports():
    ports = [{"device": p.device, "desc": p.description} for p in serial.tools.list_ports.comports()]
    return jsonify(ports)

@app.route('/api/serial/connect', methods=['POST'])
def serial_connect():
    data = request.json
    port = data.get('port')
    baud = int(data.get('baud', 115200))
    
    close_serial_connection() 
    try:
        SERIAL_STATE["serial_obj"] = serial.Serial(port, baud, timeout=1)
        SERIAL_STATE["port"] = port
        SERIAL_STATE["is_running"] = True
        return jsonify({"status": "connected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/serial/disconnect', methods=['POST'])
def serial_disconnect():
    close_serial_connection()
    return jsonify({"status": "disconnected"})

@app.route('/api/serial/stream')
def serial_stream():
    def generate():
        while SERIAL_STATE["is_running"] and SERIAL_STATE["serial_obj"] and SERIAL_STATE["serial_obj"].is_open:
            try:
                if SERIAL_STATE["serial_obj"].in_waiting > 0:
                    line = SERIAL_STATE["serial_obj"].readline().decode('utf-8', errors='replace')
                    yield f"data: {json.dumps({'text': line})}\n\n"
                else:
                    time.sleep(0.05)
            except Exception as e:
                yield f"data: {json.dumps({'text': '[Serial Error] ' + str(e)})}\n\n"
                break
    return Response(generate(), mimetype='text/event-stream')

@app.route('/preview', methods=['POST'])
def preview_code():
    data = request.json
    try:
        wrapped = generate_wrapped_code(data.get('ssid'), data.get('pass'), data.get('api_url'), data.get('topic'), data.get('code'))
        return jsonify({"code": wrapped})
    except Exception as e:
        return jsonify({"code": "// Error generating preview:\n" + str(e)})

@app.route('/api/manifest', methods=['GET'])
def api_manifest():
    device_id = request.args.get('device')
    auth_header = request.headers.get('Authorization')
    device_info = DEVICE_DATABASE.get(device_id, {"auth_token": "secret_abc123"})
    
    if auth_header != f"Bearer {device_info['auth_token']}":
        return jsonify({"error": "Unauthorized"}), 401

    target_version = "v_base_001"
    fleet = "Ramanand"
    
    for f_name, f_data in VERSION_MANIFEST.items():
        if device_id in f_data.get("nodes", {}):
            fleet = f_name
            target_version = f_data["nodes"][device_id]
            break
        elif f_name in VERSION_MANIFEST:
            fleet = f_name
            target_version = f_data.get("fleet_version", "v_base_001")

    try:
        s3_client = get_s3_client()
        download_url = s3_client.generate_presigned_url(
            'get_object', Params={'Bucket': R2_BUCKET, 'Key': f'{fleet}/firmware.bin'}, ExpiresIn=900
        )
    except:
        download_url = ""

    return jsonify({"target_version": target_version, "download_url": download_url})

@app.route('/compile', methods=['POST'])
def compile_code():
    try:
        data = request.json
        ssid, passw, api_url, topic = data.get('ssid'), data.get('pass'), data.get('api_url'), data.get('topic')
        user_code = data.get('code')
        is_wrapped = data.get('is_wrapped', False)
        logs = []

        if is_wrapped:
            dummy_code = user_code
        else:
            dummy_code = generate_wrapped_code(ssid, passw, api_url, topic, user_code, "DUMMY_VERSION")
        
        code_hash = hashlib.md5(dummy_code.encode('utf-8')).hexdigest()[:8]
        version_id = f"v_{code_hash}"

        bin_path = os.path.join(CACHE_DIR, f"{version_id}.bin")
        skip_compile = version_id in COMPILED_CACHE and os.path.exists(bin_path)

        parts = topic.split('/')
        company = parts[0] if len(parts) > 0 else "Ramanand"
        is_fleet_update = "fleet" in topic
        device_id = parts[2] if len(parts) >= 3 else "node_1"

        if skip_compile:
            logs.append(f"[*] ⚡ CACHE HIT! Version '{version_id}' is already compiled. Bypassing arduino-cli.")
        else:
            final_code = user_code if is_wrapped else generate_wrapped_code(ssid, passw, api_url, topic, user_code, version_id)
            
            cli_path = SETUP_STATE["cli_path"]
            fqbn = "esp32:esp32:esp32"
            
            with tempfile.TemporaryDirectory() as temp_dir:
                sketch_name = "CloudSketch"
                sketch_dir = os.path.join(temp_dir, sketch_name)
                os.makedirs(sketch_dir)
                
                sketch_file = os.path.join(sketch_dir, f"{sketch_name}.ino")
                with open(sketch_file, "w", encoding="utf-8") as f:
                    f.write(final_code)

                logs.append("[*] Compiling new sketch with arduino-cli...")
                output_dir = os.path.join(temp_dir, "output")
                os.makedirs(output_dir, exist_ok=True)

                compile_res = subprocess.run([cli_path, "compile", "--fqbn", fqbn, "--build-cache-path", CORE_CACHE_DIR, "--output-dir", output_dir, sketch_file], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
                if compile_res.returncode != 0: 
                    return jsonify({"logs": "\n".join(logs) + "\n\n❌ COMPILATION FAILED:\n" + compile_res.stderr})
                
                compiled_bin = os.path.join(output_dir, f"{sketch_name}.ino.bin")
                shutil.copy(compiled_bin, bin_path)
                COMPILED_CACHE[version_id] = bin_path
                logs.append("[✓] Compilation successful and saved to local cache!")

        logs.append("[*] Uploading binary to Cloudflare R2...")
        try:
            s3_client = get_s3_client()
            remote_filename = f"{company}/firmware.bin"
            with open(bin_path, "rb") as bin_file:
                s3_client.put_object(Bucket=R2_BUCKET, Key=remote_filename, Body=bin_file, ContentType="application/octet-stream")
            
            if company not in VERSION_MANIFEST:
                VERSION_MANIFEST[company] = {"fleet_version": "v_base_001", "nodes": {}}
            
            if is_fleet_update:
                VERSION_MANIFEST[company]["fleet_version"] = version_id
                VERSION_MANIFEST[company]["nodes"] = {} 
                logs.append(f"[✓] Fleet Manifest updated: {company} -> {version_id} (All node overrides cleared)")
            else:
                VERSION_MANIFEST[company]["nodes"][device_id] = version_id
                logs.append(f"[✓] Node Manifest updated: {device_id} -> {version_id}")

            logs.append(f"[✓] Uploaded binary to R2: {remote_filename}")
            logs.append(f"\n🎉 SUCCESS! Version {version_id} published. Nodes targeting '{topic}' will pull it on next check-in.")
        except Exception as e:
            return jsonify({"logs": "\n".join(logs) + f"\n❌ R2 Upload Failed: {str(e)}"})

        return jsonify({"logs": "\n".join(logs)})
        
    except Exception as e:
        return jsonify({"logs": f"❌ Server Code Crash: {str(e)}"})

@app.route('/flash', methods=['POST'])
def flash_route():
    data = request.json
    logs = []
    
    close_serial_connection()
    time.sleep(1.5) 
    
    cli_path = SETUP_STATE["cli_path"]
    fqbn = "esp32:esp32:esp32"

    if not data.get('port'):
        return jsonify({"logs": "❌ Error: No COM port selected!"})
    
    user_setup = 'Serial.println("Global OTA Architecture Running!");'
    user_loop = 'static unsigned long p=0; if(millis()-p>=1000){p=millis(); Serial.println("Working...");}'

    full_code = MASTER_CPP_TEMPLATE
    full_code = full_code.replace("{WIFI_SSID}", data['ssid'])
    full_code = full_code.replace("{WIFI_PASS}", data['pass'])
    full_code = full_code.replace("{API_BASE_URL}", data['api_url'])
    full_code = full_code.replace("{DEVICE_ID}", data['device_id'])
    full_code = full_code.replace("{AUTH_TOKEN}", data['auth_token'])
    full_code = full_code.replace("{CURRENT_VERSION}", "v_base_001")
    full_code = full_code.replace("{USER_GLOBALS}", "")
    full_code = full_code.replace("{USER_SETUP}", user_setup)
    full_code = full_code.replace("{USER_LOOP}", user_loop)
    full_code = full_code.replace("{WIFI_FAIL_ACTION}", "ESP.restart();")

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            sketch_dir = os.path.join(temp_dir, "ProvSketch")
            os.makedirs(sketch_dir)
            sketch_file = os.path.join(sketch_dir, "ProvSketch.ino")
            with open(sketch_file, "w", encoding="utf-8") as f:
                f.write(full_code)

            logs.append("[*] Compiling Pull-Architecture sketch...")
            res = subprocess.run([cli_path, "compile", "--fqbn", fqbn, "--build-cache-path", CORE_CACHE_DIR, sketch_file], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            if res.returncode != 0: return jsonify({"logs": "❌ COMPILATION FAILED:\n" + res.stderr})
            
            logs.append(f"[*] Uploading via USB at 115200 baud to bypass boot pin requirement...")
            up_res = subprocess.run([cli_path, "upload", "-p", data['port'], "--fqbn", fqbn, "--upload-property", "upload.speed=115200", sketch_file], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            
            if up_res.returncode != 0: return jsonify({"logs": "❌ UPLOAD FAILED:\n" + up_res.stderr})

            logs.append("🎉 SUCCESS: ESP32 is now globally connected!")
            logs.append(f"It will check {data['api_url']} for updates on every boot.")
    except Exception as e:
        return jsonify({"logs": f"❌ Error: {str(e)}"})

    return jsonify({"logs": "\n".join(logs)})


if __name__ == '__main__':
    import webview
    
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    def start_server():
        app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    setup_thread = threading.Thread(target=setup_environment_thread, daemon=True)
    setup_thread.start()

    time.sleep(1.0)

    webview.create_window(
        title='WiFeY Studio', 
        url='http://127.0.0.1:5000', 
        width=1600, 
        height=1000,
        min_size=(1200, 800),
        background_color='#0f172a'
    )
    
    webview.start()