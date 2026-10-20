# MyFile Hosting Platform Progress

Updated: 2026-10-20

Statuses mean **PASS** only after the behavior was actually run and observed.

## Phase 0 — Project inspection

- **PASS** — The tested checkout is `~/.github-push-review`; the live service runs from `~/` using `server.py` and `ui.html`.
- **PASS** — A Python standard-library HTTP server listens on port 8000. `myforever_service.sh` supervises `lan_host.py`, which owns the server child and existing Cloudflare connector.
- **PASS** — SQLite is `~/.local/share/myforever/platform.sqlite3`; Shared files are in `~/shared`. The application password, tunnel token, model files, and service configuration are outside the deployed source files and were left untouched.
- **PASS** — Live pre-deployment status: supervisor running; unauthenticated `/` returned 401; Cloudflare tunnel metrics reported four active edges.
- **PASS** — Device/runtime: Samsung SM-X115, Android 16, ARM64, non-root Termux; PRoot-Distro 5.3.0. The request's Android 10 assumption does not match this device.

## Phase 1 — Execution engine

- **PASS** — Controlled process, temporary filesystem, loopback HTTP, conflict detection, process cleanup, and basic resource probes pass using `python -m execution_engine.diagnostics`.
- **PASS** — A real ARM64 `nginx:alpine` OCI image ran through PRoot-Distro and served the expected response.
- **FAIL / platform capability unavailable** — `unshare -Ur true` fails with `Invalid argument`; PRoot shares the host kernel/network namespace and provides no kernel isolation or cgroup CPU/RAM enforcement.
- **NOT VERIFIED** — OCI compatibility beyond Nginx, Android 10, LAN isolation, enforced resource quotas, and tablet reboot recovery.

## Phase 2 — Image deployment

- **PASS** — The live Host controller prepared and ran two temporary real `nginx:alpine` deployments. Both reached `running/ready`, proxied the Nginx welcome page, and used the expected host port 2080.
- **PASS** — Private `/hosted/<id>/` route required authentication (401 without it; 200 with it). Publishing the test app made the route public as designed; authenticated hosting management still returned 401 without credentials.
- **PASS** — Stop returned the expected unavailable response (503); start and restart recovered to ready/200; deletion removed each test app record, route, and runtime alias. No applications remain deployed.
- **PASS** — Existing Phase 1 diagnostic aliases were preserved.
- **NOT VERIFIED** — Other OCI images, WebSockets, private registries, and enforced resource limits. Only trusted images are appropriate.

## Phase 3 — Host UI and routes

- **PASS** — The deployed live page includes the Host tab (`id="hostTab"`); authenticated runtime/apps APIs return 200.
- **PASS** — The public HTTPS route `https://files.costop.in/hosted/<id>/` returned HTTP 200 with the Nginx welcome page while a verification app was explicitly published. The response came through Cloudflare (`server: cloudflare`).
- **NOT TESTED** — An independent client on a network outside the tablet's LAN. The external fetch tool reported that the URL was inaccessible to that tool. The successful client request was made from the tablet using the configured public hostname.
- **PASS** — The existing `https://files.costop.in/` route still returned the expected MyFile 401 password challenge through Cloudflare. Tunnel configuration, credentials, DNS, and ports were not changed; tunnel metrics still reported four active edges.
- **PASS** — App publication explicitly makes that app path unauthenticated/public. Private app routes and all management APIs remain authenticated. PRoot redirects application ports; app ports may also be reachable directly on the LAN, so deployments must remain administrator-managed and trusted.
- **NOT VERIFIED** — Browser visual testing and off-LAN access. No browser automation/browser test was available.

## Live deployment, data, and rollback

- **PASS** — Required application files were staged and compiled, then installed into `~/` with atomic file replacements. The existing supervisor's server-only restart signal was used; its supervisor PID and Cloudflare child remained active. Service scripts were not replaced.
- **PASS** — Live SQLite v2-to-v3 migration was tested first on a consistent backup, then completed at server startup. Live schema integrity is `ok`; 19 conversations, 59 messages, 3 indexed documents, 98 chunks, and 1 indexing job remain. Hosting table is empty after cleanup.
- **PASS** — Pre-deployment rollback artifacts: `~/.local/share/myforever/backups/deploy-20261020-092831/` contains a SQLite online-backup snapshot and `live-application-files.tar.gz` (application source plus Shared files). Private config, model files, and tunnel token were not overwritten.
- **PASS** — Live upload, list, download, and preview succeeded with a uniquely named temporary text file; that file was deleted after the test. Agent status, local AI status, and health readiness endpoints returned 200.
- **PASS** — Full checkout suite: 57 tests passed. Python compilation, Node inline-script syntax check, and execution-engine diagnostics with real Nginx OCI smoke test passed.

## Recovery notes

Use the existing manager: `bash ~/myforever_service.sh status`, `logs`, or `restart`. The pre-deployment SQLite snapshot is the rollback point. Before restoring it, take a fresh SQLite online backup of the current database; restoring the snapshot also rolls back database writes made after 2026-10-20 09:28 local time. To restore code, stop/restart through the manager as needed and extract only the backed-up source paths (do not restore `shared/` unless intentionally reverting user files). No rollback has been performed.

## Final external verification — 2026-10-20

These results supersede earlier “in progress” and generic-route statuses above.

- **PASS** — The live Host DOM contains the tab, panel, create form, refresh control, runtime status, application list, error alert, and action bindings. It displays application status, an empty state, and actionable API errors. Publish and delete require UI confirmation.
- **PASS** — The UI stylesheet has a 700 px single-column Host form breakpoint.
- **NOT TESTED** — Browser-rendered visual correctness and real phone/tablet viewport behavior. No browser executable or browser automation package is installed.
- **PASS** — A fresh Nginx test app was created from the authenticated Host API, became ready, and served Nginx on its private route. Unauthenticated private access returned 401.
- **PASS** — Authenticated publication made the app route public. Exact URL: https://files.costop.in/hosted/9f488dfe78054c4e86dcc0d641f2d7ef/. It returned HTTP 200 with the Nginx welcome page through Cloudflare (server: cloudflare).
- **PASS** — Unpublishing restored 401 for unauthenticated app-route access. Hosting management remained 401 without credentials throughout. Stop returned 503; start and restart restored HTTP 200 after readiness.
- **BLOCKED / NOT TESTED** — No independent client on a network outside the tablet’s LAN was available. The external fetch tool could not access arbitrary hosted-app URLs. The successful HTTPS request originated on the tablet and is not counted as off-LAN verification.
- **PASS** — The temporary app was deleted. Its route now returns 404, the hosting table contains zero records, and the existing diagnostic aliases remain untouched.
- **PASS** — Full test suite rerun: 57 tests passed. Python compilation and execution-engine diagnostics, including the real Nginx OCI smoke test, passed. Database integrity is ok; the existing 19 conversations and 59 messages remain.

For an independent check, deploy and explicitly publish a new test app, then from a phone with Wi-Fi disabled run: curl -i --max-time 20 'https://files.costop.in/hosted/9f488dfe78054c4e86dcc0d641f2d7ef/'. This exact historical URL belongs to the deleted test deployment and now returns 404; use the new URL shown in Host after publishing. The service supervisor and Cloudflare Tunnel remain active and unchanged.

## Dedicated Host page routing correction — 2026-10-20

- **PASS** — Root cause fixed: `/host` previously returned the home SPA and rendered inside its tab shell. Server now serves `hosting.html` for `/host`, `/host/new`, `/host/existing`, and `/deployed/<id>`; the home app remains served at `/` and `/agentic`.
- **PASS** — Dedicated Host document has its own Overview / Host New / See Existing navigation and reads real authenticated hosting API data.
- **PASS** — Live authenticated route checks returned separate Host documents for all Host paths and the original home shell at `/`. Live HTTPS requests through `https://files.costop.in/` behaved the same when sent with a browser user agent.
- **PASS** — Existing supervisor and tunnel remain active; no database change. SQLite online backup passed integrity check (schema 3): `~/.local/share/myforever/backups/host-page-final-20261020-100223/`.
- **PASS** — 59 automated tests, Python compilation, inline JavaScript syntax checks, and `git diff --check`.
- **NOT TESTED** — Browser-rendered visual inspection and independent off-LAN access.

## Shared Host header navigation — 2026-10-20

- **PASS** — One shared navigation component is injected into homepage and standalone Host pages. Host retains its separate route and dashboard.
- **PASS** — Host top-right menu includes Home, theme selection, and existing application destinations. Removed the redundant Back to MyFile button.
- **PASS** — 59 unit/integration tests, Python compilation, inline JS parsing, and whitespace checks.
- **PASS** — Live local and public-hostname (tablet-originated) route checks; unauthenticated Host page remained HTTP 401; supervisor and tunnel remained healthy.
- **NOT TESTED** — Visual browser interaction and off-LAN reachability. The live backup is `~/.local/share/myforever/backups/host-shared-navigation-20261020-100858/`.

## Dedicated-route-only shared menu — 2026-10-20

- **PASS** — Shared menu now contains only Select Theme and navigation to dedicated Home, Agentic Chat (`/agentic`), and Hosting (`/host`). Removed links to homepage tabs; homepage tabs themselves were left unchanged.
- **PASS** — Route-aware menu hides Home on `/`, Agentic Chat on `/agentic`, and Hosting on `/host` and its subroutes.
- **PASS** — 59 tests, Python compilation, inline JavaScript syntax checks, and `git diff --check`.
- **PASS** — Live local and public-hostname checks on `/`, `/agentic`, `/host`, `/host/new`, and `/host/existing`; Hosting auth still returns 401 anonymously; supervisor and tunnel remain active. Templates were copied atomically; server restart was not needed.
- **NOT TESTED** — Actual visual/click interaction in a browser; no browser automation is available.


## ARM64 Node.js source deployment — 2026-10-20

- **Root cause — PASS:** The preserved OCI image `docker.io/rajputshashank/sum-server` is AMD64, while the tablet is ARM64 (`aarch64`). PRoot-Distro reported `Image architecture amd64 does not match device architecture aarch64`. The application source itself is architecture-independent Node.js/Express; its `package.json` start script is `node server.js`, dependency is Express, and it listens on port 8080. There are no `/health` or `/status` routes in the source. Repository: https://github.com/rajputshashank003/sum-server
- **Strategy — PASS:** Added a distinct authenticated GitHub Node.js source workflow. It shallow-clones the requested public branch, requires `package.json`, `package-lock.json`, and `scripts.start`, runs `npm ci --omit=dev --ignore-scripts`, and launches `npm run start` directly with Termux ARM64 Node. OCI and source deployments remain separately identified in the API/UI. Source workloads run as the Termux user without PRoot isolation; use trusted repositories only.
- **Live deployment — PASS:** Host New source mode is deployed at `~/hosting.html`; authenticated `/api/hosting/source` is live. Existing failed AMD64 image record `cf99d7db656d426c81f4236cc0efbb94` was preserved. New source deployment ID: `d567f5d7b4c042fabf021620c281df73`; assigned host/application port: 8080; status running and published. Public URL: https://files.costop.in/hosted/d567f5d7b4c042fabf021620c281df73/
- **Launch defect found and fixed — PASS:** First live start installed dependencies but failed because the child environment omitted Termux `LD_PRELOAD`/loader/execution-shim variables; `/system/bin/env` could not execute `node` (exit 126). `_node_environment` now preserves the Termux exec context while keeping application HOME/TMPDIR under its source directory. A real `npm run start` reproduction then returned the application’s HTTP 200 root response. The same deployment record was started successfully after the fix.
- **Endpoint checks — PASS:** Direct port 8080 and local published route `/hosted/d567f5d7b4c042fabf021620c281df73/` returned HTTP 200. `/sum?a=8&b=13` returned `{"success":true,"res":21}` directly, locally through the MyFile proxy, and through the public HTTPS hostname from the tablet. Configured health check uses `/` and succeeds. `/health` and `/status` return HTTP 404 because those routes do not exist in the upstream application; no routes were fabricated. Authenticated start/restart returned 202 and restored running/200. Anonymous hosting management returned 401.
- **Public access — PASS from tablet / NOT TESTED off-LAN:** The public HTTPS URL returned the expected application response from the tablet. The external browser fetch tool could not access the hosted URL; no independent off-LAN client was available. Do not treat the tablet request as proof of off-LAN reachability.
- **Service and data — PASS:** Persistent supervisor PID 11137 remained active, port 8000 returned its expected unauthenticated 401, and Cloudflare Tunnel metrics remained at four edges. Live SQLite schema 3 integrity is `ok`; the prior failed OCI record and new source record are present. Fresh backup: `~/.local/share/myforever/backups/sum-server-arm64-deploy-20261020-111100/` (server/controller/UI snapshots and SQLite online backup, integrity `ok`).
- **Tests — PASS:** `python -m unittest discover -s tests -q` passed 63 tests before the Termux-environment regression test was added; targeted hosting suite passed 16 tests after it. Final full suite passed 64 tests after adding the Termux loader-environment regression test. Python compile, Node syntax, and `git diff --check` passed after correction.
- **NOT TESTED:** Visual browser interaction, off-LAN reachability, Android reboot recovery, and source execution isolation/resource limits.


### Final ARM64 source verification update

- **PASS** — Final full `python -m unittest discover -s tests -q`: 64 tests. `py_compile`, Node syntax checks for inline scripts in `hosting.html`, `main_navigation.html`, and `ui.html`, and `git diff --check` passed.
- **PASS** — After a MyFile supervisor-managed server restart, the Node deployment remained `running`; local proxy and Cloudflare HTTPS root and `/sum?a=8&b=13` returned HTTP 200. The live deployed `server.py`, `hosting.py`, and `hosting.html` match the tested checkout.
- **PASS** — The live Host New page displays the GitHub Node source option, the live applications API reports the running source deployment, anonymous management API is still HTTP 401, and database integrity remains `ok`. The old failed AMD64 OCI row remains present and unmodified by this source workflow.
- **BLOCKED / NOT TESTED** — No independent device outside the tablet's network was available; the public-hostname request from the tablet is not off-LAN proof. Browser-rendered visual testing is also unavailable.

## Concurrent deployment support — 2026-10-20

- **PASS — Root cause removed:** `hosting.py` had `MAX_RUNNING_APPS = 1`; both OCI and Node source deployment creation rejected new active apps, and the runtime status API/UI advertised the one-app cap. The controller now permits concurrent deployments and no longer emits `runningLimit`.
- **PASS — Port isolation:** Host ports are allocated from 20000–29999, checking active OS listeners, all persisted deployment port reservations (including stopped/failed records), and MyFile-reserved ports 8000/20241. Allocation and record creation are serialized. Each deployment retains its own ID, work directory/source tree or PRoot root, private environment, logs, process identity, and host port. Node source apps bind loopback and the adapter maps common Node `listen()` calls to that port. Standard Nginx listeners are adapted per OCI deployment. Resource checks produce warnings rather than a fabricated count limit.
- **PASS — Automated verification:** `python -m unittest discover -s tests -q` passed 67 tests. A new real-process integration test starts three independent Node applications on unique ports, verifies all three `/hosted/<id>/` proxy paths, stops one while the others continue, restarts a different one while siblings continue, and exercises recovery without duplicate starts. Python compile and `git diff --check` passed.
- **PASS — Live deployment:** Checked-out `hosting.py`, `server.py`, and `hosting.html` were backed up and atomically installed into the existing live Termux home. SQLite online backup integrity was `ok`, schema/data left in place; backup directory: `~/.local/share/myforever/backups/multiapp-hosting-20261020-113725/`. Existing MyFile supervisor PID 11137 and Cloudflare Tunnel remained operational. MyFile HTTP 8000 returned its expected unauthenticated 401 after restart.
- **PASS — Three live processes/routes:** Existing deployment `d567f5d7b4c042fabf021620c281df73` was found stopped before this change and was started without modifying/deleting it. It is running on its preserved port 8080. Two independent deployments were added: `a7d42a4971a14cb8a72c324654018bc7` on 20000 and `f458e3e0299042ba90526dc15c69ba16` on 20001. All three run the ARM64 Node source application, have distinct PIDs/working directories/logs, and their individual public HTTPS routes returned HTTP 200; `/sum?a=8&b=13` returned `{"success":true,"res":21}` on each.
- **PASS — Isolation/restart:** The first new deployment was restarted through the authenticated API; its assigned port was preserved and it recovered to running. The existing app and sibling stayed running and all three published URLs stayed healthy. A subsequent MyFile supervisor-managed server restart preserved the three application PIDs and all three local `/hosted/<id>/sum` routes returned HTTP 200. Unauthenticated management remained HTTP 401.
- **PASS — Resource warning:** After the three deployments were live, the health API measured about 905 MB available system RAM (3.82 GB total) and reported 3 active deployments with an advisory warning because available RAM was below 1 GiB. No app was rejected for the count or this warning. The reading is time-dependent; the tablet's 4 GB RAM, CPU, storage, and Android process policy remain practical limits.
- **NOT TESTED — External client:** Public HTTPS was requested from the tablet over the configured hostname and Cloudflare. No separate off-LAN client was available, so this does not independently verify outside-network reachability.
- **LIMITATION:** Live instances are separate deployment processes but use the same trusted `sum-server` source. Three different Node applications with distinct behavior are covered by the automated integration test. PRoot is not kernel-isolated; source deployments run as the Termux user. OCI port binding may expose application ports on the LAN. Only trusted administrator-managed applications are appropriate.


## Generic Docker/OCI runtime compatibility review — 2026-10-20

- **PASS — Environment diagnosis:** Actual host is non-root Termux on ARM64 (`aarch64`), Android 16 (not the requested Android 10 assumption). PRoot-Distro 5.3.0 is installed. Current capability probe finds no Docker CLI, `dockerd`, `containerd`, or `runc`; user/mount namespace probes fail, cgroup v2 controllers are inaccessible, rootless helpers and QEMU user-mode executables are absent. Remote Docker is not configured.
- **PASS — Local runtime smoke:** `python -m execution_engine.diagnostics` completed; controlled process, filesystem, process lifecycle, loopback HTTP, port-conflict, and real ARM64 Nginx OCI smoke checks passed. User namespace creation failed (`Invalid argument`), which matches Docker/rootless infeasibility here.
- **PASS — Runtime abstraction:** Added `hosting_runtimes.py`, with a safe PRoot-Distro argv adapter and secret-free capability inventory. Docker Engine, Rootless Docker, PRoot OCI, Termux Node source, and Remote Docker are reported as distinct backends. Runtime inventory caching no longer blocks through a nested lock.
- **PASS — Image inspection:** OCI deployment details show actual image architecture, declared TCP ports, entrypoint/cmd presence, working directory, environment key names only, Docker healthcheck and volume declarations. A blank port uses exactly one TCP `EXPOSE`; zero/multiple ports require an explicit HTTP port. Wrong-architecture errors identify missing QEMU or suggest native ARM64/multi-platform image. Healthcheck, volume and port-mapping limitations are stated.
- **LIMITATION:** PRoot OCI is not generic Docker compatibility: no kernel isolation, cgroups or CPU/RAM enforcement, Docker network/port mapping, arbitrary host volume mapping, Docker HEALTHCHECK, or QEMU integration. It shares the host network namespace; only trusted HTTP workloads that can listen on the assigned port are appropriate. Redis/PostgreSQL raw protocols are not served by the HTTP proxy. AMD64-only images remain unsupported locally. No remote Docker engine is configured.
- **NOT VERIFIED:** Android 10 operation, AMD64 emulation, generic image classes beyond the previously tested Nginx ARM64 image, off-LAN published-route access and PRoot workload isolation. Three concurrent Node-source apps remain the live concurrency verification; existing deployments were left untouched by this compatibility review.


### Generic OCI runtime and live image verification — 2026-10-20

- **PASS — Runtime inventory:** Live `GET /api/hosting/status` identifies Docker Engine and Rootless Docker as unavailable, PRoot-Distro OCI and ARM64 Termux Node.js as available, and remote Docker as not configured. The detailed inventory reports `docker`, `dockerd`, `containerd`, `runc`, rootless helpers, cgroup v2, user/mount namespace probes, and QEMU availability truthfully. No remote host is contacted.
- **PASS — Generic image diagnostics:** Host creation supports a Docker/OCI reference as a separate deployment type from GitHub-source Node deployments. It requests the native `aarch64` architecture, reads the PRoot image manifest/configuration, auto-selects one declared TCP EXPOSE port only, otherwise requires an explicit HTTP port, and checks actual HTTP readiness. Details expose backend, architecture, safe image config metadata, and unsupported requirements without returning environment secret values.
- **PASS — Two concurrent OCI images:** Deployed two temporary trusted `nginx:alpine` images simultaneously while the three existing Sum Server deployments remained running. They received unique ports 20002 and 20003, both reported PRoot-Distro OCI / ARM64 and passed the HTTP health check; their separate private proxy routes each returned the Nginx page. Stopping one left the other route at HTTP 200. Both temporary images were stopped and deleted; their records now return 404. No pre-existing deployment was changed.
- **PASS — OCI lifecycle and publication:** A temporary Nginx deployment passed health, direct and MyFile proxy checks, stop, restart, and cleanup. A second temporary Nginx instance's published route returned HTTP/2 200 and the expected Nginx page through `https://files.costop.in/hosted/1eb1fd8401684184b71696500297d681/` while live; it was then stopped and deleted (therefore that temporary URL is no longer active). Unauthenticated management returned HTTP 401.
- **PASS — Automated verification:** Full suite `python -m unittest discover -s tests -q`: 71 passed. `python -m py_compile *.py execution_engine/*.py`, inline Node syntax checks for `hosting.html`, `ui.html`, and `main_navigation.html`, and `git diff --check` passed. `python -m execution_engine.diagnostics` passed its real ARM64 Nginx OCI smoke test and basic process/filesystem/loopback checks.
- **PASS — Safe deployment:** Created an SQLite online backup with integrity `ok` at `/data/data/com.termux/files/home/.local/share/myforever/backups/generic-oci-runtime-20261020-115223`. Atomically installed `hosting.py`, `hosting.html`, and new `hosting_runtimes.py`; restarted only the existing supervisor-managed server child. Supervisor PID 11137 and four Cloudflare tunnel edges remained active; port 8000 returned the expected anonymous HTTP 401.
- **PASS — Existing workloads preserved:** The three existing Node deployments remained `running` on ports 8080, 20000, and 20001; each local published route and `/sum?a=8&b=13` returned HTTP 200 with `{"success":true,"res":21}`. The prior failed AMD64-only `sum-server` record remains untouched. SQLite integrity remains `ok`, with 4 persisted deployment records (3 running, 1 preserved failed).
- **LIMITATION:** This is compatible-image execution through PRoot-Distro, not general Docker Engine. PRoot shares kernel and network namespace, does not enforce CPU/RAM limits, lacks general Docker port mappings and persistent volumes, and the proxy supports HTTP only. AMD64 emulation, Docker HEALTHCHECK, arbitrary kernel-dependent services, Redis/PostgreSQL public protocol forwarding, and untrusted images are unsupported/unverified. Remote Docker requires a separately configured authenticated remote engine, not present here.
- **NOT TESTED:** Independent off-LAN verification, Android 10 behavior (the detected tablet runs Android 16), and arbitrary public images outside tested Nginx ARM64. A Python default User-Agent request to Cloudflare was blocked with error 1010; using curl's User-Agent succeeded. No Cloudflare settings changed.



## Latest OCI deployment failure diagnosis — 2026-10-20

- **Deployment:** `docker.io/rajputshashank/sum-server`, ID `1e844166120a448481074a63bdb30640`, created 2026-10-20 12:11:49 +05:30. Selected backend: PRoot-Distro OCI. It failed after image pull/extraction, during `_manifest()` architecture validation; entrypoint execution, process startup, port bind, health check, and reverse proxy startup were never reached.
- **Original error (preserved):** `Image architecture amd64 does not match device architecture aarch64. Use a multi-architecture ARM64 image or deploy supported Node.js source from Host New.` The retained PRoot image config identifies Linux/amd64 even though its separate PRoot target-architecture metadata says `aarch64`.
- **Image details:** The current Docker Hub `latest` registry response is a single Docker v2 manifest, not a multi-architecture index; its config digest matches the retained image config. `Entrypoint` is `docker-entrypoint.sh`, `Cmd` is `npm run start`, `WorkingDir` `/app`, `ExposedPorts` `8080/tcp`, environment keys include Node runtime variables, and no Docker HEALTHCHECK or VOLUME is declared. The image's `/usr/local/bin/node` is an x86-64 ELF executable. The app source itself is portable Express/JavaScript, listens on 8080, and GET `/` returns HTTP 200 when run natively; however, the image bundles an AMD64 Node runtime. The device has no QEMU user-mode emulator.
- **Root cause — NOT A CONTROLLER LAUNCH BUG:** PRoot-Distro accepted/unpacked this single-architecture image despite the requested ARM64 target metadata. The hosting controller correctly rejected the architecture mismatch before executing an incompatible binary. There is no ARM64 image manifest and no local emulation capability. This is an unsupported image/device combination.
- **Targeted code change:** `_manifest()` now returns the explicit limitation (“no QEMU user-mode emulator configured”) and directs the user to a native ARM64/multi-architecture image or the distinct GitHub Node.js source deployment. No automatic source fallback occurs. Added a regression test proving an amd64 manifest is rejected on ARM64 before any process spawn.
- **Reproduction:** The same image was submitted through authenticated `POST /api/hosting/apps` as a temporary diagnostic deployment; it failed with the same architecture mismatch before entrypoint execution. Only that temporary record was deleted. Its failure log was captured; the original record and logs were not deleted or replaced. The original deployment was then started once through its API after the fix; it returned to `failed` with the clearer QEMU-specific message. The original log entries remain, with the new start-failure event appended.
- **Health and public URL:** The app did not run, so its configured root health check cannot succeed. The published route `https://files.costop.in/hosted/1e844166120a448481074a63bdb30640/` correctly returns HTTP 503 (“Hosted application is stopped or unavailable”); no success is claimed.
- **Regression and deployment:** Full suite `python -m unittest discover -s tests -q` passed 72 tests. The updated `hosting.py` was deployed atomically after backup and the existing supervisor-managed server child was restarted. Supervisor PID 11137, port 8000 (expected anonymous 401), and four Cloudflare tunnel edges remained healthy. The three existing Sum Server source deployments stayed running on 8080, 20000, and 20001 and their individual HTTPS `/sum?a=8&b=13` routes returned HTTP 200 from the tablet. Management without credentials remains HTTP 401.
- **Backups:** SQLite online backup integrity is `ok`; current snapshot and the original failed deployment log are preserved at `/data/data/com.termux/files/home/.local/share/myforever/backups/oci-failure-diagnostic-20261020-121545/`.
- **Final deployment status:** The requested Docker image is **not running**. Required next option is to publish an ARM64 or multi-architecture image, or explicitly create a separate Node.js source deployment. No deployment-method switch was performed. Public checks originated on the tablet; independent off-LAN reachability remains unverified.

## Docker image deployment reliability update — 2026-10-20

- **PASS — Device/runtime audit:** Android Termux, ARM64 (`aarch64`), Python 3.13, PRoot-Distro 5.3.0, and 3.56 GiB physical RAM were observed; 0.93 GiB RAM was available at this check, and PRoot storage had 29.7 GiB free. Docker CLI/daemon, containerd, runc, rootless helpers, user/mount namespace creation, delegated cgroups, and QEMU user emulators are unavailable. Docker Engine/rootless Docker are therefore unavailable here. Remote Docker is not configured.
- **PASS — Registry preflight:** Added bounded, HTTPS-only Docker Hub manifest inspection before image pull. It resolves single-platform config architecture and multi-platform indexes, uses the fixed Docker Hub bearer-token realm, bounds registry metadata to 2 MiB, and fails closed on confirmed missing/private/incompatible images. Other registries are not fetched by this server-side preflight; PRoot-Distro remains responsible for pulling them. Transient Hub lookup failures are logged and deferred to PRoot's own resolver.
- **PASS — Simple Host form:** Application name is optional and defaults from the Docker image or source repository name; the image port is auto-detected when the manifest declares one TCP port. Public publishing remains an explicit opt-in because it bypasses MyFile authentication.
- **PASS — Current image facts:** Live Docker Hub metadata shows `nginx:alpine` includes `linux/arm64/v8`. `rajputshashank/sum-server:latest` resolves to `linux/amd64` only. The latter was submitted through the live API after deployment; the job failed with an explicit ARM64 incompatibility before PRoot started, and no image rootfs was created. Its temporary record was deleted.
- **PASS — Real local OCI:** A temporary published `nginx:alpine` deployment installed and ran using PRoot-Distro OCI on ARM64, passed HTTP readiness, returned the Nginx page on localhost and through `https://files.costop.in/hosted/f45667c762664c4fab3558ae71e01160/`, and passed stop/start/restart with the same route returning HTTP 200 after restart. Its logs contained the actual manifest-preflight event. The temporary deployment was stopped and deleted.
- **PASS — Existing workloads preserved:** All three Sum Server Node.js-source deployments remained running simultaneously on ports 8080, 20000, and 20001. Each public `/sum?a=8&b=13` route returned HTTP 200 with `{"success":true,"res":21}`. Total database hosting records are five (three running, two preserved failed records); temporary test IDs are absent.
- **PASS — Regression/security:** `python -m unittest discover -s tests -q` passed 75 tests. `py_compile` passed for `hosting.py`, `hosting_registry.py`, `hosting_runtimes.py`, and `server.py`; inline JavaScript syntax and `git diff --check` passed. Unauthenticated hosting management returned HTTP 401.
- **PASS — Live update/backup:** Added `hosting_registry.py` and installed it with `hosting.py` in the existing live home. Backup: `~/.local/share/myforever/backups/docker-manifest-preflight-20261020-122141/`; SQLite online backup integrity was `ok` and included five records. The supervisor PID 11137, port 8000 (expected anonymous 401), and four Cloudflare tunnel edges remained active. An immediate check during the supervised worker restart was transiently unavailable; supervisor logs recorded a successful recovery, and subsequent checks passed.
- **LIMITATION — General Docker semantics:** The working local backend is PRoot-Distro OCI, not Docker Engine. It shares the Android kernel/network context, has no general Docker port mapping, volume persistence, Docker HEALTHCHECK execution, kernel-level isolation, or enforced CPU/RAM caps. Only compatible native-architecture HTTP applications that listen on the assigned host port (or are adapted) can be exposed through the current proxy. Redis/PostgreSQL raw TCP hosting, privileged/kernel-dependent images, and AMD64 execution are not supported locally. Run only trusted images.
- **BLOCKED — Remote fallback:** No remote Docker endpoint, CA/client certificates, SSH transport configuration, or credentials are available. No remote deployment backend was fabricated or contacted. A secure remote Docker backend requires an administrator-supplied TLS-authenticated or SSH-controlled Linux Docker Engine endpoint and separate verification.
- **NOT TESTED:** Independent off-LAN access, Redis/PostgreSQL, arbitrary registries/images, AMD64 emulation, and true Docker Engine execution. The public URL test was made through Cloudflare from the tablet using a browser user-agent; an unauthenticated Python urllib user-agent is blocked by Cloudflare Error 1010.
