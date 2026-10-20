# MyForever Execution Engine — Phase 1

This module is a diagnostic foundation for evaluating local application execution on Termux. It is deliberately independent of `server.py`, file operations, authentication, and the existing `myforever_service.sh` supervisor. It adds no HTTP endpoint and installs no packages.

## Architecture and runtime choice

The host is currently a non-root ARM64 Termux environment. The candidate runtime is **Termux PRoot-Distro**. Its official current project documentation describes OCI/Docker image pull, image layer assembly, and image entrypoint execution through PRoot, without a Docker daemon or root. The adapter only detects `proot-distro` and `proot`; it does not install them, pull images, start containers, or claim OCI support from detection alone.

This is a constrained Linux userspace runner, not a Docker-equivalent isolation boundary. PRoot is user-space path emulation and does not provide kernel PID/network/IPC namespaces, cgroups, or seccomp isolation. Image workloads share the Android/Termux kernel and must not be treated as safe for untrusted multi-user deployment. Phase 2 must not expose arbitrary image execution until security and resource boundaries are designed and reviewed.

## Diagnostics

From the repository root:

```sh
python -m execution_engine.diagnostics
```

Exit codes: `0` means basic probes and candidate runtime detection pass; `1` means a basic probe failed; `2` means local probes passed but the candidate runtime is unavailable. `OCI image execution` remains `NOT VERIFIED` unless the separate real image smoke test is run. After installing the test image explicitly, run `python -m execution_engine.oci_smoke`; this command never downloads or installs the image itself.

Probes use only a fixed Python child process, a temporary directory created and removed by the test, and a loopback HTTP listener bound to `127.0.0.1` on an ephemeral port. Process lifecycle checks include start, status, observed termination, restart, and cleanup. No public port, image, shell command from a user, application data, or Cloudflare configuration is touched.

The report reads `/proc/meminfo` when available and reports storage for the filesystem containing Termux `HOME` only; this is not necessarily the device-wide storage total. Namespace diagnostics execute only a fixed `unshare -Ur true` probe when the executable exists. Missing/blocked facilities are reported rather than inferred from file presence.

## Compatibility status

| Capability | Status on inspected tablet |
| --- | --- |
| Android/Termux | Detected; Android 16, Samsung SM-X115 (the brief's Android 10 assumption does not match this device) |
| CPU architecture | ARM64 / `aarch64` |
| Root | Not available; process runs as an unprivileged app UID |
| PRoot-Distro | Package is listed by the configured Termux repository, but not installed during this task |
| `proot` | Package is listed by the configured Termux repository, but not installed during this task |
| Basic host process, temporary filesystem, and loopback diagnostics | Executed by this module; command output is authoritative for this run |
| OCI manifest/layer support | Tested by pulling official `nginx:alpine` ARM64 image layers with PRoot-Distro 5.3.0 |
| OCI image entrypoint + local HTTP | Tested on the tablet with Nginx and a loopback-only response; see `python -m execution_engine.oci_smoke` |
| Docker image entrypoint execution | Verified for the tested Nginx image only; not a claim of general image compatibility |
| Docker daemon / kernel container isolation | Not available or provided by this approach |
| LAN/public port forwarding | Not tested; loopback probe is not evidence of either |
| Multi-tenant isolation | Not provided; unsuitable as a security boundary |

Termux repository package metadata showed `proot-distro` 5.3.0 and `proot` 5.1.107.80 for aarch64; both were installed for this validation. The official v5.3.0 source documents Docker/OCI image installation and execution. This package version does not have the newer `ps`/`kill` commands; the smoke test controls only its own attached PRoot process and uses SIGQUIT for teardown. See [the v5.3.0 project source](https://github.com/termux/proot-distro/tree/v5.3.0), [current runtime limitations](https://github.com/termux/proot-distro#limitations), and [Termux PRoot](https://github.com/termux/proot). Package availability is not equivalent to runtime verification. The currently listed `proot` build is one patch below the upstream PRoot-Distro recommendation for newer releases, so verify the paired package versions before proceeding. The runtime was installed for this test using the Termux package manager (four packages, no upgrades). Rerun the diagnostics and smoke test after changing package/runtime versions.

## Resource and compatibility limitations

The diagnostics read current RAM and Termux-home filesystem values when run; these readings vary with Android and other apps. PRoot adds CPU and memory overhead, and application images may expect kernel features or filesystem behavior Android does not provide. Image size, startup time, application memory, network behavior, and compatibility must be measured per workload. This module does not reserve RAM, supervise deployments, forward ports, or guarantee background uptime.

## Phase 2 boundary

This phase does not add a Host UI, public application URLs, image registry credentials, image pulls, deployments, multi-user access, or a deployment API. Before Phase 2, verify a small trusted ARM64 OCI image and its actual entrypoint on-device, measure resource behavior alongside MyForever, review storage/network constraints, and define an explicit threat model. Do not use PRoot as isolation for hostile code.
