# ⚡ WiFeY Studio

**Wireless Infrastructure for Firmware & Embedded sYstems**

WiFeY Studio is an open-source, lightweight desktop IDE and Over-The-Air (OTA) management platform for ESP32 microcontroller fleets. It allows creators, students, and embedded developers to compile C++ code locally, distribute binaries over secure cloud storage, and provision ESP32 nodes globally without maintaining dedicated cloud server infrastructure or MQTT brokers.

---

## 🌟 Key Features

* **Zero-Config Fast Toolchain Setup:** Automatically detects local or global `arduino-cli`, with fast-track instant initialization and automated downloads for ESP32 board index, Xtensa toolchains, and `ArduinoJson`.
* **Ultra-Fast Multi-Core Build Cache:** Utilizes all CPU cores (`-j 0`) and smart persistent build caching, cutting compile times to just seconds.
* **Persistent Fleet & Device Registry:** Live SQLite/JSON device database (`wifey_db.json`) tracking connected ESP32 nodes, target versions, check-in history, and online status.
* **Pull-Based OTA Architecture:** ESP32 nodes check a lightweight JSON manifest endpoint over HTTPS to pull down update binaries only when a new version is published.
* **Cloudflare R2 Binary Distribution:** Uses S3-compatible pre-signed URLs to distribute firmware binaries securely over wide area networks (WANs).
* **Automated Ngrok Tunneling:** Exposes local Flask management endpoints to the internet automatically via static Ngrok tunnels.
* **USB Initial Provisioning Tool:** Flash new ESP32 microcontrollers with the WiFeY OTA bootloader with a single click and auto-register them in your fleet.
* **Bidirectional Serial Monitor:** Integrated real-time SSE serial monitor with command sending, configurable baud rates, timestamps, and autoscroll for hardware debugging.
* **Developer-Friendly Code Editor:** Features synchronized line numbering, Tab key indentation, keyboard shortcuts (`Ctrl+S`, `Ctrl+U`), wrapped OTA preview, and built-in example sketches.
* **In-IDE Cloud & Settings Config:** Manage `.env` credentials, Ngrok domain, R2 bucket keys, and toolchain configurations directly from the desktop interface.
* **Native Desktop Interface:** Runs as a native desktop application using `pywebview`.

---

## 🏗️ System Architecture

```text
  ┌─────────────────────────────────────────────────────────┐
  │                    WiFeY Studio IDE                     │
  │            (PyWebView Desktop + Flask Local Host)       │
  └───────────┬─────────────────────────────────┬───────────┘
              │                                 │
     (Build & Cache)                     (Firmware Upload)
              │                                 │
              ▼                                 ▼
    ┌───────────────────┐             ┌───────────────────┐
    │   arduino-cli     │             │   Cloudflare R2   │
    │  (Local Compiler) │             │ (S3 Binary Bucket)│
    └───────────────────┘             └─────────┬─────────┘
                                                │
                                         (Pre-Signed URL)
                                                │
  ┌───────────────────┐   Manifest Check        │
  │    ESP32 Node     │ ────────────────────────┘
  │ (Pull OTA Client) │ <--- Download Binary
  └───────────────────┘
```

---

## 📋 Prerequisites

Before setting up WiFeY Studio, ensure you have:

1. **Python 3.10+** installed on your system.
2. A free **[Ngrok Account](https://ngrok.com)** with a Static Domain configured.
3. A free **[Cloudflare R2](https://www.cloudflare.com/developer-platform/r2/)** bucket created for storing firmware binaries.

---

## ⚙️ Environment Configuration (`.env`)

WiFeY Studio requires a `.env` file in the project root directory to connect to your Ngrok tunnel and Cloudflare R2 storage bucket.

1. Create a file named `.env` in the root directory:
   ```bash
   touch .env
   ```

2. Add your credentials into `.env`:
   ```ini
   # --- NGROK CONFIGURATION ---
   NGROK_STATIC_DOMAIN=your-custom-domain.ngrok-free.dev

   # --- CLOUDFLARE R2 CONFIGURATION ---
   R2_ACCOUNT_ID=your_cloudflare_account_id
   R2_ACCESS_KEY=your_r2_access_key_id
   R2_SECRET_KEY=your_r2_secret_access_key
   R2_BUCKET=wifey-firmware
   ```

> ⚠️ **Important Security Note:** Never commit your `.env` file to public GitHub repositories! Ensure `.env` is added to your `.gitignore` file.

---

## 🚀 Quick Start & Installation

You can run WiFeY Studio either directly using Python or by compiling it into a standalone Windows Executable (`.exe`).

### Option 1: Automated Installer & Build Pipeline (Recommended)

Run the included automated build script. It will install all required Python libraries, compile `app.py` into a standalone `.exe` using PyInstaller, and launch the application:

```bash
python WiFeY-Installer.py
```

After compilation, the executable `WiFeY_Studio.exe` will be located in the `dist/` directory.

---

### Option 2: Running from Source (Development Mode)

1. **Clone the Repository:**
   ```bash
   git clone [https://github.com/your-username/WiFeY-Studio.git](https://github.com/your-username/WiFeY-Studio.git)
   cd WiFeY-Studio
   ```

2. **Install Required Python Libraries:**
   ```bash
   pip install flask pyserial boto3 python-dotenv pyngrok pywebview
   ```

3. **Ensure `.env` is set up** (see [Environment Configuration](#️-environment-configuration-env)).

4. **Launch the Application:**
   ```bash
   python app.py
   ```

---

## 📖 User Guide & Workflow

### 1. Provisioning a New ESP32 (First-Time Setup)
1. Connect your ESP32 board to your computer via USB.
2. Open **WiFeY Studio** and click **🔌 Open USB Provisioning Tool**.
3. Select the COM port, enter your local Wi-Fi SSID, Wi-Fi Password, and a unique Device ID (e.g., `node_1`).
4. Click **Compile & Provision via USB**. The board will be flashed with the WiFeY Pull-OTA bootloader.

### 2. Deploying Over-The-Air (OTA) Updates
1. Open the **WiFeY Studio Main IDE**.
2. Select or enter your **Target Fleet / Device Topic** (e.g., `FleetName/devices/node_1/update`).
3. Write your standard Arduino C++ code inside the editor.
4. Click **🚀 OTA Compile & Update**.
5. The application will compile the binary locally, cache core libraries, upload the `.bin` to Cloudflare R2, and update the global version manifest.
6. The targeted ESP32 will automatically detect the new manifest version during its next check-in, download the binary securely over HTTPS, and execute the update.

---

## 📂 Project Structure

```text
WiFeY-Studio/
├── app.py                  # Main Flask application, API endpoints & native GUI
├── WiFeY-Installer.py      # Automated pipeline & PyInstaller standalone builder
├── wifey_db.json           # Persistent fleet & device registry database
├── .env                    # Secret API keys & domain config (Create manually)
├── .env.example            # Template for environment variables
├── build_cache/            # Multi-core smart compilation cache (Automated)
├── core_cache/             # Arduino core binary cache (Automated)
└── dist/                   # Output directory for compiled EXE
```

---

## 🤝 Contributing

Contributions, bug reports, and feature requests are welcome! Feel free to check the issues page or submit a pull request.

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## 📜 License

Distributed under the MIT License. See `LICENSE` for more information.
