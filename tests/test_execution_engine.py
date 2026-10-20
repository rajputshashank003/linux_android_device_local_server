import subprocess
import unittest
from unittest.mock import patch

from execution_engine import diagnostics, runtime
from execution_engine.process import ManagedProbe, run_output_probe


class ExecutionEngineDiagnosticsTests(unittest.TestCase):
    def test_controlled_process_probe_captures_expected_output(self):
        result = run_output_probe()
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.returncode, 0)
        self.assertIn("MYFOREVER_EXEC_ENGINE_PROBE_OK", result.stdout)
        self.assertLess(result.elapsed_seconds, 5)

    def test_output_probe_reports_timeout(self):
        with patch("execution_engine.process.subprocess.run", side_effect=subprocess.TimeoutExpired("python", 1, output=b"partial")):
            result = run_output_probe()
        self.assertEqual(result.status, "FAIL")
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stdout, "partial")

    def test_managed_probe_detects_exit_and_stops(self):
        probe = ManagedProbe()
        try:
            probe.start()
            self.assertEqual(probe.status(), "running")
            child = probe._process
            self.assertIsNotNone(child)
            child.terminate()
            child.wait(timeout=5)
            self.assertEqual(probe.status(), "exited")
            probe.start()
            self.assertEqual(probe.status(), "running")
            probe.stop()
            self.assertEqual(probe.status(), "exited")
        finally:
            probe.stop()

    def test_filesystem_probe_uses_and_cleans_its_temporary_directory(self):
        result = diagnostics._filesystem_test()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["bytes_written"], len("myforever-execution-probe"))

    def test_http_probe_binds_only_loopback_and_releases_ephemeral_port(self):
        result = diagnostics._network_test()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["interface"], "127.0.0.1")
        self.assertGreater(result["port"], 0)

    def test_runtime_absence_is_not_misreported_as_oci_support(self):
        with patch.object(runtime.shutil, "which", return_value=None):
            report = runtime.detect_runtime()
        self.assertFalse(report.available)
        self.assertEqual(report.oci_image_support, "NOT VERIFIED")

    def test_diagnostic_report_distinguishes_host_probes_from_oci(self):
        report = diagnostics.run_diagnostics()
        checks = {item["name"]: item for item in report["checks"]}
        self.assertEqual(checks["Controlled process execution"]["status"], "PASS")
        self.assertEqual(checks["Temporary filesystem"]["status"], "PASS")
        self.assertEqual(checks["Loopback HTTP"]["status"], "PASS")
        self.assertEqual(checks["Port conflict detection"]["status"], "PASS")
        self.assertEqual(checks["Process lifecycle"]["status"], "PASS")
        self.assertIn(checks["OCI image execution"]["status"], {"PASS", "NOT VERIFIED"})
        self.assertIn("Loopback only", report["networkScope"])


if __name__ == "__main__":
    unittest.main()
