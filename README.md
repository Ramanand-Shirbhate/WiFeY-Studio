# 🌐 WiFeY Studio

WiFeY Studio is a secure, automated, global Over-The-Air (OTA) firmware compiler and USB provisioning tool designed for ESP32 IoT fleets. 

Built with a hybrid Python/Flask backend and a native desktop UI (via PyWebView), this tool allows you to write C++ Arduino code, instantly compile it in the cloud, and push updates to remote IoT devices anywhere in the world via Cloudflare R2 and Ngrok.

## ✨ Features
* **Zero-Setup Auto-Installer:** On first launch, the app silently downloads the official Arduino CLI, Espressif ESP32 hardware cores, and necessary libraries in the background. No manual environment setup required!
* **Native Desktop Experience:** Runs completely independent of a browser with a zoom-optimized, dark-themed UI.
* **Smart Build Cache:** MD5-hashes your code to prevent redundant compilations. If the code hasn't changed, it bypasses the compiler, saving massive amounts of time.
* **Global OTA Architecture:** ESP32 devices use a "pull model" to securely check an auto-generated Ngrok API for new firmware manifests, downloading binaries directly from Cloudflare R2.
* **Integrated Serial Monitor:** Built-in real-time USB serial monitor and automatic COM port detection.
* **Fleet Management Topics:** Update a single specific device (e.g., `node_1`) or an entire fleet simultaneously.

---

## ⚙️ Configuration (Required)

Because this tool interacts with secure cloud infrastructure, you must provide your own API keys. 
Create a file named `.env` in the exact same directory as your script (or `.exe`) and add your credentials:

```env
# .env
NGROK_STATIC_DOMAIN=your-static-domain.ngrok-free.dev
R2_ACCOUNT_ID=your_cloudflare_r2_account_id
R2_ACCESS_KEY=your_r2_access_key
R2_SECRET_KEY=your_r2_secret_key
R2_BUCKET=wifey-firmware
