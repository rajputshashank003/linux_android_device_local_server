from pathlib import Path
import os
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import lan_host
from server import server_password_from_environment


class TermuxPersistenceTests(unittest.TestCase):
    def test_password_can_be_loaded_from_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("private-password\n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(server_password_from_environment({
                "TERMUX_SERVER_PASSWORD_FILE": str(path),
            }), "private-password")

    def test_password_file_rejects_group_or_world_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("private-password", encoding="utf-8")
            path.chmod(0o640)
            with self.assertRaises(RuntimeError):
                server_password_from_environment({"TERMUX_SERVER_PASSWORD_FILE": str(path)})

    def test_password_file_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            path = Path(directory) / "password"
            target.write_text("private-password", encoding="utf-8")
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises(RuntimeError):
                server_password_from_environment({"TERMUX_SERVER_PASSWORD_FILE": str(path)})

    def test_crash_restart_backoff_is_exponential_and_bounded(self):
        self.assertEqual([lan_host.restart_delay(n) for n in range(6)], [2, 4, 8, 16, 32, 60])
        self.assertEqual(lan_host.restart_delay(100), 60)
        self.assertEqual(lan_host.restart_delay(-1), 2)

    def test_saved_managed_process_can_be_adopted_and_stopped_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = str(Path(directory) / "managed-child.py")
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", expected])
            try:
                identity = {"pid": child.pid, "startTicks": lan_host.process_start_ticks(child.pid)}
                self.assertIsNotNone(identity["startTicks"])
                reference = lan_host.adopt_managed_process(
                    identity, lan_host._script_matcher(expected), "test child"
                )
                self.assertIsNotNone(reference)
                self.assertIsNone(reference.poll())
                reference.terminate()
                reference.wait(timeout=3)
                self.assertIsNotNone(reference.poll())
                child.wait(timeout=3)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def test_process_adoption_rejects_wrong_command_or_reused_pid(self):
        current = os.getpid()
        self.assertIsNone(lan_host.adopt_managed_process(
            {"pid": current, "startTicks": lan_host.process_start_ticks(current)},
            lan_host._script_matcher("/not/the/current/process.py"), "wrong process"
        ))
        self.assertIsNone(lan_host.adopt_managed_process(
            {"pid": current, "startTicks": -1}, lambda argv: True, "reused PID"
        ))

    def test_android_recovery_scripts_are_idempotent_and_bounded(self):
        root = Path(__file__).resolve().parents[1]
        manager = (root / "myforever_service.sh").read_text(encoding="utf-8")
        watchdog = (root / "myforever_android_watchdog.sh").read_text(encoding="utf-8")
        boot = (root / "termux/boot/10-myforever.sh").read_text(encoding="utf-8")
        self.assertIn("flock -n 9", manager)
        self.assertIn("--period-ms 900000", manager)
        self.assertIn("--persisted true", manager)
        self.assertIn('"$PREFIX/bin/bash" "$MANAGER" start', watchdog)
        self.assertIn('"$PREFIX/bin/bash" "$MANAGER" start', boot)

    def test_health_monitor_restarts_only_after_consecutive_failures(self):
        state, failures = lan_host.health_recovery_decision("unhealthy", 0, threshold=3)
        self.assertEqual((state, failures), ("wait", 1))
        state, failures = lan_host.health_recovery_decision("unhealthy", failures, threshold=3)
        self.assertEqual((state, failures), ("wait", 2))
        state, failures = lan_host.health_recovery_decision("unhealthy", failures, threshold=3)
        self.assertEqual((state, failures), ("restart", 3))

    def test_database_outage_does_not_trigger_backend_restart(self):
        self.assertEqual(
            lan_host.health_recovery_decision("database_unavailable", 2, threshold=3),
            ("wait", 0),
        )

    def test_healthy_service_resets_failure_counter(self):
        self.assertEqual(lan_host.health_recovery_decision("ready", 2), ("keep", 0))

    def test_resource_snapshot_reports_or_explicitly_omits_measurements(self):
        snapshot = lan_host.resource_snapshot()
        self.assertIn("availableMemoryBytes", snapshot)
        self.assertIn("freeStorageBytes", snapshot)
        for value in snapshot.values():
            self.assertTrue(value is None or isinstance(value, int))

    def test_authenticated_readiness_probe_uses_private_password_file(self):
        class Response:
            status = 200
            def read(self, _limit):
                return b'{"state":"ready","database":"healthy"}'

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("test-only-secret", encoding="utf-8")
            path.chmod(0o600)
            connection = mock.Mock()
            connection.getresponse.return_value = Response()
            with mock.patch.object(lan_host.http.client, "HTTPConnection", return_value=connection):
                self.assertEqual(lan_host.server_readiness(path), ("ready", "local API and database are ready"))
            headers = connection.request.call_args.kwargs["headers"]
            self.assertTrue(headers["Authorization"].startswith("Basic "))
            self.assertNotIn("test-only-secret", repr(connection.request.call_args))

    def test_readiness_probe_refuses_insecure_password_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "password"
            path.write_text("test-only-secret", encoding="utf-8")
            path.chmod(0o640)
            with mock.patch.object(lan_host.http.client, "HTTPConnection") as connection:
                self.assertEqual(lan_host.server_readiness(path)[0], "unverified")
                connection.assert_not_called()


class AdditionalReliabilityTests(unittest.TestCase):
    def test_connectivity_layers_are_classified_independently(self):
        cases = [
            (True, True, True, True, True, "healthy"),
            (True, False, True, True, True, "wifi_unavailable"),
            (True, True, False, False, False, "internet_unavailable"),
            (True, True, True, False, False, "tunnel_disconnected"),
            (True, True, True, True, False, "public_hostname_unreachable"),
            (False, True, True, True, True, "backend_unhealthy"),
            ("database_unavailable", True, True, True, True, "database_unavailable"),
            (True, True, None, True, True, "internet_unverified"),
        ]
        for local, wifi, internet, tunnel, public, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(lan_host.classify_network_state(local, wifi, internet, tunnel, public), expected)

    def test_tunnel_process_identity_handles_real_argument_order(self):
        self.assertTrue(lan_host.is_cloudflared_tunnel_run("/usr/bin/cloudflared tunnel --no-autoupdate --loglevel info run --token-file private"))
        self.assertFalse(lan_host.is_cloudflared_tunnel_run("/usr/bin/cloudflared tunnel list"))
        self.assertFalse(lan_host.is_cloudflared_tunnel_run("echo cloudflared tunnel run"))

    def test_tunnel_and_backend_retry_delays_use_bounded_jitter(self):
        class FixedRandom:
            @staticmethod
            def uniform(low, high):
                return high
        self.assertEqual(lan_host.jittered_restart_delay(0, base=2, rng=FixedRandom()), 2.4)
        self.assertEqual(lan_host.jittered_restart_delay(20, base=20, maximum=300, rng=FixedRandom()), 300.0)
        self.assertEqual(lan_host.jittered_restart_delay(0, base=20, rng=FixedRandom()), 24.0)

    def test_reliability_state_is_atomic_private_and_bounded(self):
        from reliability_state import ReliabilityState, MAX_EVENTS
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "reliability.json"
            state = ReliabilityState(path)
            state.update(startedAt=100.0, serviceState={"overall": "HEALTHY"})
            for number in range(MAX_EVENTS + 8):
                state.record_event("INFO", "network", "sample", f"event {number}", timestamp=number)
            self.assertEqual(len(state.snapshot()["events"]), MAX_EVENTS)
            state.increment_restart("backend")
            self.assertEqual(state.snapshot()["restartCounts"]["backend"], 1)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored = ReliabilityState(path).snapshot()
            self.assertEqual(restored["startedAt"], 100.0)
            self.assertEqual(restored["events"][-1]["message"], f"event {MAX_EVENTS + 7}")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_reliability_state_write_failure_does_not_raise(self):
        from reliability_state import ReliabilityState
        with tempfile.TemporaryDirectory() as directory:
            state = ReliabilityState(Path(directory) / "state.json")
            with mock.patch.object(state, "_write", side_effect=OSError("disk full")):
                state.record_event("ERROR", "memory", "pressure", "write attempt")
            self.assertFalse(state.snapshot()["persistenceAvailable"])
            self.assertEqual(state.snapshot()["persistenceError"], "OSError")

    def test_memory_pressure_thresholds_are_explicit(self):
        warning = 256 * 1024 * 1024
        self.assertEqual(lan_host.memory_pressure_level({"availableMemoryBytes": None}, warning), "unavailable")
        self.assertEqual(lan_host.memory_pressure_level({"availableMemoryBytes": warning + 1}, warning), "normal")
        self.assertEqual(lan_host.memory_pressure_level({"availableMemoryBytes": warning - 1}, warning), "warning")
        self.assertEqual(lan_host.memory_pressure_level({"availableMemoryBytes": 100 * 1024 * 1024}, warning), "critical")
        self.assertEqual(lan_host.memory_pressure_level({"availableMemoryBytes": warning + 1, "pressureAvg10": 6}, warning), "warning")

    def test_log_compaction_keeps_bounded_recent_tail_without_symlink_following(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.log"
            path.write_bytes(b"a" * (2 * 1024 * 1024) + b"b" * (512 * 1024))
            path.chmod(0o600)
            self.assertTrue(lan_host.bound_log_file(path))
            self.assertLessEqual(path.stat().st_size, 1024 * 1024)
            compacted = path.read_bytes()
            self.assertEqual(len(compacted), 1024 * 1024)
            self.assertEqual(compacted[-(512 * 1024):], b"b" * (512 * 1024))
            target = Path(directory) / "target"
            target.write_text("untouched", encoding="utf-8")
            link = Path(directory) / "link.log"
            link.symlink_to(target)
            self.assertFalse(lan_host.bound_log_file(link))
            self.assertEqual(target.read_text(encoding="utf-8"), "untouched")

    def test_slow_retry_mode_is_recorded_once_at_threshold(self):
        from reliability_state import ReliabilityState
        with tempfile.TemporaryDirectory() as directory:
            state = ReliabilityState(Path(directory) / "state.json")
            lan_host.note_slow_retry_mode(state, "tunnel", 5)
            self.assertEqual(state.snapshot()["recoveryAttemptsExceeded"], [])
            lan_host.note_slow_retry_mode(state, "tunnel", 6)
            lan_host.note_slow_retry_mode(state, "tunnel", 7)
            saved = state.snapshot()
            self.assertEqual(len(saved["recoveryAttemptsExceeded"]), 1)
            self.assertEqual(saved["recoveryAttemptsExceeded"][0]["attempt"], 6)
            self.assertEqual(saved["events"][-1]["event"], "slow_retry_mode")

    def test_network_state_updates_preserve_other_service_health(self):
        from reliability_state import ReliabilityState
        with tempfile.TemporaryDirectory() as directory:
            state = ReliabilityState(Path(directory) / "state.json")
            state.update(serviceState={"overall": "DEGRADED", "backend": "healthy", "database": "degraded", "tunnel": "degraded"})
            state.update_service_component("tunnel", "healthy")
            current = state.snapshot()["serviceState"]
            self.assertEqual(current["database"], "degraded")
            self.assertEqual(current["tunnel"], "healthy")
            self.assertEqual(current["overall"], "DEGRADED")
            state.update(serviceState={"overall": "HEALTHY", "backend": "healthy", "database": "healthy", "tunnel": "unchanged"})
            self.assertEqual(state.snapshot()["serviceState"]["tunnel"], "healthy")

    def test_known_process_memory_reading_is_measured_or_unavailable(self):
        measured = lan_host.process_memory_bytes(os.getpid())
        self.assertTrue(measured["rssBytes"] is None or measured["rssBytes"] > 0)
        self.assertEqual(lan_host.process_memory_bytes(None), {"rssBytes": None, "peakRssBytes": None})

    def test_private_reliability_state_symlink_is_not_followed_or_overwritten(self):
        from reliability_state import ReliabilityState
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private-target"
            target.write_text("do not read", encoding="utf-8")
            target.chmod(0o600)
            link = root / "reliability.json"
            link.symlink_to(target)
            state = ReliabilityState(link)
            self.assertFalse(state.snapshot()["persistenceAvailable"])
            state.record_event("INFO", "supervisor", "attempt", "must remain in memory only")
            self.assertEqual(target.read_text(encoding="utf-8"), "do not read")

    def test_low_memory_defers_optional_indexing_without_blocking_core_service(self):
        from server import optional_job_block_reason
        self.assertIsNone(optional_job_block_reason(None, 256))
        self.assertIsNone(optional_job_block_reason(512, 256))
        self.assertIn("retry when memory recovers", optional_job_block_reason(128, 256))

    def test_public_probe_url_rejects_credentials_and_non_https(self):
        self.assertEqual(lan_host._safe_public_probe_url("https://files.example/"), "https://files.example/")
        self.assertIsNone(lan_host._safe_public_probe_url("http://files.example/"))
        self.assertIsNone(lan_host._safe_public_probe_url("https://user:secret@files.example/"))
        self.assertIsNone(lan_host._safe_public_probe_url("https://files.example/?token=secret"))

    def test_public_probe_discards_body_and_reports_http_status(self):
        class Response:
            status = 401
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit): return b"must-not-be-stored"
        with mock.patch("lan_host.urlopen", return_value=Response()):
            self.assertEqual(lan_host._http_probe("https://example.invalid/"), {"reachable": True, "status": 401})

    def test_reliability_health_is_returned_without_private_paths(self):
        import health
        from reliability_state import ReliabilityState
        with tempfile.TemporaryDirectory() as directory:
            state = ReliabilityState(Path(directory) / "state.json")
            with mock.patch("reliability_state.ReliabilityState", return_value=state):
                result = health._reliability_snapshot({"inferenceProcessRssBytes": None})
        import json
        persisted_view = json.dumps(result).lower()
        self.assertNotIn(str(Path.home()).lower(), persisted_view)
        self.assertNotIn("password", persisted_view)
        self.assertNotIn("authorization", persisted_view)



if __name__ == "__main__":
    unittest.main()
