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
from flask import Flask, render_template_string, request, jsonify, Response, send_file
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
    if os.name != 'nt':
        return 
    if not getattr(sys, 'frozen', False):
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
        with open(vbs_path, 'w') as f:
            f.write(vbs_script)
        subprocess.run(['cscript.exe', '//Nologo', vbs_path], creationflags=CREATE_NO_WINDOW)
        os.remove(vbs_path)
    except Exception as e:
        pass 

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

CORE_CACHE_DIR = os.path.join(os.getcwd(), "core_cache")
os.makedirs(CORE_CACHE_DIR, exist_ok=True)

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
const char* api_url = "{API_BASE_URL}/api/manifest?fleet={FLEET_ID}&device={DEVICE_ID}";
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
    fleet_id = parts[0] if len(parts) > 0 else "DefaultFleet"
    device_id = parts[2] if len(parts) >= 3 else "node_1"
    
    final_code = MASTER_CPP_TEMPLATE
    final_code = final_code.replace('{WIFI_SSID}', ssid)
    final_code = final_code.replace('{WIFI_PASS}', passw)
    final_code = final_code.replace('{API_BASE_URL}', api_url)
    final_code = final_code.replace('{FLEET_ID}', fleet_id)
    final_code = final_code.replace('{DEVICE_ID}', device_id)
    final_code = final_code.replace('{AUTH_TOKEN}', "secret_abc123")
    final_code = final_code.replace('{CURRENT_VERSION}', version_id)
    final_code = final_code.replace('{USER_GLOBALS}', user_globals)
    final_code = final_code.replace('{USER_SETUP}', user_setup)
    final_code = final_code.replace('{USER_LOOP}', user_loop)
    final_code = final_code.replace('{WIFI_FAIL_ACTION}', wifi_fail_action)
    return final_code

# --- BRAND NEW SPLASH SCREEN ---
SETUP_HTML = """
<!DOCTYPE html>
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
                        ardBg: '#181818',
                        ardPanel: '#1e1e1e',
                        ardBorder: '#333333',
                        ardTeal: '#00979C',
                        ardTextMain: '#cccccc',
                        ardTextMuted: '#858585'
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
        body { background-color: #181818; color: #cccccc; }
        .loader { border: 3px solid #333333; border-top: 3px solid #00979C; border-radius: 50%; width: 28px; height: 28px; animation: spin 1s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #1e1e1e; }
        ::-webkit-scrollbar-thumb { background: #424242; border-radius: 4px; }
    </style>
</head>
<body class="h-screen w-screen flex flex-col items-center justify-center font-sans antialiased">
    
    <div class="bg-ardPanel border border-ardBorder p-8 rounded-lg shadow-2xl flex flex-col items-center w-[90%] max-w-[600px]">
        <div class="flex items-center gap-3 mb-2">
            <i class="ph ph-cpu text-4xl text-ardTeal"></i>
            <h2 class="text-3xl font-semibold text-white tracking-wide"><span class="text-ardTeal">WiFeY</span> Studio</h2>
        </div>
        <p class="text-ardTextMuted text-sm mb-8 text-center">Downloading core compilers. This only happens on the very first launch.</p>
        
        <div class="flex items-center gap-4 mb-4 w-full px-4">
            <div class="loader flex-shrink-0" id="spinner"></div>
            <div id="status-text" class="font-medium text-white text-sm flex-1">Initializing...</div>
        </div>
        
        <div class="w-full bg-[#181818] rounded-full h-1.5 mb-6 overflow-hidden border border-ardBorder relative">
            <div id="pbar" class="bg-ardTeal h-1.5 rounded-full transition-all duration-300 w-0"></div>
        </div>
        
        <div id="logs" class="w-full h-40 bg-[#181818] border border-ardBorder rounded p-4 font-mono text-xs text-ardTextMuted overflow-y-auto whitespace-pre-wrap">Starting...</div>
    </div>

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
                    document.getElementById('status-text').classList.add("text-red-400");
                    document.getElementById('spinner').style.display = "none";
                    document.getElementById('pbar').classList.remove("bg-ardTeal");
                    document.getElementById('pbar').classList.add("bg-red-500");
                }
            });
        }, 800);
    </script>
</body>
</html>
"""

# --- NEW IDE HTML (Arduino v2 Style + WiFeY Branding + UI Fixes) ---
IDE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>WiFeY Studio | Cloud OTA Compiler</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/@phosphor-icons/web"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    
    <script>
        // INJECTED CONFIGURATION FROM FLASK
        const NGROK_API_URL = "{{ ngrok_url }}"; 
        let activeBinFile = null; 
        let serialEventSource = null;
        let currentTopic = "ARES-1 Nodes/fleet/update"; 
    </script>

    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        ardBg: '#181818',       
                        ardPanel: '#1e1e1e',    
                        ardBorder: '#333333',   
                        ardTeal: '#00979C',     
                        ardHover: '#2a2d2e',    
                        ardTextMain: '#cccccc',
                        ardTextMuted: '#858585'
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
        body { background-color: #181818; color: #cccccc; overflow: hidden;}
        ::-webkit-scrollbar { width: 10px; height: 10px; }
        ::-webkit-scrollbar-track { background: #1e1e1e; }
        ::-webkit-scrollbar-thumb { background: #424242; border: 2px solid #1e1e1e; border-radius: 5px; }
        ::-webkit-scrollbar-thumb:hover { background: #4f4f4f; }
        .editor-textarea { resize: none; outline: none; tab-size: 4; line-height: 1.5rem; }
        .line-numbers { line-height: 1.5rem; }
        .view-hidden { display: none !important; }
        .flat-input { background-color: transparent; border: 1px solid transparent; outline: none; transition: all 0.2s; }
        .flat-input:hover { border-color: #333333; background-color: #2a2d2e; }
        .flat-input:focus { border-color: #00979C; background-color: #1e1e1e; }
        .dropdown-content { display: none; }
        .dropdown-content.show { display: block; }
    </style>
</head>
<body class="h-screen w-screen flex flex-col text-sm antialiased selection:bg-ardTeal selection:text-white">

    <input type="file" id="bin-file-input" accept=".bin" class="hidden" onchange="handleBinLoad(event)">

    <!-- TOP ACTION BAR -->
    <header class="h-12 bg-ardBg border-b border-ardBorder flex items-center justify-between px-3 flex-shrink-0 z-20">
        <div class="flex items-center gap-3">
            <div class="relative">
                <button class="w-8 h-8 rounded flex items-center justify-center hover:bg-ardHover outline-none menu-btn text-ardTextMuted hover:text-white transition-colors" onclick="toggleDropdown('menu-file', event)" title="File Options">
                    <i class="ph ph-folder-open text-xl pointer-events-none"></i>
                </button>
                <div id="menu-file" class="dropdown-content absolute top-full left-0 mt-1 w-48 bg-[#252526] border border-ardBorder shadow-xl z-50 py-1 rounded">
                    <button class="w-full text-left px-4 py-1.5 hover:bg-ardTeal hover:text-white flex items-center group">
                        <i class="ph ph-file-plus mr-2 text-lg text-ardTextMuted group-hover:text-white"></i> New Sketch
                    </button>
                    <div class="h-px bg-[#333] my-1"></div>
                    <button class="w-full text-left px-4 py-1.5 hover:bg-ardTeal hover:text-white flex items-center group" onclick="triggerLoadBin()">
                        <i class="ph ph-upload-simple mr-2 text-lg text-ardTextMuted group-hover:text-white"></i> Load .bin...
                    </button>
                    <button class="w-full text-left px-4 py-1.5 hover:bg-ardTeal hover:text-white flex items-center group" onclick="triggerSaveBin()">
                        <i class="ph ph-download-simple mr-2 text-lg text-ardTextMuted group-hover:text-white"></i> Save .bin...
                    </button>
                </div>
            </div>

            <div class="h-5 w-px bg-ardBorder mx-1"></div>

            <div class="flex items-center gap-2">
                <button class="w-8 h-8 rounded-full bg-ardTeal text-white flex items-center justify-center hover:brightness-110 active:opacity-40 transition-all shadow-md" title="Verify (Compile)" onclick="simulateAction('Verifying')">
                    <i class="ph ph-check text-xl"></i>
                </button>
                <button class="w-8 h-8 rounded-full bg-ardTeal text-white flex items-center justify-center hover:brightness-110 active:opacity-40 transition-all shadow-md" title="Upload / OTA" onclick="simulateAction('Uploading')">
                    <i class="ph ph-arrow-right text-xl"></i>
                </button>
                <div id="action-progress-container" class="w-24 h-1.5 bg-[#333] rounded-full overflow-hidden hidden ml-1 flex-shrink-0 relative">
                    <div id="action-progress-bar" class="h-full bg-yellow-400 w-0 transition-all duration-200 ease-out absolute left-0 top-0"></div>
                </div>
            </div>

            <div class="h-5 w-px bg-ardBorder mx-1"></div>

            <div class="relative">
                <button id="fleet-selector-btn" class="flex items-center gap-2 px-3 py-1.5 rounded hover:bg-ardHover border border-transparent hover:border-ardBorder transition-colors text-sm group" onclick="toggleDropdown('fleet-dropdown', event)">
                    <i class="ph ph-cpu text-ardTeal"></i>
                    <span class="font-medium text-white">ARES-1 Nodes</span>
                    <i class="ph ph-caret-down text-ardTextMuted group-hover:text-white transition-colors"></i>
                </button>
                <div id="fleet-dropdown" class="dropdown-content absolute top-full left-0 mt-1 w-80 bg-[#252526] border border-ardBorder rounded shadow-xl z-50 py-2">
                    <div class="px-3 py-1.5 text-xs font-semibold text-ardTextMuted uppercase tracking-wider">Select Target Devices</div>
                    <div class="mt-2">
                        <div class="flex items-center justify-between px-3 py-1.5 bg-[#2a2d2e] border-y border-[#333]">
                            <span class="font-medium text-white">ARES-1 Nodes</span>
                            <span class="text-xs bg-emerald-500/10 text-emerald-400 px-1.5 py-0.5 rounded border border-emerald-500/20">4/5 ✅</span>
                        </div>
                    </div>
                </div>
            </div>
            
            <div class="h-5 w-px bg-ardBorder"></div>
            
            <div class="flex items-center gap-2">
                <i class="ph ph-wifi-high text-ardTextMuted"></i>
                <input type="text" id="top-wifi-ssid" placeholder="SSID" value="Wi-Fi_Network" class="flat-input text-xs px-2 py-1 w-28 rounded placeholder-gray-600">
                <div class="relative flex items-center">
                    <input type="password" id="top-wifi-pass" placeholder="Password" value="dummy" class="flat-input text-xs px-2 py-1 w-28 rounded pr-6 placeholder-gray-600">
                    <button type="button" onclick="togglePassword('top-wifi-pass')" class="absolute right-1 text-ardTextMuted hover:text-white">
                        <i class="ph ph-eye text-xs"></i>
                    </button>
                </div>
            </div>
        </div>

        <div class="flex items-center gap-3">
            <div class="flex items-center gap-1.5 px-2 py-0.5 rounded bg-ardHover text-xs text-ardTextMuted">
                <span class="w-2 h-2 rounded-full bg-emerald-500 shadow-[0_0_5px_#10b981]"></span>
                Online
            </div>
            <div class="flex items-center gap-2">
                <i class="ph ph-cpu text-2xl text-ardTeal"></i>
                <span class="font-semibold tracking-wide text-white"><span class="text-ardTeal">WiFeY</span> Studio</span>
            </div>
        </div>
    </header>

    <div class="flex-1 flex overflow-hidden">
        
        <!-- Sidebar -->
        <aside class="w-12 bg-ardBg border-r border-ardBorder flex flex-col items-center py-3 gap-4 flex-shrink-0 z-10 relative">
            <button onclick="switchMainView('view-editor')" id="nav-editor" class="nav-btn p-2 rounded text-ardTeal bg-ardHover relative group" title="Editor">
                <i class="ph ph-files text-2xl"></i>
            </button>
            <button onclick="switchMainView('view-usb')" id="nav-usb" class="nav-btn p-2 rounded text-ardTextMuted hover:text-white hover:bg-ardHover relative group transition-colors" title="USB Provisioning">
                <i class="ph ph-usb text-2xl"></i>
            </button>
        </aside>

        <!-- Main Content Area -->
        <div class="flex-1 flex flex-col min-w-0 bg-ardPanel">
            
            <!-- Views Container (Flex-1 allows it to fill space above Bottom Panel) -->
            <div class="flex-1 flex flex-col min-h-0 relative">
                
                <!-- VIEW: EDITOR -->
                <main id="view-editor" class="flex-1 flex flex-col overflow-hidden">
                    <div class="flex items-center h-9 bg-[#1e1e1e] overflow-x-auto select-none px-2 shadow-[0_1px_0_0_#333]">
                        <button onclick="switchEditorTab('tab-raw')" id="btn-tab-raw" class="editor-tab h-full flex items-center px-4 gap-2 text-sm text-white bg-[#1e1e1e] border-t-2 border-ardTeal">
                            <i class="ph ph-file-code text-ardTeal"></i> Raw Input
                        </button>
                        <button onclick="switchEditorTab('tab-wrap')" id="btn-tab-wrap" class="editor-tab h-full flex items-center px-4 gap-2 text-sm text-ardTextMuted hover:text-white border-t-2 border-transparent bg-transparent transition-colors">
                            <i class="ph ph-file-text"></i> Preview Wrapped
                        </button>
                    </div>

                    <div id="tab-raw" class="editor-content flex-1 flex overflow-hidden bg-ardPanel">
                        <div class="w-12 text-right pr-3 py-4 font-mono text-xs text-[#6e7681] bg-ardPanel select-none border-r border-ardBorder line-numbers flex flex-col">
                            <span>1</span><span>2</span><span>3</span><span>4</span><span>5</span><span>6</span><span>7</span><span>8</span>
                        </div>
                        <textarea class="editor-textarea flex-1 p-4 bg-transparent text-[#d4d4d4] font-mono text-sm whitespace-pre" spellcheck="false">int ledPin = 2;
void setup() {
  pinMode(ledPin, OUTPUT);
  Serial.println("Hello from WiFeY Studio OTA Architecture!");
}
void loop() {
  digitalWrite(ledPin, HIGH); delay(1000);
  digitalWrite(ledPin, LOW); delay(1000);
}</textarea>
                    </div>

                    <div id="tab-wrap" class="editor-content flex-1 flex overflow-hidden bg-ardPanel view-hidden">
                        <div class="w-12 text-right pr-3 py-4 font-mono text-xs text-[#6e7681] bg-ardPanel select-none border-r border-ardBorder line-numbers flex flex-col">
                            <span>1</span>
                        </div>
                        <textarea class="editor-textarea flex-1 p-4 bg-transparent text-[#d4d4d4] font-mono text-sm whitespace-pre" readonly spellcheck="false">/* Click tab again to load smart preview... */</textarea>
                    </div>
                </main>

                <!-- VIEW: USB PROVISIONING -->
                <main id="view-usb" class="flex-1 overflow-y-auto p-8 view-hidden">
                    <div class="max-w-4xl mx-auto w-full flex flex-col gap-6 pb-8">
                        <div class="border-b border-ardBorder pb-4">
                            <h2 class="text-2xl font-semibold text-white flex items-center gap-2">
                                <i class="ph ph-usb text-ardTeal"></i> Direct USB Provisioning
                            </h2>
                            <p class="text-sm text-ardTextMuted mt-1">Configure fleets and flash physical devices via serial port.</p>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                            <div class="bg-ardBg border border-ardBorder rounded-lg p-5 flex flex-col shadow-lg">
                                <h3 class="text-sm font-semibold text-white mb-4 uppercase tracking-wider flex items-center gap-2">
                                    <i class="ph ph-cpu text-ardTeal"></i> Device Flash Settings
                                </h3>
                                
                                <div class="space-y-4 flex-1">
                                    <div>
                                        <label class="block text-xs font-medium text-ardTextMuted mb-1.5">Target COM Port</label>
                                        <div class="flex gap-2">
                                            <select id="prov_port" class="min-w-0 flex-1 bg-[#1e1e1e] border border-ardBorder text-white rounded focus:border-ardTeal block p-2 outline-none text-sm cursor-pointer"></select>
                                            <button onclick="loadSerialPorts()" class="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded border border-ardBorder bg-[#1e1e1e] p-0 text-ardTextMuted transition-colors hover:bg-ardHover hover:text-white">
                                                <i class="ph ph-arrows-clockwise"></i>
                                            </button>
                                        </div>
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-ardTextMuted mb-1.5">Assign Fleet Name</label>
                                        <input type="text" id="prov_fleet_id" value="ARES-1" class="w-full bg-[#1e1e1e] border border-ardBorder text-white rounded focus:border-ardTeal block p-2 outline-none text-sm font-mono">
                                    </div>
                                    <div>
                                        <label class="block text-xs font-medium text-ardTextMuted mb-1.5">Assign Device ID (Unique)</label>
                                        <input type="text" id="prov_device_id" value="node_1" class="w-full bg-[#1e1e1e] border border-ardBorder text-white rounded focus:border-ardTeal block p-2 outline-none text-sm font-mono">
                                    </div>
                                </div>

                                <div class="pt-6 mt-4 border-t border-ardBorder">
                                    <button onclick="flashDevice()" class="w-full flex items-center justify-center gap-2 py-3 px-4 bg-white hover:bg-gray-200 text-black text-sm rounded transition-colors font-semibold shadow-lg active:scale-[0.98]">
                                        <i class="ph ph-lightning text-lg text-ardTeal"></i> Compile & Provision via USB
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </main>
            </div>

            <!-- Bottom Panel (Fixed height, persists across views) -->
            <div class="h-64 flex flex-col border-t border-ardBorder bg-[#1e1e1e] flex-shrink-0 z-10">
                <div class="flex items-center h-8 bg-ardBg px-2 border-b border-ardBorder select-none shadow-[0_1px_0_0_#333]">
                    <button onclick="switchPanelTab('panel-out')" id="btn-panel-out" class="panel-tab flex items-center px-3 h-full text-xs text-white border-b border-ardTeal">
                        Output
                    </button>
                    <button onclick="switchPanelTab('panel-serial')" id="btn-panel-serial" class="panel-tab flex items-center px-3 h-full text-xs text-ardTextMuted hover:text-white border-b border-transparent transition-colors">
                        Serial Monitor
                    </button>
                </div>

                <!-- Panel Content: Output -->
                <div id="panel-out" class="panel-content flex-1 p-3 overflow-y-auto font-mono text-xs text-[#cccccc] bg-[#1e1e1e]">
                    <div class="text-[#858585]">Environment initialized. Ready.</div>
                </div>

                <!-- Panel Content: Serial Monitor -->
                <div id="panel-serial" class="panel-content flex-1 flex flex-col view-hidden bg-[#1e1e1e]">
                    <div class="flex items-center gap-2 p-1.5 border-b border-ardBorder bg-[#252526]">
                        <select id="serial_port" class="flat-input text-xs py-1 px-2 rounded bg-transparent w-40 text-white cursor-pointer">
                            <option disabled selected>Loading Ports...</option>
                        </select>
                        
                        <select id="serial_baud" class="flat-input text-xs py-1 px-2 rounded bg-transparent text-white cursor-pointer w-28">
                            <option>9600</option>
                            <option selected>115200</option>
                        </select>
                        
                        <div class="h-4 w-px bg-[#444] mx-1"></div>
                        
                        <button id="btn-serial-connect" onclick="connectSerial()" class="flex items-center gap-1.5 px-3 py-1 rounded text-xs border border-[#333] hover:border-emerald-500 text-emerald-400 hover:bg-emerald-500/10 transition-colors">
                            <i class="ph ph-plug"></i> Connect
                        </button>
                        <button id="btn-serial-disconnect" onclick="disconnectSerial()" class="flex items-center gap-1.5 px-3 py-1 rounded text-xs border border-[#333] hover:border-red-500 text-[#858585] hover:text-red-400 hover:bg-red-500/10 transition-colors" style="display:none;">
                            <i class="ph ph-plugs"></i> Disconnect
                        </button>

                        <div class="flex-1"></div>

                        <button onclick="document.getElementById('serial_out').innerHTML=''" class="flex items-center gap-1.5 px-3 py-1 rounded text-xs border border-[#333] hover:border-[#555] text-ardTextMuted hover:text-white transition-colors">
                            <i class="ph ph-prohibit"></i> Clear
                        </button>
                    </div>
                    
                    <div id="serial_out" class="flex-1 p-3 overflow-y-auto font-mono text-xs text-[#cccccc] whitespace-pre-wrap"><span class="text-[#858585] italic">--- Serial Monitor Not Connected ---</span></div>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Keep the app at its default scale.
        window.addEventListener('keydown', function(e) {
            if ((e.ctrlKey || e.metaKey) && (e.key === '=' || e.key === '+' || e.key === '-')) {
                e.preventDefault();
            }
        });

        window.onload = () => {
            loadSerialPorts();
        }

        // --- Core UI Logic ---
        function togglePassword(inputId) {
            const input = document.getElementById(inputId);
            const icon = input.nextElementSibling.querySelector('i');
            if (input.type === 'password') { input.type = 'text'; icon.className = 'ph ph-eye-slash text-xs'; } 
            else { input.type = 'password'; icon.className = 'ph ph-eye text-xs'; }
        }

        function switchMainView(viewId) {
            document.getElementById('view-editor').classList.add('view-hidden');
            document.getElementById('view-usb').classList.add('view-hidden');
            document.getElementById(viewId).classList.remove('view-hidden');
            document.querySelectorAll('.nav-btn').forEach(btn => {
                btn.classList.remove('text-ardTeal', 'bg-ardHover');
                btn.classList.add('text-ardTextMuted');
            });
            const activeBtn = document.getElementById(viewId === 'view-editor' ? 'nav-editor' : 'nav-usb');
            activeBtn.classList.remove('text-ardTextMuted');
            activeBtn.classList.add('text-ardTeal', 'bg-ardHover');
            if(viewId === 'view-usb') loadSerialPorts();
        }

        function switchEditorTab(tabId) {
            document.querySelectorAll('.editor-content').forEach(el => el.classList.add('view-hidden'));
            document.querySelectorAll('.editor-tab').forEach(el => {
                el.classList.remove('text-white', 'border-ardTeal', 'bg-[#1e1e1e]');
                el.classList.add('text-ardTextMuted', 'border-transparent', 'bg-transparent');
            });
            document.getElementById(tabId).classList.remove('view-hidden');
            const activeBtn = document.getElementById('btn-' + tabId);
            activeBtn.classList.remove('text-ardTextMuted', 'border-transparent', 'bg-transparent');
            activeBtn.classList.add('text-white', 'border-ardTeal', 'bg-[#1e1e1e]');
            if(tabId === 'tab-wrap') fetchPreview();
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
            if(event) event.stopPropagation();
            closeAllMenus();
            document.getElementById(dropdownId).classList.toggle("show");
        }
        function closeAllMenus() {
            var dropdowns = document.getElementsByClassName("dropdown-content");
            for (var i = 0; i < dropdowns.length; i++) dropdowns[i].classList.remove('show');
        }
        window.onclick = function(event) {
            if (!event.target.closest('#fleet-selector-btn') && !event.target.closest('.dropdown-content') && !event.target.closest('.menu-btn')) {
                closeAllMenus();
            }
        }

        function logToOutput(msg) {
            const outputPanel = document.getElementById('panel-out');
            const logEntry = document.createElement('div');
            if (msg.includes("[Build]") || msg.includes("SUCCESS")) logEntry.className = "text-ardTeal mt-1";
            else if (msg.includes("[System]")) logEntry.className = "text-emerald-400 mt-1";
            else if (msg.includes("[File]")) logEntry.className = "text-yellow-400 mt-1";
            else if (msg.includes("Error") || msg.includes("❌")) logEntry.className = "text-red-400 mt-1";
            else logEntry.className = "text-[#cccccc] mt-1";
            logEntry.textContent = msg;
            outputPanel.appendChild(logEntry);
            outputPanel.scrollTop = outputPanel.scrollHeight;
            switchPanelTab('panel-out');
        }

        // --- Fetch & Hardware Logic ---
        function fetchPreview() {
            const rawCode = document.querySelector('#tab-raw textarea').value;
            const payload = {
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL, topic: currentTopic, code: rawCode
            };
            document.querySelector('#tab-wrap textarea').value = "Generating smart preview...";
            fetch('/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
            .then(res => res.json()).then(data => { document.querySelector('#tab-wrap textarea').value = data.code; })
            .catch(err => { document.querySelector('#tab-wrap textarea').value = "// Error: " + err; });
        }

        function loadSerialPorts() {
            fetch('/api/ports').then(r=>r.json()).then(ports => {
                let sel1 = document.getElementById('serial_port');
                let sel2 = document.getElementById('prov_port');
                const html = ports.length ? ports.map(p => `<option value="${p.device}">${p.device} - ${p.desc}</option>`).join('') : '<option value="">No Devices</option>';
                if(sel1) sel1.innerHTML = html;
                if(sel2) sel2.innerHTML = html;
            });
        }

        function connectSerial() {
            const port = document.getElementById('serial_port').value;
            const baud = document.getElementById('serial_baud').value;
            if(!port) return alert("Please select a COM port first.");
            fetch('/api/serial/connect', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({port: port, baud: baud}) })
            .then(r=>r.json()).then(res => {
                if(res.error) { alert("Failed to connect: " + res.error); return; }
                document.getElementById('btn-serial-connect').style.display = 'none';
                document.getElementById('btn-serial-disconnect').style.display = 'inline-flex';
                const out = document.getElementById('serial_out');
                out.innerHTML += `\\n[*] Connected to ${port} at ${baud} baud\\n`;
                serialEventSource = new EventSource('/api/serial/stream');
                serialEventSource.onmessage = function(e) {
                    const data = JSON.parse(e.data);
                    out.appendChild(document.createTextNode(data.text));
                    out.scrollTop = out.scrollHeight;
                };
            });
        }

        function disconnectSerial(quiet = false) {
            fetch('/api/serial/disconnect', {method: 'POST'}).then(() => {
                if(serialEventSource) { serialEventSource.close(); serialEventSource = null; }
                document.getElementById('btn-serial-connect').style.display = 'inline-flex';
                document.getElementById('btn-serial-disconnect').style.display = 'none';
                if (!quiet) document.getElementById('serial_out').innerHTML += `\\n[*] Connection Closed.\\n`;
            });
        }

        // --- The Flash & Auto-Reconnect Logic ---
        let pendingSerialReconnect = false;
        let savedSerialPort = "";
        let savedSerialBaud = "";

        function flashDevice() {
            const port = document.getElementById('prov_port').value;
            if(!port) return alert("Select a COM port first.");
            
            if (serialEventSource) {
                savedSerialPort = document.getElementById('serial_port').value;
                savedSerialBaud = document.getElementById('serial_baud').value;
                if (savedSerialPort === port) {
                    pendingSerialReconnect = true;
                    disconnectSerial(true); 
                }
            }
            
            const data = {
                port: port,
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL,
                fleet_id: document.getElementById('prov_fleet_id').value,
                device_id: document.getElementById('prov_device_id').value,
                auth_token: "secret_abc123"
            };
            
            logToOutput("[System] Hardware Flashing initiated via USB...");
            
            fetch('/flash', { method: 'POST', headers: { 'Content-Type': 'application/json'}, body: JSON.stringify(data) })
            .then(res => res.json()).then(result => {
                logToOutput(result.logs);
                if (pendingSerialReconnect) {
                    logToOutput("[System] Reconnecting Serial Monitor in 2 seconds...");
                    setTimeout(() => {
                        document.getElementById('serial_port').value = savedSerialPort;
                        document.getElementById('serial_baud').value = savedSerialBaud;
                        connectSerial();
                        pendingSerialReconnect = false;
                        switchPanelTab('panel-serial');
                    }, 2000);
                }
            })
            .catch(err => {
                logToOutput(`[System] Error: ${err}`);
                pendingSerialReconnect = false;
            });
        }

        // --- Build & Deploy Logic ---
        function simulateAction(actionType) {
            if (actionType === 'Verifying') triggerSaveBin();
            else if (actionType === 'Uploading') deployLoadedBin();
        }

        function triggerSaveBin() {
            closeAllMenus();
            const container = document.getElementById('action-progress-container');
            const bar = document.getElementById('action-progress-bar');
            container.classList.remove('hidden'); bar.style.width = '50%';
            logToOutput("[Build] Starting compilation on local server...");

            const rawCode = document.querySelector('#tab-raw textarea').value;
            const payload = {
                ssid: document.getElementById('top-wifi-ssid').value,
                pass: document.getElementById('top-wifi-pass').value,
                api_url: NGROK_API_URL, topic: currentTopic, code: rawCode
            };

            fetch('/api/build', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
            .then(async response => {
                bar.style.width = '100%';
                setTimeout(() => container.classList.add('hidden'), 500);

                if (response.headers.get("content-type").includes("application/json")) {
                    const data = await response.json(); logToOutput(data.logs); return;
                }
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url; a.download = "wifey_firmware.bin";
                document.body.appendChild(a); a.click(); a.remove();
                window.URL.revokeObjectURL(url);
                logToOutput("[System] Binary compiled and downloaded to your machine successfully.");
            })
            .catch(err => { container.classList.add('hidden'); logToOutput(`[System] Compile Error: ${err}`); });
        }

        function triggerLoadBin() { document.getElementById('bin-file-input').click(); closeAllMenus(); }
        function handleBinLoad(event) {
            const file = event.target.files[0];
            if (file) {
                activeBinFile = file;
                logToOutput(`[File] Loaded binary file: ${file.name} (${(file.size / 1024).toFixed(2)} KB)`);
                logToOutput(`[System] Ready to flash. Click the right-arrow icon to deploy to fleet.`);
                event.target.value = ''; 
            }
        }

        function deployLoadedBin() {
            if (!activeBinFile) { logToOutput("[System] ❌ No binary loaded. Click File -> Load .bin first."); return; }
            const container = document.getElementById('action-progress-container');
            const bar = document.getElementById('action-progress-bar');
            container.classList.remove('hidden'); bar.style.width = '40%';
            logToOutput("[Deploy] Pushing staged binary to Cloudflare R2...");

            const formData = new FormData();
            formData.append('bin_file', activeBinFile);
            formData.append('topic', currentTopic); 

            fetch('/api/deploy', { method: 'POST', body: formData })
            .then(res => res.json()).then(data => {
                bar.style.width = '100%'; setTimeout(() => container.classList.add('hidden'), 500);
                logToOutput(data.logs); activeBinFile = null; 
            })
            .catch(err => { container.classList.add('hidden'); logToOutput(`[System] Deploy Error: ${err}`); });
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
    fleet_id = request.args.get('fleet', 'DefaultFleet')
    device_id = request.args.get('device')
    auth_header = request.headers.get('Authorization')
    device_info = DEVICE_DATABASE.get(device_id, {"auth_token": "secret_abc123"})
    
    if auth_header != f"Bearer {device_info['auth_token']}":
        return jsonify({"error": "Unauthorized"}), 401

    target_version = "v_base_001"
    
    if fleet_id in VERSION_MANIFEST:
        f_data = VERSION_MANIFEST[fleet_id]
        if device_id in f_data.get("nodes", {}):
            target_version = f_data["nodes"][device_id]
        else:
            target_version = f_data.get("fleet_version", "v_base_001")

    try:
        s3_client = get_s3_client()
        download_url = s3_client.generate_presigned_url(
            'get_object', Params={'Bucket': R2_BUCKET, 'Key': f'{fleet_id}/firmware.bin'}, ExpiresIn=900
        )
    except:
        download_url = ""

    return jsonify({"target_version": target_version, "download_url": download_url})

@app.route('/api/build', methods=['POST'])
def build_binary():
    try:
        data = request.json
        ssid, passw = data.get('ssid'), data.get('pass')
        api_url, topic = data.get('api_url'), data.get('topic')
        user_code = data.get('code')

        final_code = generate_wrapped_code(ssid, passw, api_url, topic, user_code, "manual_build")
        
        cli_path = SETUP_STATE.get("cli_path") or shutil.which("arduino-cli")
        fqbn = "esp32:esp32:esp32"
        
        with tempfile.TemporaryDirectory() as temp_dir:
            sketch_name = "WiFeYSketch"
            sketch_dir = os.path.join(temp_dir, sketch_name)
            os.makedirs(sketch_dir)
            
            sketch_file = os.path.join(sketch_dir, f"{sketch_name}.ino")
            with open(sketch_file, "w", encoding="utf-8") as f:
                f.write(final_code)

            output_dir = os.path.join(temp_dir, "output")
            os.makedirs(output_dir, exist_ok=True)

            compile_res = subprocess.run([cli_path, "compile", "--fqbn", fqbn, "--build-cache-path", CORE_CACHE_DIR, "--output-dir", output_dir, sketch_file], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
            
            if compile_res.returncode != 0: 
                return jsonify({"error": True, "logs": "❌ COMPILATION FAILED:\n" + compile_res.stderr})
            
            compiled_bin = os.path.join(output_dir, f"{sketch_name}.ino.bin")
            
            return send_file(compiled_bin, as_attachment=True, download_name="wifey_firmware.bin", mimetype="application/octet-stream")
            
    except Exception as e:
        return jsonify({"error": True, "logs": f"❌ Server Crash: {str(e)}"})

@app.route('/api/deploy', methods=['POST'])
def deploy_binary():
    try:
        if 'bin_file' not in request.files:
            return jsonify({"error": True, "logs": "No binary file provided."})
            
        bin_file = request.files['bin_file']
        topic = request.form.get('topic')
        version_id = f"v_{int(time.time())}" 
        
        parts = topic.split('/')
        company = parts[0] if len(parts) > 0 else "DefaultFleet"
        is_fleet_update = "fleet" in topic
        device_id = parts[2] if len(parts) >= 3 else "node_1"

        s3_client = get_s3_client()
        remote_filename = f"{company}/firmware.bin"
        s3_client.put_object(Bucket=R2_BUCKET, Key=remote_filename, Body=bin_file.read(), ContentType="application/octet-stream")
        
        if company not in VERSION_MANIFEST:
            VERSION_MANIFEST[company] = {"fleet_version": "v_base_001", "nodes": {}}
        
        if is_fleet_update:
            VERSION_MANIFEST[company]["fleet_version"] = version_id
            VERSION_MANIFEST[company]["nodes"] = {} 
        else:
            VERSION_MANIFEST[company]["nodes"][device_id] = version_id

        return jsonify({"error": False, "logs": f"🎉 SUCCESS! Version {version_id} manually published to '{topic}'."})
        
    except Exception as e:
        return jsonify({"error": True, "logs": f"❌ Deploy Failed: {str(e)}"})

@app.route('/flash', methods=['POST'])
def flash_route():
    data = request.json
    logs = []
    
    close_serial_connection()
    time.sleep(1.5) 
    
    cli_path = SETUP_STATE.get("cli_path") or shutil.which("arduino-cli")
    fqbn = "esp32:esp32:esp32"

    if not data.get('port'):
        return jsonify({"logs": "❌ Error: No COM port selected!"})
    
    user_setup = 'Serial.println("Global OTA Architecture Running!");'
    user_loop = 'static unsigned long p=0; if(millis()-p>=1000){p=millis(); Serial.println("Working...");}'

    full_code = MASTER_CPP_TEMPLATE
    full_code = full_code.replace("{WIFI_SSID}", data.get('ssid', ''))
    full_code = full_code.replace("{WIFI_PASS}", data.get('pass', ''))
    full_code = full_code.replace("{API_BASE_URL}", data.get('api_url', ''))
    full_code = full_code.replace("{FLEET_ID}", data.get('fleet_id', 'DefaultFleet'))
    full_code = full_code.replace("{DEVICE_ID}", data.get('device_id', 'node_1'))
    full_code = full_code.replace("{AUTH_TOKEN}", data.get('auth_token', ''))
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
        background_color='#181818'
    )
    
    webview.start()