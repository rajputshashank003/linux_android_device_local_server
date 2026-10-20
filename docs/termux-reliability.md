# MyForever reliability on Android / Termux

## Audited deployment

This machine is a non-root Samsung SM-X115-class ARM64 tablet running Android 16 and Google Play Termux (the installed environment was observed directly during this audit; earlier project notes mention Android 10, so re-check after any OS change). The core MyForever server runs directly in Termux, supervised by `lan_host.py`; it is not in Docker or PRoot. Hosted applications may use PRoot-Distro independently.

- Live project / working directory: `$HOME` (`/data/data/com.termux/files/home`). The review checkout is `$HOME/.github-push-review`.
- Main server: `server.py`, port 8000, launched by the supervisor with the existing private password file.
- Supervisor: `lan_host.py --manage-server`, started detached by `bash ~/myforever_service.sh start`; a `flock` lock and verified PID prevent duplicate managed supervisors.
- LAN discovery/access: existing Wi-Fi address proxy and mDNS child managed by the same supervisor.
- Cloudflare Tunnel: same supervisor, existing `cloudflared tunnel run --token-file ...`, loopback metrics at `127.0.0.1:20241`. Tunnel token/configuration and DNS were not modified.
- Database: embedded SQLite at `~/.local/share/myforever/platform.sqlite3`; there is no separate database service/process.
- Files: `$HOME/shared`; model files and settings remain in their existing private Termux locations.
- State: `~/.local/state/myforever/reliability.json`; bounded operational history only, mode 0600, atomic replace. Supervisor log is `~/.local/state/myforever/supervisor.log`; tunnel log is `$HOME/cloudflared.log`. Live append-only logs are compacted when they exceed 2 MiB, retaining roughly the newest 1 MiB.
- Boot script: repository copy `termux/boot/10-myforever.sh`; the installed hook is `$HOME/.termux/boot/10-myforever.sh`, executable. It sets an explicit Termux environment, waits 20 seconds then invokes the idempotent service manager.
- Android process fallback: persisted JobScheduler job 84532017 invokes `$HOME/myforever_android_watchdog.sh` every 15 minutes or later; it is enabled with `bash ~/myforever_service.sh watchdog-enable`. The Termux `termux-api` package was installed (no companion APK or sensitive permission); a one-shot JobScheduler marker ran successfully. The fallback is subject to Android scheduling restrictions and force-stop.
- Supervisor crash recovery: managed child PIDs and process start times are persisted privately. A restarted supervisor adopts only matching saved processes, avoiding needless server/tunnel restarts and rejecting PID reuse.

The live state before this additional layer was healthy: supervisor PID 21298, backend PID 21880, port 8000 returned the expected unauthenticated HTTP 401 challenge, authenticated readiness and SQLite were healthy, and Cloudflare metrics showed four active edges. The SQLite read-only `PRAGMA quick_check` returned `ok`. No user files or database rows were changed by the implementation.

## Implemented recovery layer

- The existing supervisor remains the sole backend/tunnel/process manager. Its `flock` and the manager's PID verification are retained.
- Backend crashes use bounded exponential delay with jitter; repeated local readiness failures trigger a backend-only restart after three failed 30-second probes. Database-unavailable and authentication-unverified results do not cause blind backend restarts.
- Startup checks the persistent `shared` directory before starting services. Tunnel startup waits for a local HTTP listener. The tunnel remains optional to local access.
- Cloudflared reconnects normally while its process remains alive. An exited connector is retried with bounded jitter. If internet probes succeed but tunnel metrics report zero edges continuously for five minutes, only the managed tunnel is restarted. Public-route failure does not restart the backend.
- Every five minutes, low-cost device-side probes independently record local backend+database readiness, private Wi-Fi route availability, HTTPS internet reachability, loopback tunnel edge count, and the existing public hostname response. The public probe treats the expected root authentication challenge as reachable. Probe results do not carry a password or response body into state.
- Every 60 seconds, the supervisor samples `/proc/meminfo`, storage, memory PSI (`/proc/pressure/memory`) and the kernel OOM counter (`/proc/vmstat`) where accessible. It records RSS/peak RSS for the known managed backend and tunnel PIDs. SQLite is identified as embedded in the backend. Local inference RSS is read from the existing authenticated health API; model-weight memory is not inferred from process RSS.
- Pressure states: warning below the configured `MYFOREVER_LOW_MEMORY_WARNING_MB` threshold (default 256 MiB), critical below 160 MiB, and unavailable when the kernel metric cannot be read. No unrelated Android processes are killed. The existing model activation guard still checks its model-specific minimum available memory (currently 1,200 MiB); new document-index jobs are deferred below `MYFOREVER_OPTIONAL_JOB_MIN_MEMORY_MB` (default 256 MiB). The deferred indexing request is returned as an error for the user to retry; it is not silently queued. There is no automatic model loading at boot.
- Health Endpoint now shows supervisor recovery state, restart counters, last recovery and successful readiness times, network-layer classification, service memory measurements, and a bounded recent-event list. It is behind the existing authentication because `/api/health` is authenticated.
- `bash ~/myforever_service.sh health` includes the last persisted recovery/connectivity summary. The existing `status`, `logs`, `restart`, `recovery-test`, and `diagnose` commands remain available.

The state journal stores no prompts, user content, database paths, passwords, API keys, tunnel token, or raw process command lines. If the state filesystem becomes unavailable/full, state writes report an unavailable status and do not terminate the supervisor. Logs are diagnostic only; they do not grant management control.

## Connectivity classification and actions

| Observed condition | Recorded state | Action |
| --- | --- | --- |
| Local API/readiness fails | `backend_unhealthy` / `database_unavailable` | Wait for threshold; restart only the managed backend after repeated backend failures. A database failure is reported and not “fixed” by deleting/resetting SQLite. |
| Wi-Fi route unavailable | `wifi_unavailable` | Keep the backend and current proxy during the existing 12-second grace; rebind the LAN proxy when a private Wi-Fi address returns. Public/tunnel state is checked separately. |
| Wi-Fi works, internet probe fails | `internet_unavailable` | Leave backend and tunnel process alone; cloudflared gets its normal reconnect opportunity. |
| Internet works, no tunnel edges | `tunnel_disconnected` | Allow five minutes for cloudflared's normal reconnect, then restart only the managed connector, with backoff. |
| Tunnel edges exist, public hostname fails | `public_hostname_unreachable` | Report a likely ingress/hostname/public-route issue; do not restart a healthy local backend or change DNS/tunnel identity. |
| Public probe responds but local readiness fails | `backend_unhealthy` plus the public status field | Report the discrepancy; backend readiness policy applies independently. |
| Optional AI is missing/unloaded | AI runtime reports stopped/unavailable | Core file hosting remains independent. Model loading is user-triggered and guarded by available RAM. |

The device's public-hostname probe is **not an independent external monitor**. It runs on the tablet and cannot run after Termux/device power is lost.

## Android recovery boundaries

A process watchdog and health monitor inside Termux share the same Android app/process boundary. If Android force-stops Termux, kills all Termux processes, or the tablet loses power, `lan_host.py` cannot recover itself. Termux:Boot can start the manager after a supported Android boot broadcast, but it is not a watchdog for an app process killed while Android remains running. Android may suppress boot/background launches after force-stop until the user opens Termux again.

The installed Termux build is `googleplay.2026.10.09`; upstream states that this Play build has merged Termux:Boot functionality into the main app, so the absence of a separate `com.termux.boot` package is expected. The executable hook exists and a manual invocation was idempotent. A physical reboot was not performed, so boot-broadcast delivery remains **not verified**. Other Termux builds may still require the Termux:Boot add-on from the same APK signing source; open it once and keep `~/.termux/boot/10-myforever.sh` executable. Never mix APK signing sources. The shell could not read Samsung/Android battery-exemption settings due permission restrictions.

Manual Android setup:

1. Android Settings → Apps → Termux → Battery: choose **Unrestricted** / **Allow background activity**, if offered.
2. On Samsung, add Termux (and Termux:Boot if installed) to **Never auto sleeping apps** under Battery / Background usage limits. Do not disable Android security controls globally.
3. Keep Termux's persistent notification/service visible if the build offers it. Do not swipe away or force-stop Termux as an ordinary shutdown method.
4. `termux-wake-lock` is present and the manager requests it at startup. Its OS-held state was not independently measurable in this audit. Use `termux-wake-unlock` after `bash ~/myforever_service.sh stop` when the tablet should sleep. A wake lock can increase battery drain and heat; it does not prevent Android from killing Termux or power loss.
5. A reboot before the first unlock may leave credential-encrypted Termux files unavailable. The server password, SQLite DB and app data are in Termux's private app storage, so boot recovery should be expected only after Android has unlocked user storage. This device's actual locked-boot behavior was not tested.
6. Automatic power-on when charging after battery depletion is a device/firmware feature. It was not verified and is not controlled by this software; do not assume it happens.

Tasker/MacroDroid-style automation was not installed. On stock Android, an automation app may launch a permitted boot action, but cannot reliably observe or restart a force-stopped Termux UID, bypass first-unlock storage, or power on a dead tablet. Root-only init/boot modifications were not used.

## Independent monitoring and optional standby

No external uptime or notification provider is configured. To receive an alert independent of the tablet, configure an uptime checker on another system to request `https://files.costop.in/` at a reasonable interval (for example, 2–5 minutes), accept the existing HTTP 401 password challenge as “origin reachable,” and alert after 3 consecutive failures with a recovery notification. Do not provide it credentials or a private file URL. This only alerts; it cannot restart Termux or power on the tablet. A second host is optional: it should monitor first, and only serve read-only/static fallback content until a deliberate promotion with an explicit data-sync and split-brain plan. Automatic failover and data replication are not implemented.

## Safe commands

```sh
bash ~/myforever_service.sh status
bash ~/myforever_service.sh health
bash ~/myforever_service.sh logs
bash ~/myforever_service.sh recovery-test
bash ~/myforever_service.sh restart   # restarts only supervisor-managed backend
bash ~/myforever_service.sh stop      # stops owned backend/proxy/mDNS/tunnel
bash ~/myforever_service.sh start
termux-wake-lock
termux-wake-unlock
```

For the earlier reliability rollout, see its backup directory in the earlier report. For the final edge-audit code changes, the exact pre-deployment files are in `$HOME/.local/state/myforever/reliability-backups/edge-audit-20261020-170354/`. Disable the JobScheduler fallback first, then restore the pre-edge-audit code and boot hook:

```sh
B="$HOME/.local/state/myforever/reliability-backups/edge-audit-20261020-170354"
bash "$HOME/myforever_service.sh" watchdog-disable
bash "$HOME/myforever_service.sh" stop
cp -p "$B/lan_host.py" "$HOME/lan_host.py"
cp -p "$B/live-reliability_state.predeploy.py" "$HOME/reliability_state.py"
cp -p "$B/myforever_service.sh" "$HOME/myforever_service.sh"
cp -p "$B/10-myforever.sh" "$HOME/.termux/boot/10-myforever.sh"
bash "$HOME/myforever_service.sh" start
```

The backup also contains a consistent copy of SQLite at `$B/platform.sqlite3`, but this change made no schema or database-content changes; do not restore it for an ordinary code rollback. The new journal/module is additive and can remain present. DNS, Cloudflare token/configuration, and tunnel identity were not changed.

## Verification status and limits

- **PASS:** Existing full suite, including connectivity classification, bounded jitter, atomic/private state, log retention helper, process RSS availability, optional-work memory gate, and authenticated health payload checks.
- **PASS:** Python compilation, manager and boot shell syntax, inline UI JavaScript syntax, and `git diff --check`.
- **PASS:** Read-only live service check before activation: local unauthenticated HTTP 401, authenticated API+SQLite readiness, four tunnel metric edges, SQLite `quick_check=ok`.
- **NOT TESTED:** Live backend kill, tunnel termination, Wi-Fi interruption, repeated crash loop, memory exhaustion, physical reboot, force-stop, locked boot, and battery-depletion recovery during this additional layer. These could interrupt the public service or damage device availability; the supervisor crash recovery had been exercised in the earlier reliability pass, but is not claimed as a new test here.
- **NOT CONFIGURED:** Independent external uptime alerting; no provider credentials/integration are present.

Typical backend restart is designed to begin after roughly 1.6–2.4 seconds of initial jitter plus the polling/startup time; subsequent retries follow the existing 2/4/8/16/32/60-second schedule with ±20% jitter. Health-check-triggered recovery waits up to three 30-second failures first. Tunnel process-exit retries start around 16–24 seconds and back off to five minutes; a live connector with no edges is observed for five minutes before a tunnel-only restart. These are policy windows, not measured end-to-end recovery guarantees. Android scheduling, model memory pressure, DB recovery, DNS, network availability, and app startup can extend them.
