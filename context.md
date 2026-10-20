# MyForever / MyFile — Project Context and Reliability Handoff

Updated: 2026-10-20. This document is a handoff for the next coding agent. It records repository evidence and the latest device-side checks available at the time of writing. Older rollout notes are historical; Android lifecycle behaviors not physically exercised remain unverified.

## 1. Project overview

MyForever is a local-first file server and AI platform, with a self-hosted application hosting feature. The checked-out repository is `$HOME/.github-push-review`; the live deployment is a separate copy in `$HOME` (the Termux home directory). Do not assume edits to this checkout alter the live service. The live source of truth is the installed files under `$HOME`, its private configuration, and the SQLite database. See [README.md](README.md), [docs/hosting-platform-progress.md](docs/hosting-platform-progress.md), and [docs/termux-reliability.md](docs/termux-reliability.md).

Verified device details from the edge-case audit: Samsung SM-X115, ARM64, Android 16/API 36, non-root Termux Google Play build `googleplay.2026.10.09`. This differs from earlier project requests describing Android 10; re-check device details before relying on platform assumptions. The MyFile backend runs directly in Termux as Python, not inside Docker or PRoot. PRoot-Distro is used separately by the hosting engine for hosted workloads. `server.py` uses Python's standard-library threaded HTTP server, serves port 8000 and defaults to `0.0.0.0` (`MYFOREVER_BIND_ADDRESS` can override it). Existing authentication is retained; an unauthenticated request to `/` returns HTTP 401 as the expected password challenge.

`server.py` contains backend routes and serves `ui.html` plus separate hosting documents. It handles file upload/download, previews and file operations, AI/chat APIs, health APIs, and authenticated hosting management. `ui.html` is the main application UI. `hosting.py`, `hosting_runtimes.py`, `hosting_registry.py`, `hosting.html`, and `execution_engine/` implement hosting. Hosting includes administrator-managed local PRoot/OCI and source deployments, per-app lifecycle/logs/health, SQLite metadata, and `/hosted/<id>/` proxy routes. PRoot is not Docker-equivalent or kernel-isolated; do not represent it as a secure sandbox for arbitrary multi-tenant images. Optional local AI and OpenRouter features remain distinct from core file hosting.

`app_db.py` embeds SQLite; there is no separate database daemon. It stores conversations/messages, document index metadata/chunks, application settings/index jobs, and hosted-app records. Its default path is `$HOME/.local/share/myforever/platform.sqlite3`, outside the served `shared/` directory. Schema version is 3. SQLite uses WAL, foreign keys and a busy timeout. User files live in `$HOME/shared`; do not delete, overwrite, or commit them. Model artifacts and credentials remain outside Git.

`lan_host.py --manage-server` is the single process supervisor for the backend and its managed LAN proxy, mDNS advertiser, and existing `cloudflared` process. `myforever_service.sh` starts/stops and inspects this supervisor. The checked-in boot hook `termux/boot/10-myforever.sh` is installed at `$HOME/.termux/boot/10-myforever.sh`. `myforever_android_watchdog.sh` is invoked by an Android JobScheduler fallback. Local backend readiness does not depend on internet or tunnel availability. Public access uses the existing Cloudflare Tunnel and hostname `files.costop.in`; tunnel configuration/token is private at `$HOME/.config/cloudflared/token` and must never be printed or committed. Tunnel metrics are loopback-only at `127.0.0.1:20241`.

## 2. Existing automatic recovery architecture

The following mechanisms exist in the current checkout. Live activation is separately recorded in the progress report. File names refer to repository files unless a live path is stated.

| Mechanism | Implementation and behavior |
|---|---|
| Detached startup / single manager | `myforever_service.sh start` checks required files, storage and private password file, takes a `flock` start lock when available, verifies the current supervisor identity, and launches `lan_host.py --manage-server` detached with `setsid`/`nohup`. It avoids starting another verified supervisor. |
| Supervisor and child lifecycle | `lan_host.py` starts/monitors `server.py`, LAN address proxy, mDNS and cloudflared. Managed identities (PID plus `/proc` start ticks and command matching) persist privately. `ManagedProcessReference` uses pidfds where available and validates identity before signaling. A restarted supervisor adopts only its matching surviving children; it does not adopt arbitrary listeners. |
| Backend crash/readiness recovery | `lan_host.py` detects managed child exit on its loop (about 3 s), restarts only the backend with bounded exponential delay (2, 4, 8, 16, 32, 60 s plus jitter). Authenticated `/api/health/ready` is checked at a configurable 30 s default interval. Three consecutive backend-unhealthy checks lead to backend-only restart. Database-unavailable or unverified-auth states do not trigger destructive reset or blind restart. `myforever_service.sh recovery-test` requests a controlled backend restart and waits for two ready checks. |
| Boot after Android restart | `termux/boot/10-myforever.sh` sets Termux paths/environment, waits 20 s for storage initialization, then invokes the idempotent manager. Live hook is `$HOME/.termux/boot/10-myforever.sh`, mode 0700. This Google Play Termux build reports integrated boot support; other builds may require the matching-signature Termux:Boot app. Physical reboot/boot broadcast was not tested. |
| Android scheduled fallback | `myforever_service.sh watchdog-enable` registers persisted JobScheduler job 84532017 with requested 900,000 ms (15 min) period and no network/battery/storage constraints. It runs `myforever_android_watchdog.sh`, which calls idempotent `start`; it does not restart a verified healthy service. Android can defer or suppress jobs. This is outside the supervisor process but still inside the Termux package boundary. |
| Cloudflare recovery | Existing connector launches with its token file and loopback metrics; token is not logged. Cloudflared can reconnect normally. An exited managed tunnel is retried separately with jitter/backoff; internet success plus zero tunnel edges must persist five minutes before only the tunnel restarts. Tunnel/public failure does not restart a healthy backend. DNS, tunnel identity and credentials were not changed. |
| Network classification | `lan_host.py` independently records local API/database readiness, Wi-Fi route, internet probe, tunnel edges, and public hostname response. A 12 s grace is used around missing Wi-Fi routes before old-address proxy handling. Internet/public diagnostics run every five minutes. Local service remains independent of public connectivity. The device-side public check is not an independent external monitor. |
| Resource monitoring / optional AI | `lan_host.py` samples `/proc/meminfo`, storage and, where readable, memory PSI/OOM counters every 60 s; it records RSS/peak RSS for known backend/tunnel processes. `health.py` supplies existing app/model measurements. Low-memory warning defaults to 256 MiB and critical is 160 MiB; inaccessible metrics remain unavailable. New indexing work is deferred below a configurable threshold. Local model loading retains a 1,200 MiB available-memory guard and is not auto-started at boot. It does not kill unrelated apps or automatically unload an active model. |
| Persistent event state and logs | `reliability_state.py` writes bounded private state at `$HOME/.local/state/myforever/reliability.json` using atomic replacement and mode 0600. It records service/restart/readiness/network/resource events without prompts, secrets, raw command lines or private paths. Supervisor log is `$HOME/.local/state/myforever/supervisor.log`; tunnel log is `$HOME/cloudflared.log`; watchdog log is `$HOME/.local/state/myforever/android-watchdog.log`. Logs are bounded/compacted. State-write failures do not stop the server. |
| Database/data safety | Readiness checks DB through authenticated API. SQLite is embedded; there is no DB process to restart. Failures are surfaced and do not trigger DB deletion/recreation. `server.py` upload writes to a random hidden temporary file and renames it only after complete receipt; ordinary failures remove the temp. Hard process/power loss may leave a hidden `.*.upload` temp file; no automatic recursive cleanup occurs. Never “repair” by deleting DB or Shared files. |
| Android wake lock | Manager requests `termux-wake-lock` when available and stop requests unlock. Command availability was verified; Android did not expose an independently readable held-lock state. Wake lock helps CPU sleep but does not prevent force-stop, all memory kills, overheating, or power loss. |

External alerting, automatic failover, data replication, power-on-after-charge, and a recovery controller outside Termux are not implemented.

## 3. Verified environment and source locations

- Repository: `$HOME/.github-push-review`; Git branch `main`; remote `origin`, configured GitHub repo `rajputshashank003/linux_android_device_local_server`; latest pushed commit before this context change: `547720d` (`feat: add Termux reliability recovery layer`). Do not disclose credential-bearing remote URLs if Git configuration changes.
- Live app root: `$HOME`; checkout: `$HOME/.github-push-review`. Live manager `$HOME/myforever_service.sh`; live boot hook `$HOME/.termux/boot/10-myforever.sh`; live watchdog `$HOME/myforever_android_watchdog.sh`.
- Backend: `$HOME/server.py`, Python 3, HTTP port 8000, default bind `0.0.0.0`; supervisor `$HOME/lan_host.py --manage-server`.
- Main start: `bash ~/myforever_service.sh start`; convenience wrapper: `bash start_myserver.sh`.
- DB: `$HOME/.local/share/myforever/platform.sqlite3`, configurable with `MYFOREVER_DB_PATH`; schema version 3. Private config includes `$HOME/.config/myforever/server_password` and `$HOME/.config/cloudflared/token` (names only; do not read or report contents).
- Shared user data: `$HOME/shared`. Private state/logs: `$HOME/.local/state/myforever/`. Earlier deployment backups are under `$HOME/.local/share/myforever/backups/`; edge-audit backups are under `$HOME/.local/state/myforever/reliability-backups/`.
- Public hostname from code default and existing docs: `https://files.costop.in/`. Root HTTP 401 is an expected unauthenticated challenge and indicates a response, not authenticated feature verification.
- LAN file access uses the existing server and private-Wi-Fi address proxy on port 80; Cloudflare Tunnel remains the existing config.
- The `.termux/boot/` hook is checked in as `termux/boot/10-myforever.sh`; installation/update copies it to the live hook and sets mode 0700. Never overwrite private config.

## 4. Existing implementation and test results

### Fresh checks during this context update (2026-10-20)

- `python -m unittest discover -s tests -q`: **PASS**, 99 tests in 30.203 seconds. Python emitted non-failing SQLite `ResourceWarning`s about unclosed test connections; suite result was `OK`.
- `bash ~/myforever_service.sh status`: **PASS at check time**; supervisor PID 8620, port 8000 returned expected unauthenticated HTTP 401, tunnel metrics reported 4 active edges.
- `bash ~/myforever_service.sh health`: **PASS at check time**; local API/database `ready`, recovery state `HEALTHY`, backend/tunnel restart counts `1/0`, device-side connectivity sample `healthy`. Point-in-time readings: available RAM 0.95 GiB, total storage 48.21 GiB, used 22.61 GiB, free 25.48 GiB. These are not a promise of future resources.
- `bash ~/myforever_service.sh watchdog-status`: **PASS**; job 84532017 shown pending, periodic 900000 ms, persisted.
- Live installed boot hook, manager and watchdog were executable (mode 0700).
- Current checkout was clean before this documentation addition; branch/remote checked and recent commit recorded above.

### Historical verification from the 2026-10-20 edge-case audit

These are prior tests, not re-run now: six concurrent `start` calls left one supervisor/backend; backend SIGTERM recovery returned authenticated readiness in 4.08 seconds; supervisor SIGKILL recovery via JobScheduler completed in 7.33 seconds while preserving backend and tunnel PIDs; manually running boot hook was idempotent and did not duplicate the supervisor. A one-shot JobScheduler marker ran in 2 seconds. A prior DB `PRAGMA quick_check` returned `ok`. See [docs/termux-edge-case-audit.md](docs/termux-edge-case-audit.md) for evidence/caveats.

Automated coverage includes restart backoff, managed PID identity/adoption, health thresholds, DB outage handling, memory metrics as measured/unavailable, connectivity classification, scheduler/boot idempotency, bounded journal/log state, and authenticated health/readiness. It does not prove Android UI/OEM lifecycle behavior.

## 5. Current failure matrix

Status meanings: **PASS** = failure injected and recovery observed; **CONDITIONAL** = mechanism exists but depends on Android/runtime/network conditions or was partially tested; **UNTESTED** = not physically injected; **UNSUPPORTED** = current setup cannot reliably recover. More evidence is in [docs/termux-edge-case-audit.md](docs/termux-edge-case-audit.md).

| Scenario | Detector / recovery | Status and delay | Files / residual risk |
|---|---|---|---|
| Backend process crashes | `lan_host.py` detects managed PID exit, restarts backend and verifies authenticated readiness. | **PASS**; 4.08 s observed after controlled SIGTERM; normal polling ~3 s plus startup. | `lan_host.py`, `reliability_state.py`; OOM not tested. |
| Backend manually terminated | Same managed-PID monitor. | **PASS** for controlled SIGTERM; 4.08 s observed. | `lan_host.py`; Android force-stop differs. |
| Terminal session closes | Detached setsid/nohup should outlive terminal. | **CONDITIONAL**, no physical session-close test. | `myforever_service.sh`; verify remotely; says nothing about task swipe. |
| Termux swiped from Recents | Children may continue; scheduler may recover if package not stopped. | **UNTESTED**, no measured delay. | Android/OEM dependent; manual test after battery setup. |
| Android kills Termux under memory pressure | Persisted JobScheduler may run watchdog and recreate manager if package/jobs remain eligible. | **CONDITIONAL**, whole-package kill not induced; requested schedule 15 min or later. | watchdog files; Android can defer or kill again. |
| Termux app crashes/all processes lost | JobScheduler can potentially start recovery script inside Termux package. | **CONDITIONAL**, supervisor-only crash tested, not whole package. | `myforever_android_watchdog.sh`; same package boundary applies. |
| Supervisor crashes | JobScheduler starts watchdog; new supervisor adopts matching children and avoids duplicates. | **PASS** for supervisor SIGKILL; recovery measured at 7.33 s. | `lan_host.py`, manager, watchdog. |
| Health monitor crashes | Monitor is supervisor loop; supervisor recovery applies. | **PASS** for supervisor kill; hung-but-alive loop is not independently detected. | `lan_host.py`; watchdog checks manager presence. |
| Cloudflare Tunnel exits | Supervisor restarts only connector; cloudflared also reconnects. | **UNTESTED** live exit; code retry starts ~16–24 s and backs off; zero-edge grace 5 min. | `lan_host.py`; network/config/DNS can still block public access. |
| SQLite/database failure | Readiness reports degraded; no separate DB process and no destructive reset. | **UNTESTED** injection; live DB ready and prior quick_check passed. | `app_db.py`, `server.py`; backup/manual diagnosis may be required. |
| Android reboots | Hook waits 20 s then starts manager. | **CONDITIONAL**, real boot broadcast not tested. | `termux/boot/10-myforever.sh`; boot delivery, unlock/storage/settings may block. |
| Boot hook fails | JobScheduler may provide delayed recovery if package is eligible. | **CONDITIONAL**, hook manually invoked, broadcast not tested. | Hook/watchdog; inspect status/logs. |
| JobScheduler delayed/unavailable | No faster recovery after supervisor/all-process loss. | **CONDITIONAL**, job registered; actual OEM deferral unknown; nominal 15 min may be longer. | Android owns schedule; force-stop suppresses jobs. |
| Wi-Fi unavailable | Local backend stays up; LAN proxy route grace/rebind. | **UNTESTED**, 12 s grace; no outage injected. | `lan_host.py`, `lan_access.py`; actual reconnect unknown. |
| Internet unavailable, LAN okay | Leave backend alone; cloudflared reconnects; connectivity state recorded. | **UNTESTED**, diagnostics every ~5 min. | `lan_host.py`; public access waits for internet. |
| Backend healthy, public hostname fails | Tunnel/public checks remain distinct; no backend restart or DNS rewrite. | **UNTESTED**, checks approx every 5 min. | `lan_host.py`; device-side probe is not external. |
| Explicit Android Force stop | Android stopped-package rules block jobs/self-start until app opened by user. | **UNSUPPORTED** for automatic recovery; not tested. | Manual reopen Termux; shell/wake lock/boot hook cannot reliably bypass. |
| Power loss / battery depletion | Boot hook only helps if firmware powers device on and boot event/storage access occur. | **UNSUPPORTED** automatic power-on unknown; not tested. | May need charging, Power button, unlock. |
| Aggressive battery saving / Doze | Unrestricted/Never sleeping may improve eligibility; scheduler still deferrable. | **CONDITIONAL/UNTESTED** settings unreadable from shell and unchanged. | Samsung/Android policy; wake lock is not a bypass. |
| Severe memory pressure / optional AI OOM | Warnings/model guard/index deferral; no killing unrelated apps. | **CONDITIONAL/UNTESTED**, no pressure injected. | `lan_host.py`, `local_ai.py`; package death defeats in-process recovery. |
| Storage full / SQLite write failure | Report DB/storage issue; no automatic delete/recreate. | **UNTESTED**, intentionally not induced. | `app_db.py`, `server.py`; safe manual space management/backup. |
| Thermal shutdown / power interruption | OS/firmware handles throttling/shutdown; no app thermal recovery. | **UNSUPPORTED**, not induced. | Cool device/restore power; no availability guarantee. |

## 6. Primary unresolved problem

Backend crashes, supervisor-only crashes and boot startup already have recovery layers. The unresolved objective is recovery when Android stops/kills the **Termux application/package**, especially:

- **Recent Apps/task removal:** swipe Termux away and server processes may subsequently stop. Behavior has not been physically observed on this Samsung build. Task removal, background-process kill and Settings → Force stop are distinct events.
- **Memory-pressure process death:** Android may kill all Termux processes. JobScheduler is registered but can be deferred or disabled with the package. Supervisor-only recovery does not prove recovery after whole-package death. Desired behavior is to resume when Android permits without manually reopening Termux.

Target is approximately 99% practical availability, not a guarantee. Under stock Android, explicit force-stop blocks package jobs/components until the user launches the app. A same-package watchdog, boot hook, wake lock, shell script, or external request cannot reliably bypass this. External monitoring alerts only; it cannot start a force-stopped app or power on the tablet. Stronger recovery might require a native Android foreground-service/companion component, device-owner privileges, root, or a second always-on host, all with security/permission/architecture implications. Do not install or implement these without user authorization. Android 16 is the observed build; investigate current supported APIs and Samsung task-removal behavior rather than extrapolating Android 10 behavior.

## 7. Research and engineering task for the next agent

Investigate actual device lifecycle, first via code/documentation and then controlled physical tests. Separate: terminal close, Recents swipe, OS memory reclaim, application crash and explicit Force stop. Determine whether this Google Play Termux build exposes a supported mechanism that restores Termux after its process is killed while package is not force-stopped. Inspect current Termux components and Samsung One UI behavior; do not assume the periodic job always runs or bypasses stopped-package semantics.

User preference: avoid dependence on a separate third-party automation app. Do not install automation/root tools, grant device-owner/accessibility permissions, modify protected Android components, or weaken security without explaining and obtaining authorization. If only a native Android companion app/service offers stronger recovery, present it first as an optional, permission-scoped approach. A second host is optional and requires explicit replication/ownership/split-brain design; do not introduce it silently.

### Desired future recovery flow

1. Detect backend/supervisor unavailability using real readiness and process identity.
2. Determine whether Termux package/process remains runnable; do not confuse absent child with force-stopped package.
3. Use the strongest supported Android mechanism actually available; retain idempotent manager and JobScheduler fallback.
4. Restart only the missing component, with locks/backoff and no duplicate backend/tunnel.
5. Require authenticated `/api/health/ready` before declaring recovery.
6. Check local access separately from internet, tunnel edges and public hostname.
7. Persist bounded recovery attempts/errors with secrets redacted.
8. Never recover by deleting SQLite, files, models or deployments; stop loops and report exhausted retries.
9. Clearly report Android restrictions that make recovery impossible.

### Proposed acceptance tests (not yet complete)

- **Safe/staging:** backend termination while supervisor lives; supervisor termination with JobScheduler; parallel starts; intentional stop; readiness failure threshold; missing optional AI; tunnel-child restart on staging; no duplicate processes; DB/files unchanged.
- **Manual maintenance window:** from a second LAN client, close only terminal session, then separately swipe Termux from Recents and observe local/public health for 5–15 minutes. Record One UI version, outage time, job execution and recovery. Do not use Force stop for this case.
- **Reboot:** schedule downtime; ensure no upload/model load; reboot; unlock; wait; inspect boot log, status, health, job registration, child count and public route. Do not claim locked-boot behavior without testing.
- **Force stop:** only if user elects to test; expect no automatic recovery until opening Termux. Never label this as self-recovery.
- **Memory/full disk/thermal/power loss:** do not induce on production. Use staging or mark untested; do not exhaust RAM/disk or overheat hardware.
- Mark PASS only when failure and expected recovery were actually observed; otherwise use CONDITIONAL, UNTESTED or UNSUPPORTED.

## 8. Operational commands

These commands exist in `myforever_service.sh` and/or README. Run against live `$HOME` unless intentionally testing checkout scripts.

```sh
# Start/inspect
bash ~/myforever_service.sh start
bash ~/myforever_service.sh status
bash ~/myforever_service.sh health
bash ~/myforever_service.sh watchdog-status
bash ~/myforever_service.sh logs

# Controlled operations (restart affects backend child; stop affects owned stack)
bash ~/myforever_service.sh restart
bash ~/myforever_service.sh recovery-test
bash ~/myforever_service.sh stop

# Scheduler fallback
bash ~/myforever_service.sh watchdog-enable
bash ~/myforever_service.sh watchdog-disable

# Repository tests and syntax checks
python -m unittest discover -s tests -q
python -m py_compile server.py lan_host.py reliability_state.py health.py app_db.py
bash -n myforever_service.sh myforever_android_watchdog.sh termux/boot/10-myforever.sh

# Public root check; unauthenticated 401 is the expected password challenge
curl -i --max-time 20 https://files.costop.in/
```

`health` verifies authenticated local API/database readiness using the configured private password file and reports memory, storage, tunnel metrics and journal summary. `status` checks supervisor identity, local port-8000 response and tunnel metrics. `logs` follows supervisor log. `diagnose` executes `bash diagnose_myserver.sh`. Do not include password or tunnel credential values in commands/reports.

There is no `backup` or `restore` subcommand in `myforever_service.sh`. Earlier deployment reports show SQLite snapshots were made with SQLite online backup before live updates, under dated `$HOME/.local/share/myforever/backups/` directories. The current edge-audit pre-deploy code backup is `$HOME/.local/state/myforever/reliability-backups/edge-audit-20261020-170354/`. Its code rollback commands are in [docs/termux-reliability.md](docs/termux-reliability.md); disable watchdog before restoring saved scripts. Do not restore an old SQLite snapshot for a code-only rollback because it discards later writes. Before any DB restore, make a fresh consistent online backup and assess lost writes. No rollback occurred in this work.

## 9. Current state and next steps

### Implemented

- MyFile file/AI/chat/hosting functions and authenticated APIs.
- Termux detached manager and one supervisor for backend, LAN proxy, mDNS and existing tunnel.
- PID/start-time-safe child tracking/adoption, duplicate-start lock, backend-only recovery, health/readiness checks, tunnel-only recovery, network-layer diagnostics, bounded retries/logs/state, RAM/storage and optional-job guards.
- Executable Termux boot hook and persisted Android JobScheduler watchdog.
- Documentation distinguishing backend/supervisor recovery from Android package/force-stop recovery.

### Verified

- Fresh unit suite: 99 tests passed, with non-failing SQLite `ResourceWarning`s.
- Fresh device check: supervisor PID 8620; backend/database ready; port 8000 expected HTTP 401; Cloudflare metrics four edges; Job 84532017 pending/persisted.
- Historical injected backend and supervisor process failures recovered in 4.08 s and 7.33 s; six concurrent starts and manual boot-hook idempotency were previously checked.

### Untested / cannot guarantee

- Physical Recents swipe; whole-Termux-app memory kill; Android reboot broadcast; locked boot; Wi-Fi/internet outage; tunnel process exit; repeated real crash loop; disk-full/SQLite failure; thermal shutdown; battery depletion/auto-power-on.
- Android battery exemption setting was unreadable from shell and unchanged. Wake-lock command ran, but OS-held state could not be measured.
- No independent external monitor or failover host is configured. Device-side public check cannot run while Termux/device is offline.
- Stock Android force-stop blocks reliable self-restart. Power-on after total battery depletion is hardware/firmware behavior, unknown here.

### Next engineering task (priority)

Run a controlled physical **Recent Apps removal** test on this SM-X115 after the user confirms a maintenance window, with a second device monitoring LAN and public health. First record Samsung battery/background settings and One UI build; then distinguish task swipe from Force stop. Measure outage/recovery and whether JobScheduler executes. Research only supported Android/Termux mechanisms for whole-package recovery. Do not alter Android settings or add a companion/automation app without permission. If stock Termux cannot recover after package death, report that boundary and propose the least-privilege native companion or optional second-host architecture; do not promise 99% until observed availability data supports it.

Likely code review points only if test evidence justifies changes: `myforever_service.sh`, `myforever_android_watchdog.sh`, `termux/boot/10-myforever.sh`, `lan_host.py`, `reliability_state.py`, and `tests/test_termux_persistence.py`. Avoid competing supervisors/watchdogs or DNS/tunnel credential changes.
