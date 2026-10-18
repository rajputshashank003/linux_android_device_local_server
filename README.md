# MyForever — local file server for Termux and Python

MyForever is a password-protected file manager that runs on your device. It uses Python's standard library, so the core server does not need pip packages, a database, root access, or a Cloudflare account.

The server listens on port `8000` and serves files from the `shared/` folder beside the scripts. The first run creates that folder if needed and asks you to choose a password for the current server session. Sign in with username `admin` and that password. An optional **Agent** tab can search supported documents and answer questions through OpenRouter; it requires your own OpenRouter API key and sends selected excerpts to that provider.

## Fastest local start

After downloading or cloning this repository, open a terminal in its folder and run:

```sh
bash run_myforever.sh
```

Open this address on the same device:

```text
http://127.0.0.1:8000/
```

Keep the terminal running while you use the server. Press **Ctrl+C** in that terminal to stop it. Restart with the same command; you will choose a password again for the new session.

## Install on Android with Termux

1. Install Termux from a trusted distribution source and open it.
2. Install Python and Git:

   ```sh
   pkg update
   pkg install python git
   ```

3. Clone this project and enter its folder:

   ```sh
   git clone https://github.com/rajputshashank003/linux_android_device_local_server.git
   cd linux_android_device_local_server
   ```

4. Start MyForever:

   ```sh
   bash run_myforever.sh
   ```

5. Open `http://127.0.0.1:8000/` in the tablet or phone's browser.

To use Android shared storage, copy files into the repository's `shared/` directory using a file manager, or grant Termux storage access with `termux-setup-storage` and copy files from the resulting `~/storage/shared/` path. MyForever only serves files inside its own `shared/` folder.

## Install on Linux or macOS

Install Python 3 and Git with your operating system's package manager, clone the repository, then run:

```sh
git clone https://github.com/rajputshashank003/linux_android_device_local_server.git
cd linux_android_device_local_server
bash run_myforever.sh
```

Open `http://127.0.0.1:8000/` on that computer. The server binds to all network interfaces so other devices on your private Wi-Fi can also connect using this computer's LAN IPv4 address, for example `http://192.168.1.25:8000/`. Allow that connection only on a trusted private network if your operating system firewall asks.

## Windows

Install Python 3, download or clone this repository, open PowerShell in the project folder, and run:

```powershell
New-Item -ItemType Directory -Force shared
py -3 server.py
```

Open `http://127.0.0.1:8000/`. To stop the server, press **Ctrl+C** in PowerShell. (The Bash helper scripts are for Termux, Linux, and macOS.)

## Optional: use your own Cloudflare Tunnel

Cloudflare is **not needed** for local or same-Wi-Fi access. For access from outside your home network, use your own Cloudflare account and tunnel; do not share your tunnel token or put it in this repository.

1. Start MyForever and leave it running.
2. Install `cloudflared` for your operating system using Cloudflare's official instructions.
3. In Cloudflare Zero Trust, create or select a tunnel and configure a public hostname to forward to:

   ```text
   http://127.0.0.1:8000
   ```

4. Use Cloudflare's own connector instructions to run that tunnel on the same device. Keep the tunnel credential private.
5. Visit the HTTPS hostname Cloudflare assigns/configures. MyForever should still ask for its `admin` password.

A custom domain requires a domain managed by the relevant Cloudflare zone and an appropriate DNS route. This repository does not create tunnels, edit DNS, or contain anyone's Cloudflare credentials. The server must stay running for the tunnel to reach it.

## Optional: enable the file Agent

The Agent tab lists and lets you tag files/folders in `shared/`, then searches up to 500 supported documents and answers from relevant excerpts. It does not rename, move, upload, or delete files. Tags are stored separately in the ignored local file `.myforever_agent.json` and are not uploaded to GitHub. Supported text includes common TXT, Markdown, CSV, JSON, YAML, source-code, and log formats; PDF text extraction is optional through `pypdf`. JPG/JPEG, PNG, WebP, and GIF images can be analyzed when explicitly selected with the vision model. Images are limited to 5 MiB each, three per request, and 8 MiB total. Other binary files are listed and taggable but are not analyzed.

1. Create your own API key in your OpenRouter account. The key is not included in this repository.
2. In Termux, run:

   ```sh
   bash configure_openrouter.sh
   ```

   Paste the key when the terminal asks; input is hidden. The script stores it outside the repository in `~/.config/myforever/openrouter_key` with private file permissions. Never put a key in `server.py`, `ui.html`, chat, or GitHub.
3. Start MyForever and open the **Agent** tab. The key is read by the server and never sent to the browser.
4. For PDF question answering, install the optional pure-Python extractor, then restart the server:

   ```sh
   python -m pip install -r requirements-agent.txt
   ```

   Without `pypdf`, PDFs remain visible and taggable but their text is not included in answers.

The default model is Nemotron 3 Ultra through OpenRouter's free endpoint. The model menu also includes a vision model for selected images, plus other free model options. Free model availability, provider logging practices, rate limits, and quality can change. The Agent locally caps requests at 45 per 24 hours and spaces requests by at least three seconds; these local counters reset when the server restarts. The provider receives the question and up to 14,000 characters of selected/retrieved text, plus only images explicitly selected for that request. It does not upload the entire `shared/` folder. Free providers may log prompts for service improvement, so do not select private, personal, or confidential content unless you accept sending it to OpenRouter and its selected provider. The Agent requires internet access from the server device.
## Useful project commands

The simple local launcher starts only the Python file server:

```sh
bash run_myforever.sh
```

The Termux supervisor starts the existing server and attempts the local Wi-Fi proxy and mDNS services. It may also run a Cloudflare connector if you have separately configured one:

```sh
bash start_myserver.sh
```

Run Termux diagnostics with:

```sh
bash diagnose_myserver.sh
```

The server uses port `8000` by default. The startup scripts launch the existing server; do not run a second copy on a different port for the normal setup.

## Files, privacy, and security

- Put files you want to manage in `shared/`. That folder is ignored by Git and is not part of the project upload.
- The app's file operations stay within `shared/`; path traversal and symlink access are rejected.
- Authentication is enabled. Choose a strong password. The password is held only for the running server session and is not written to project files.
- Local access uses HTTP. On an untrusted network, HTTP does not encrypt passwords or file contents. Use a trusted private network or a properly configured HTTPS tunnel for remote access.
- Do not port-forward port `8000` directly from your router. For internet access, use a tunnel and keep its credentials private.
- Chat and clipboard state are held in memory and reset when the Python server stops. This project does not record video calls.
- Android may suspend background apps. Keep Termux open and disable battery optimization for Termux if you need it to stay active; Android can still stop processes.

## What is included

- Password-protected file browsing, upload, download, preview, rename, move, delete, search, and ZIP operations.
- Responsive mobile and desktop UI, including file-list loading placeholders and PDF.js assets.
- In-memory clipboard and global chat routes in the existing server.
- Optional authenticated Agent tab with file tags, OpenRouter text and selected-image analysis, and lightweight animated request states.
- Optional Termux LAN proxy, mDNS responder, startup supervisor, and diagnostics.

The project does not include a database or external service requirement for local use.
