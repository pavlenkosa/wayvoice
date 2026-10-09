"""Service/direct log collection uses bounded reads and no real service calls."""
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "wayvoice.ui.diagnostics", Path(__file__).resolve().parents[1] / "app/src/wayvoice/ui/diagnostics.py")
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class LogSourceTests(unittest.TestCase):
    def test_systemd_uses_bounded_journal_command_and_surfaces_failure(self):
        with mock.patch.object(diagnostics.service, "systemd_available", return_value=True), \
             mock.patch.object(diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "line\n")) as run:
            self.assertEqual(diagnostics.read_logs(12, 3), "line\n")
        self.assertIn("12", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        with mock.patch.object(diagnostics.service, "systemd_available", return_value=True), \
             mock.patch.object(diagnostics.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "unit unavailable")):
            with self.assertRaisesRegex(subprocess.SubprocessError, "unit unavailable"):
                diagnostics.read_logs()

    def test_direct_mode_returns_last_lines_without_journalctl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daemon.log"
            path.write_text("first\nsecond\nthird\n")
            with mock.patch.object(diagnostics.service, "systemd_available", return_value=False), \
                 mock.patch.object(diagnostics.service, "service_log_path", return_value=path), \
                 mock.patch.object(diagnostics.subprocess, "run") as run:
                self.assertEqual(diagnostics.read_logs(2), "second\nthird")
            run.assert_not_called()

    def test_large_direct_log_is_bounded_and_missing_file_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daemon.log"
            path.write_bytes(b"private-old-line\n" * 10000 + b"last\n")
            with mock.patch.object(diagnostics.service, "systemd_available", return_value=False), \
                 mock.patch.object(diagnostics.service, "service_log_path", return_value=path):
                result = diagnostics.read_logs()
                self.assertTrue(result.endswith("last"))
                self.assertLessEqual(len(result.encode()), diagnostics.MAX_LOG_BYTES)
                path.unlink()
                with self.assertRaisesRegex(OSError, "daemon.log"):
                    diagnostics.read_logs(language="en")
