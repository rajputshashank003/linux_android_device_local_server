# MyForever final Termux edge-case audit

Audit date: 2026-10-20. Device identity was read from Termux: Samsung SM-X115, Android 16 / API 36, ARM64, Termux Google Play build `googleplay.2026.10.09`. The prompt's Android 10 description does not match the live device.

## Current implementation and measured live state

- MyFile runs directly in Termux. `lan_host.py --manage-server` supervises `server.py`, the LAN proxy, mDNS and `cloudflared`; SQLite is embedded in the backend.
- `bash ~/myforever_service.sh start` is detached with `nohup` + `setsid`. The supervisor has an OS file lock. The manager now serializes concurrent starts using `flock` where installed.
- The supervisor persists process IDs together with `/proc` start-time identity. After a supervisor-only crash it adopts only those exact previously managed processes; it uses pidfds when available and validates command identity before fallback signaling. It does not adopt arbitrary listeners.
- Android JobScheduler fallback job **84532017** is registered as persisted, 15-minute periodic, with no network, low-battery, or low-storage requirement. It runs `myforever_android_watchdog.sh`, which calls the existing idempotent `start`; it does not restart a verified healthy supervisor. Android may defer jobs beyond their nominal interval.
- Boot hook: `~/.termux/boot/10-myforever.sh`, mode 0700; manager mode 0700; persistent password and Cloudflare token files mode 0600. Hook sets explicit Termux paths, waits 20 seconds, then calls the manager. Local backend startup does not wait for Internet.
- Live checks: supervisor PID 8620, backend ready, Cloudflare metrics showed four active edges, root returned HTTP 401 as the expected password challenge. Six simultaneous `start` requests left exactly one supervisor and one backend. The DB was not modified.
- The Termux API command package `termux-api` was installed from the already configured Termux repository (27.3 kB download / 365 kB installed). No Android companion APK or sensitive permission was added. A harmless one-shot JobScheduler marker ran in 2 seconds; the persisted periodic job is visible in `watchdog-status`.
- Android `dumpsys`/AppOps queries for battery exemption were denied by the unprivileged Termux UID (`INTERACT_ACROSS_USERS`); Samsung's Never sleeping list could not be read from the shell. No Android setting was changed.

## Failure scenario matrix

“PASS” means the failure itself was injected and recovery observed. Code inspection or unit tests alone are not marked PASS.

| Scenario | Detection and recovery | Actual test / measured time | Status | Remaining limitation / action |
|---|---|---|---|---|
| A. Backend crashes | Supervisor polls its managed PID (~3 s); bounded jittered restart; local readiness verifies recovery. | Sent SIGTERM to the verified backend PID. Supervisor recorded one backend restart and `lastSuccessfulRecovery` 4.08 s after exit; authenticated readiness was observed. | **PASS** | This is one controlled failure, not an OOM or repeated-crash test. |
| B. Backend manually terminated | Same managed-PID path as A. | Same SIGTERM test as A. | **PASS** | Manual process termination is distinct from Android force-stop. |
| C. Terminal shell/session closes | Server/supervisor are detached from terminal using `setsid`/`nohup`, stdin `/dev/null`, logs redirected. | Live server processes had PPID 1; shell hangup was not physically exercised. | **CONDITIONAL** | Close a Termux session (not Force stop) and check `bash ~/myforever_service.sh status`. |
| D. Termux swiped from Recent Apps | Current detached children may continue; Samsung/Android behavior is device/build dependent. Scheduled recovery may help if processes are killed but package is not force-stopped. | No physical swipe test. | **UNTESTED** | Perform the manual Recent Apps test below; remove Termux from Samsung sleeping lists first. |
| E. Android kills Termux under memory pressure | Persisted JobScheduler can relaunch the recovery script after its interval if Android still permits jobs. | Marker and supervisor-crash jobs were tested; no memory pressure was induced. | **CONDITIONAL** | JobScheduler may be deferred or the package may be restricted/killed again. No guarantee. |
| F. Termux app crashes unexpectedly | Same OS-scheduled fallback may recreate the supervisor; it is outside the supervisor process but still belongs to the Termux package. | Supervisor process crash tested; whole Termux app crash not tested. | **CONDITIONAL** | Does not help if Android places package in stopped/force-stopped state. |
| G. Termux force-stopped in Android Settings | Android marks the package stopped and blocks it from self-starting until an explicit user action starts a component. | Not performed; shell cannot safely test this. | **UNSUPPORTED** | User must open Termux again, then run `bash ~/myforever_service.sh start`. A shell script, wake lock, JobScheduler job, or boot hook cannot reliably override force-stop. |
| H. Boot hook/Termux boot component fails | Boot hook is idempotent; persisted JobScheduler provides a later fallback if enabled and package is not stopped. | Hook was run manually: waited 20 s, kept supervisor PID unchanged, no duplicate. A real boot-broadcast failure was not tested. | **CONDITIONAL** | Check `bash ~/myforever_service.sh watchdog-status` and boot log. Scheduler may be delayed; force-stop/restrictions still block it. |
| I. Service supervisor crashes | Persisted Android JobScheduler script calls manager; new supervisor adopts only saved matching backend/proxy/mDNS/tunnel identities. | Killed only supervisor PID 8325 with SIGKILL. JobScheduler relaunched supervisor PID 8620; backend PID 8334 and tunnel PID 8364 were preserved; restart counters stayed 0; readiness recovered in **7.33 s**. | **PASS** | This verifies supervisor process death while the Termux package remains runnable, not whole-package death. |
| J. Health monitor crashes | Monitor is the supervisor loop; same scheduled supervisor recovery applies. | Same supervisor SIGKILL test as I. | **PASS** | A wedged-but-alive supervisor is not currently detected by the periodic fallback. |
| K. Cloudflare Tunnel exits | Supervisor polls managed tunnel; restarts tunnel only, first retry nominal 20 s ±20% (16–24 s), then bounded exponential backoff; backend remains up. | No live tunnel kill, to avoid disrupting public service. | **UNTESTED** | No measured tunnel recovery time. Cloudflared also reconnects internally. |
| L. Database fails | SQLite is embedded, not a separate process. Readiness checks DB; DB failures are reported and do not cause destructive reset or blind backend restart. | Current live DB readiness passed; no lock/corruption/full-disk injection. | **UNTESTED** | No automatic DB repair. Preserve and back up DB before manual repair. |
| M. Android OS reboots | Google Play Termux build integrates boot-script support; hook waits 20 s, manager checks paths/password, starts local backend, then tunnel independently. Job is persisted too. | Hook script and executable paths checked; manual hook was idempotent. No physical reboot. | **CONDITIONAL** | Actual boot event, locked-boot storage availability, and end-to-end public verification after reboot remain unverified. First unlock may be required. |
| N. Tablet loses power, then receives power | If firmware boots the tablet, scenario M applies. | No power event tested; auto-power-on-on-charge not determined. | **UNSUPPORTED** | Current configuration cannot power on the tablet. You may need to press Power after charging. |
| O. Wi-Fi or Internet disappears | LAN route checked about every 3 s; proxy gets 12 s grace and rebinds to new Wi-Fi address. Internet/public diagnostics run every 5 min; cloudflared reconnects without backend restart. | No Wi-Fi toggle or Internet outage injected. | **UNTESTED** | Reconnection timing depends on Android network state, DHCP, and Cloudflare. Local server process is not stopped. |
| P. Local app healthy, public hostname fails | Probes distinguish Internet, edge metrics and public hostname. No backend restart for a public-only failure; if Internet works but tunnel has zero edges continuously for 5 min, restart tunnel only. | Current public endpoint was reachable from tablet with HTTP 401; no external client or failure injected. | **UNTESTED** | A working tunnel metric with broken hostname/ingress is diagnostic-only; DNS/config changes are never automatic. |

### One-time test note

The backend-recovery test script observed authenticated readiness, then raced the journal update while reading the new managed PID and exited with a verifier `AttributeError`. Follow-up checks showed the intended state: backend PID changed, restart counter became 1, `lastUnexpectedExit` was recorded, `lastSuccessfulRecovery` followed 4.08 seconds later, and the live API/database were ready. The server recovery succeeded; the test harness’s post-check was not clean. This does not change the result, but the race is recorded rather than hidden.

## Memory, storage, database, logs, and thermal edge cases

| Condition | Current behavior | Status / gap |
|---|---|---|
| Memory warning/critical | Samples `/proc/meminfo` and known child RSS every 60 s; warning below configured 256 MiB and critical below 160 MiB. No unrelated apps are killed. | Live RAM/process readings work. Pressure/OOM counters are inaccessible on this Android build and shown unavailable. No pressure injection. |
| Optional AI | Model activation retains its existing 1,200 MiB minimum-memory guard; indexing requests are deferred below the configured 256 MiB default. No auto model load at boot. | No model load was attempted under low RAM. A currently running model/inference is not automatically unloaded by this reliability layer. |
| Storage becoming full | Free space is sampled; no automatic user-file cleanup is performed. | No low-space threshold or automated storage recovery; full-storage test intentionally not performed. User must free space safely. |
| SQLite write failure | Existing SQLite WAL/busy-timeout behavior and API error handling apply. Supervisor reports DB degradation, never deletes/recreates the DB. | No disk-full/locking fault injected; DB integrity check passed before this rollout. |
| Interrupted upload | Upload writes a hidden temporary file and renames it into the final path only after the full body arrives. Handler cleanup removes the temp on ordinary exceptions. | A hard process/power loss may leave a hidden `.*.upload` temp file; final target is not committed by that interrupted request. No automatic recursive deletion is performed. |
| Log growth | Supervisor/tunnel logs are compacted at 2 MiB; scheduled watchdog log rotates at 256 KiB; state events are bounded. | Helpers/unit tests and current file modes checked; long-duration growth not tested. |
| Repeated backend crashes | Restart delay is bounded at 2/4/8/16/32/60 s with ±20% jitter; ceiling retry continues at a controlled 60 s rate. | Backoff unit tests passed; repeated real crashes not injected. |
| Thermal stress | No thermal sensor or workload governor added; Android firmware controls throttling/shutdown. | **UNSUPPORTED** as app-managed thermal recovery. Do not hold wake lock in hot conditions; reduce workload or let the tablet cool. |

## Android-side configuration findings

The installed Termux version is the Google Play build, for which the upstream project states that Termux:Boot functionality is merged into the main Termux app; the separate companion package is not expected. The `.termux/boot` hook exists and is executable. Upstream documents that boot scripts must be under this directory and that Termux:Boot should be opened once on builds where it is a separate app. See [Termux Google Play build status](https://github.com/termux-play-store) and [Termux:Boot instructions](https://github.com/termux/termux-boot).

Android reports that a force-stopped package cannot self-start until an explicit component start. See [Android Intent API: package force-stop state](https://developer.android.com/reference/kotlin/android/content/Intent.html#ACTION_PACKAGE_UNSTOPPED).

Samsung documents the **Never sleeping apps** list under Battery / Background usage limits. Exact labels can vary by One UI version; this shell could not read the setting. See [Samsung background usage limits](https://www.samsung.com/us/support/answer/ANS10003442/).

Required manual steps:

1. Settings → Apps → Termux → Battery → set **Unrestricted** / allow background activity if offered.
2. Settings → Battery and device care → Battery → Background usage limits → add Termux to **Never sleeping apps**; ensure it is not in Sleeping or Deep sleeping.
3. Keep the Termux service/session notification enabled. Do not force-stop it; closing a terminal session is different from Settings → Force stop.
4. Wake lock is requested by the manager. `termux-wake-lock` is available, but Android's held lock state could not be independently inspected. It increases battery/heat and does not defeat force-stop or power loss. Use `termux-wake-unlock` after intentional shutdown.
5. Do not install Tasker/MacroDroid accessibility automation or grant root/device-admin access for this setup. No such automation is installed or required.

## Practical manual verification plan

Do these only when you can tolerate the stated test window and have no active uploads:

1. **Close terminal session:** open a second device on Wi-Fi, run `bash ~/myforever_service.sh status`, then close only the Termux session. Check again after 1 minute. Do not use Force stop.
2. **Recent Apps:** first apply Samsung battery settings. Swipe Termux from Recents, wait 2–5 minutes, then check local and public URLs from another device. Record whether the existing server continues; do not infer this from a shell-only process test.
3. **JobScheduler check:** `bash ~/myforever_service.sh watchdog-status`; expected pending job 84532017. To disable: `bash ~/myforever_service.sh watchdog-disable`.
4. **Reboot chain:** schedule a maintenance window, ensure no upload/model load, reboot manually, wait at least 2 minutes after unlock, then check Termux boot log, local health, and public HTTPS from a separate network. Confirm one supervisor/backend/tunnel only. Do not test before-unlock behavior with production data.
5. **Force-stop:** not recommended as an availability test. If you do it manually, expect service to remain down until Termux is opened again; this is an Android restriction, not a missing shell retry.
6. **Power loss/thermal:** do not deliberately drain the battery or overheat the tablet. Observe whether this hardware powers on when charging; otherwise manual Power-button action is required.

## Recovery commands and rollback

```sh
bash ~/myforever_service.sh status
bash ~/myforever_service.sh health
bash ~/myforever_service.sh watchdog-status
bash ~/myforever_service.sh logs
bash ~/myforever_service.sh restart
bash ~/myforever_service.sh stop
bash ~/myforever_service.sh start
```

The active backup for this edge-audit rollout is `$HOME/.local/state/myforever/reliability-backups/edge-audit-20261020-170354/`. It contains the pre-deployment code and state copies. Roll back code only after stopping the manager and copying back `lan_host.py`, `reliability_state.py`, and `myforever_service.sh`; do not restore SQLite for this code-only change. To disable the Android fallback first, run `bash ~/myforever_service.sh watchdog-disable`.

## Availability ceiling

There is no independent always-on controller outside the Termux UID. Android JobScheduler is a useful slower recovery path after supervisor/process death, but it is subject to Doze, battery policy, OEM policy and package stopped state. A separate external uptime monitor can alert from another network, but cannot power on the tablet or override force-stop. Automatic failover/data replication are not installed. A second host would need an explicit replication and split-brain plan before promotion.
