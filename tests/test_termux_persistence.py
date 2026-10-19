from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
