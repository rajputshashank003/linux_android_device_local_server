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

## Keep MyForever running in Termux

The Termux deployment uses the existing `lan_host.py --manage-server` supervisor. It manages the app server, LAN proxy, mDNS, and the already-configured Cloudflare connector; it does not create another tunnel. A detached manager command is provided so closing the visible terminal does not end the supervisor, and the supervisor now retries a crashed `server.py` with a bounded backoff.

Unattended boot needs the server password available privately. The old password prompt only kept the password in the running process, so it cannot be recovered after that process has stopped. If you know the existing password, save that same password; if it was only in memory and is now lost, choose a new password. This does not overwrite an existing password file:

```sh
bash myforever_service.sh configure-password
```

Then start and inspect the service:

```sh
bash start_myserver.sh             # detached persistent supervisor
bash myforever_service.sh status
bash myforever_service.sh logs
bash myforever_service.sh restart  # restarts only its managed file-server child
bash myforever_service.sh stop     # stops its supervisor and owned children
```

For an interactive foreground supervisor, use `bash start_myserver.sh --foreground`. `bash run_myforever.sh` remains the standalone foreground server command; it is not crash-supervised and does not start the LAN/tunnel manager.

To request automatic start after Android reboots:

1. This device is running the Google Play Termux build (`googleplay.2026.10.09`), whose boot-script support is integrated into the main app; a separate Termux:Boot install is not required. The checked-in hook is `termux/boot/10-myforever.sh`. To install it in this checkout, run `mkdir -p ~/.termux/boot && cp termux/boot/10-myforever.sh ~/.termux/boot/ && chmod 700 ~/.termux/boot/10-myforever.sh`. For F-Droid/GitHub Termux builds, install Termux:Boot from the exact same signing source as Termux, open it once, and do not mix APK sources.
2. The boot hook waits for Android startup, then idempotently calls the service manager, which requests a wake lock when `termux-wake-lock` is available. It does not start a second Cloudflare connector. Enable/allow Termux's boot/background permission in Android if shown.
3. On Samsung, set Termux battery use to **Unrestricted**, allow background activity, remove Termux from Sleeping/Deep sleeping apps, and enable Auto start if the firmware offers it. Settings labels vary by Android/One UI version.
4. Termux:API must be installed from the same source for the wake-lock command. The hook continues without it, but the CPU may sleep sooner. To release the wake lock intentionally, run `termux-wake-unlock` after stopping the service.

Android can still terminate background processes. Swiping Termux away may be tolerated on some builds; **Force stop** blocks background/boot execution until Termux is opened again. Wake lock does not prevent every kill, and boot automation cannot guarantee uptime after power loss, Android restrictions, or network outages. After reboot, verify with `bash myforever_service.sh status` and inspect `bash myforever_service.sh logs`. Cloudflare Tunnel reconnection remains owned by the current supervisor; public reachability also requires internet connectivity.

## Phase 1 execution-engine diagnostics

The optional Termux execution-engine foundation is isolated from the file server. It does not install a runtime, run images, change the server, or expose an API. See [`execution_engine/README.md`](execution_engine/README.md) for the runtime evaluation and limitations. Run its local-only checks with:

```sh
python -m execution_engine.diagnostics
```

A passing process/filesystem/loopback probe does not mean Docker/OCI image execution is verified. The diagnostics report OCI support separately.

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

## Optional: Direct Domain Hosting (experimental)

Direct Hosting is independent of both `myserver.local` and Cloudflare Tunnel. It uses a separately managed Caddy reverse proxy on this Android device and forwards to the existing authenticated `127.0.0.1:8000` service. It does not expose port 8000 or change DNS by itself.

On Termux, run diagnostics from the repository directory:

```sh
python direct_host.py diagnose
```

This checks the local origin, the observed outbound IPv4/IPv6, DNS authority and records, and local port availability. An outbound public IP lookup is not proof that inbound connections work. A private Android Wi-Fi address means router forwarding is normally required; router WAN details are needed to distinguish router NAT from ISP CGNAT. Test from a device on a different network before considering hosting live.

Prepare a dedicated hostname configuration (this only writes private local files):

```sh
python direct_host.py configure direct.costop.in
```

Review the displayed authoritative DNS provider. Add a DNS-only `A` record for that dedicated hostname at the provider that is actually authoritative, pointing to the current public IPv4. Do not replace existing website, mail, or Cloudflare records. If the address changes, update that record manually unless the supported GoDaddy updater is explicitly configured. The optional GoDaddy updater only works when GoDaddy is authoritative and updates a reviewed, existing single A record; it is unavailable while another DNS provider is authoritative. If GoDaddy becomes authoritative after reviewing and copying every required record, configure it with `python ~/direct_host.py configure-godaddy costop.in direct --confirm-host direct.costop.in`; the terminal prompts for the API key and secret using hidden input. Do not put credentials in a command, browser, or repository.

On a router with a public WAN address, forward external TCP 80 to the Android device's current LAN address on port 8080, and TCP 443 to that device on port 443. Keep a stable LAN address/DHCP reservation. If the ISP blocks inbound traffic or uses CGNAT, request a public address from the ISP; DNS and certificates cannot bypass that restriction. The Android device must permit Termux background activity.

After DNS and router setup, install Caddy from the Termux package repository (`pkg install caddy`), review the generated configuration, then start only after the app confirms the prerequisites and you type the configured hostname. Caddy obtains and renews a trusted HTTPS certificate when public DNS and validation ports work. Setup/start commands are also available from the authenticated Direct Hosting tab. Stop only this optional proxy with `python direct_host.py stop`; inspect state/logs with `python direct_host.py status` and `python direct_host.py logs`.

Direct hosting is not enabled by default. Public access exposes the authenticated file manager to the internet; use a strong unique password. Android may suspend background processes or lose network connectivity, so continuous uptime is not guaranteed. No direct internet route or certificate is claimed until tested externally.

## Useful project commands

The simple local launcher starts only the Python file server:

```sh
bash run_myforever.sh
```

The Termux supervisor starts the existing server and attempts the local Wi-Fi proxy and mDNS services. It may also run the already-configured Cloudflare connector:

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
- Authenticated Agent and Agentic Chat, OpenRouter providers, private llama.cpp models, persistent local conversations, bounded PDF summaries, selective local document indexing, and local lexical retrieval.
- Optional Termux LAN proxy, mDNS responder, startup supervisor, and diagnostics.

Application metadata, conversation history, and local document chunks use a private SQLite database; no external database service is required.

## Optional: Local AI (experimental, CPU-only)

The Agent tab includes a separate **Local AI** provider. It uses the Termux `llama-cpp` package and an official Qwen2.5 0.5B Instruct Q4_K_M GGUF model. The model is approximately 491 MB and Apache-2.0 licensed. The model manager downloads it only after the user presses **Download model** and confirms. Downloaded weights and state live under private Termux paths (`~/.local/share/myforever/models` and `~/.config/myforever`), outside `shared/`; they are not served by the file browser or committed to Git.

On Termux, install the lightweight native runtime once:

```sh
pkg install llama-cpp
```

Restart MyForever using the existing startup method so its authenticated Local AI routes are loaded. In the Agent tab, choose **Local AI**, review device memory and storage status, press **Download model**, then **Load model**. No model is downloaded or loaded at startup. Loading is blocked unless at least 1,200 MiB is currently available. Defaults are a 1,536-token context, two CPU threads, one request slot, a 384-token output cap, and a five-minute inference timeout. These are conservative settings, not a promise of performance or memory use; Android may still terminate an inference process under memory pressure. Use **Unload model** to release it. Deletion requires a separate confirmation.

The app verifies the downloaded model with its published SHA-256 before installation. Local inference requests go only to a `llama-server` process bound to `127.0.0.1:8765`; that port is not added to the website or tunnel. All model-management and chat routes use the existing MyForever authentication. Local AI has no fallback to OpenRouter. Selected text and PDFs are parsed locally; scanned PDFs require OCR, which this text-only model does not provide. Image pixels are not sent to Local AI. PDF summarization is chunked and capped at 40,000 extracted characters / 20 chunks and uses temporary request memory only. The separate Agentic Chat history is persisted in a private SQLite database. Existing Agent tab request state remains bounded; local retrieval uses a small lexical index rather than an embedding/vector service.

Model download requires internet once. After a verified download, the model itself can run without internet; the website must still be reachable by the browser through your existing local or public hosting path. When you access the website through Cloudflare Tunnel, site traffic still uses that existing network route; Local AI simply avoids calls to external AI providers. No DNS, tunnel, server port, or authentication configuration is changed for this feature. OpenRouter remains a separate provider and still sends its prompts/selected files to external model services.

Useful Termux checks (from the project directory):

```sh
python -c 'import local_ai,json; print(json.dumps(local_ai.status(),indent=2))'
python -c 'import local_ai,json; print(json.dumps(local_ai.unload(),indent=2))'
```

If the model process fails, inspect **View local logs** in Model management. The file server remains available when Local AI is missing or fails. The local model has not been bundled in this repository.


## Protected server activity logs

The authenticated **Logs** tab shows recent HTTP activity, file-search terms, Agent request starts/completions, chat join/leave/message counts, and tails from the existing supervisor, Cloudflare Tunnel, Local AI, Direct Hosting proxy, and DNS updater logs. Search terms and client IP addresses are retained. Prompts, chat message bodies, document contents, passwords, and API credentials are not written to the new audit log. The existing server uses one shared password, so an IP address is only a connection clue; Cloudflare Tunnel visitors may share a source address.

Structured activity is stored privately outside `shared/` at `~/.local/state/myforever/activity.log`, with 2 MiB rotation and five backups (maximum about 12 MiB). The viewer is protected by the existing server authentication. It shows up to 500 recent events and up to 60 lines for each known component log. It does not collect Android system logs or unrelated Termux shell history.


## Local AI platform additions

The Agent model manager includes the existing Qwen2.5 0.5B Instruct Q4_K_M model and an optional **Qwen3.5 0.8B Q4_0** registry entry. The latter is listed as a 563,036,064-byte model from the ggml-org Hugging Face GGUF repository, with the published SHA-256 recorded for download verification. It is not downloaded automatically. Download it only from the Agent model manager after reviewing its size and license. The runtime has not been smoke-tested against this model on this device, so do not treat it as verified until an actual model load and inference succeeds. Model switching unloads the previous model first and keeps the loopback inference endpoint private. The existing 1,200 MiB available-memory guard still applies.

A private SQLite database is created at `~/.local/share/myforever/platform.sqlite3`, outside the public Shared directory. It stores conversations, messages, index metadata, and text chunks. The browser creates a random owner identifier to keep conversation lists separate in the shared-password deployment. Protect browser storage and the existing server password; users with access to the same browser profile can access its chat history.

The **Health Endpoint** tab uses authenticated `/api/health` data. It samples CPU, `/proc/meminfo`, server RSS, storage, SQLite, model/runtime state, local index progress, Cloudflare readiness (when its local readiness endpoint is available), direct proxy state, and recent sanitized errors. Some Android/Termux metrics may be unavailable; the dashboard labels them accordingly. It polls every four seconds only while that tab is open.

The **Agentic Chat** tab adds saved conversations, rename/delete, provider selection, and file attachments from Shared or the device. A device attachment is uploaded to the existing Shared directory through the existing authenticated upload API. OpenRouter requests send the conversation and explicitly attached files to the selected external provider. Local AI does not use remote fallback. Local RAG retrieves a small number of matching chunks from files explicitly selected in the **Refresh index** dialog; PDFs require extractable text, and scanned PDF OCR is not added. Retrieval uses bounded SQLite-backed lexical matching, not embeddings.

Database initialization is included in server startup and is safe to repeat. Existing local/Python server, Cloudflare Tunnel, DNS, authentication, and direct-hosting configurations are not changed by these UI/API additions. The added endpoints still use the existing server authentication. The existing server process must be restarted through its usual startup process before it serves the new routes and UI.

## Optional: Hosting trusted OCI images on this device

The authenticated **Host** page at `/host` is served as a separate document from the home application and uses Termux PRoot-Distro to pull and run ARM64-compatible OCI images locally. It supports multiple concurrent deployments, each with its own stable deployment ID, reserved loopback host port, runtime process, logs, and `/hosted/<id>/` route. Ports are selected from 20000–29999 after checking persisted assignments, reserved MyFile services, and current listeners. Available RAM and storage warnings are advisory; device resources still limit practical capacity. The deployment uses the local SQLite database and keeps app logs/configuration in private Termux storage. The `nginx:alpine` deployment, local proxy, stop/restart, and delete path were exercised on a Samsung SM-X115 (Android 16, ARM64).

This is not Docker-equivalent isolation: PRoot shares the host kernel/network namespace and has no CPU/RAM cgroups. OCI image ports may be directly reachable to LAN clients; source-based Node.js apps bind their assigned host ports to loopback. Only deploy trusted images; do not use it for untrusted or multi-tenant workloads. Publishing an app bypasses the MyFile password for that specific path. Existing Cloudflare Tunnel/DNS were not changed and external reachability was not verified. See [the hosting platform guide](docs/hosting-platform.md) and [execution-engine details](execution_engine/README.md).
