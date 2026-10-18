# Termux Wi-Fi file manager

A Python standard-library file manager for the existing `shared/` directory. The app listens on port 8000; `lan_access.py` is a separate port-80 reverse proxy bound to the current private Wi-Fi IPv4 address. No packages, root access, or router changes are needed.

## Existing services and local hostname

The password-protected `server.py` serves the existing `shared/` directory on `0.0.0.0:8000`. `lan_access.py` provides the HTTP port-80 reverse proxy and binds only to the active private Wi-Fi IPv4 address. No router port forwarding is configured.

`lan_network.py` prefers the assigned address on an active `wlan*` or `wifi*` interface using `ifconfig -a`, then falls back to the kernel's UDP route selection. This avoids Android netlink permission requirements and avoids selecting a VPN tunnel when a Wi-Fi interface is available. `mdns_advertise.py` joins `224.0.0.251:5353` on that address, probes `myserver.local` for conflicts, sends an A-record announcement, and answers multicast and legacy-unicast A queries. It refreshes the record and follows Wi-Fi address changes. `lan_host.py` owns one supervisor lock, verifies the HTTP proxy and mDNS response, tolerates short route changes, and can start `server.py` when asked. A healthy server already running on port 8000 is detected and left alone.

Start the complete setup from Termux with:

```sh
bash ~/start_myserver.sh
```

The script validates the Python files, reports the current Wi-Fi address, and starts the supervisor. If `server.py` is already working, it is preserved; otherwise the server starts and prompts for its password in the terminal. Keep the Termux process running in the foreground. Logs are appended to `myserver.log`; use Ctrl+C to stop the supervisor and children it started. A pre-existing `server.py` is left running.

Run future checks with:

```sh
bash ~/diagnose_myserver.sh
```

The diagnostic checks the detected Wi-Fi IP, process list, direct port-8000 response, port-80 forwarding, OS hostname resolution, a legacy-unicast mDNS response, a standard UDP 5353 multicast response, and available logs. Android may deny `ss`, `netstat`, `ip`, or `/proc` socket inspection to unrooted Termux; the script reports that limitation and still performs real connection and DNS queries.

The host resolver currently resolves `myserver.local` to the detected Wi-Fi address and returns the server's expected password challenge. The responder has also received and answered mDNS queries from other Wi-Fi client IPs. The other clients' HTTP sessions cannot be verified from this phone. On a client connected to the same Wi-Fi, test `http://myserver.local/` and compare it with `http://<current-wifi-ip>/`. If the IP works but the name fails, that client or its network must support mDNS `.local` resolution; Android Private DNS, a VPN, browser DNS-over-HTTPS, Wi-Fi multicast filtering, or client isolation can interfere. Termux cannot override those client or router settings. If both URLs fail, check Wi-Fi client isolation and whether the phone remains awake with Termux foregrounded. Android can suspend or force-stop Termux in the background; no in-process supervisor runs after a force-stop or reboot until Termux is started again.

## File manager features

- Browse folders with breadcrumbs; search across nested items; sort by name, date, size, or type; switch between list and grid views.
- Upload multiple files with per-file progress, create folders, rename and move files or folders, and delete after confirmation. Uploads do not overwrite existing names.
- Preview common images, browser-supported audio/video, PDFs, and text/code formats. HTML is served as plain text; other unrecognized formats show file information and a download option.
- Stream media and downloads in bounded chunks and support single byte ranges for seeking. Folder ZIP downloads spool to a temporary file and include only regular files.
- All file operations remain under `shared/`; symlinks and traversal paths are rejected. Requests require the `admin` username and the password entered when starting the server.
- The Clipboard tab shares up to 32 KiB of plain text among signed-in devices. Paste or type text and choose **Share text**; other devices can refresh and copy it. The clipboard is held in memory and clears when `server.py` restarts.

Use this only on a trusted Wi-Fi network. HTTP does not encrypt the password or file contents. The proxy is bound to the phone's private Wi-Fi address; no public router forwarding is configured. Other-device access depends on Wi-Fi client isolation and needs a second device to verify.

## Backups

Before these changes, copies of `server.py`, `ui.html`, `lan_access.py`, and this README were saved under `.backups/`. The `shared/` directory was not copied or modified.
