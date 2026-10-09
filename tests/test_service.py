"""Tests for :mod:`wayvoice.service`, the systemd-or-not start layer.

Nothing here really starts a process or waits on a socket: both the daemon socket and
``subprocess`` are mocked, so the module runs offline and instantly.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import service


class SystemdAvailableTests(unittest.TestCase):
    """Both conditions are probed, so both have to be mocked."""

    def probe(self, which, isdir, socket_exists):
        with mock.patch("shutil.which", return_value=which):
            with mock.patch("os.path.isdir", return_value=isdir):
                with mock.patch.object(service, "_user_manager_socket") as sock:
                    sock.return_value.exists.return_value = socket_exists
                    return service.systemd_available()

    def test_no_systemctl_means_no_systemd(self):
        self.assertFalse(self.probe(None, True, True))

    def test_no_user_manager_means_no_systemd(self):
        # systemctl is installed in a container, but no user manager ever ran: neither
        # /run/systemd/user nor the control socket exists.
        self.assertFalse(self.probe("/usr/bin/systemctl", False, False))

    def test_runtime_dir_means_systemd(self):
        self.assertTrue(self.probe("/usr/bin/systemctl", True, False))

    def test_control_socket_alone_means_systemd(self):
        # Current systemd releases no longer create /run/systemd/user.
        self.assertTrue(self.probe("/usr/bin/systemctl", False, True))


class StartDaemonSystemdTests(unittest.TestCase):
    def test_starts_the_unit_with_exact_argv(self):
        with mock.patch.object(service, "systemd_available", return_value=True):
            with mock.patch.object(service, "daemon_socket_alive", return_value=True):
                with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as popen:
                    self.assertTrue(service.start_daemon())
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["systemctl", "--user", "start", "wayvoice.service"])
        self.assertNotIn("shell", kwargs)

    def test_failed_command_never_accepts_old_daemon_or_spawns_direct(self):
        for action in (service.start_daemon, service.restart_daemon):
            with self.subTest(action=action.__name__), \
                 mock.patch.object(service, "systemd_available", return_value=True), \
                 mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stderr="unit failed")), \
                 mock.patch.object(service, "daemon_socket_alive") as ping, \
                 mock.patch.object(service, "_spawn") as spawn:
                self.assertFalse(action(wait=0.1))
                ping.assert_not_called()
                spawn.assert_not_called()

    def test_successful_command_without_ready_socket_is_failure(self):
        with mock.patch.object(service, "systemd_available", return_value=True), \
             mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch.object(service, "daemon_socket_alive", return_value=False):
            self.assertFalse(service.start_daemon(wait=0.01))

    def test_command_errors_are_reported_without_ping(self):
        for error in (OSError("missing"), subprocess.TimeoutExpired("systemctl", 0.01)):
            with self.subTest(error=error), \
                 mock.patch.object(service, "systemd_available", return_value=True), \
                 mock.patch.object(subprocess, "run", side_effect=error), \
                 mock.patch.object(service, "daemon_socket_alive") as ping:
                self.assertFalse(service.restart_daemon(wait=0.01))
                ping.assert_not_called()

    def test_command_completion_precedes_ping_and_budget_is_shared(self):
        events = []
        def command(*args, **kwargs):
            events.append("command")
            return subprocess.CompletedProcess([], 0)
        with mock.patch.object(service, "systemd_available", return_value=True), \
             mock.patch.object(subprocess, "run", side_effect=command), \
             mock.patch.object(service, "_wait_for_daemon", side_effect=lambda wait: events.append(wait) or True), \
             mock.patch.object(service.time, "monotonic", side_effect=[0, 0, 2]):
            self.assertTrue(service.restart_daemon(wait=5))
        self.assertEqual(events, ["command", 3])


class StartDaemonDirectTests(unittest.TestCase):
    def test_running_daemon_is_left_alone(self):
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "daemon_socket_alive", return_value=True):
                with mock.patch.object(subprocess, "Popen") as popen:
                    self.assertTrue(service.start_daemon(wait=0.1))
        popen.assert_not_called()

    def test_spawns_the_daemon_module_and_waits_for_the_socket(self):
        alive = mock.Mock(side_effect=[False, False, True])
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "daemon_socket_alive", alive):
                with mock.patch.object(service, "_spawn") as spawn:
                    self.assertTrue(service.start_daemon(wait=0.5))
        spawn.assert_called_once_with("wayvoice.daemon")
        self.assertGreaterEqual(alive.call_count, 3)

    def test_reports_failure_when_the_daemon_never_answers(self):
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "daemon_socket_alive", return_value=False):
                with mock.patch.object(service, "_spawn"):
                    with mock.patch.object(service, "_wait_for_daemon", return_value=False) as waiter:
                        self.assertFalse(service.start_daemon(wait=0.2))
        waiter.assert_called_once_with(0.2)

    def test_spawn_uses_a_detached_interpreter_with_the_log_file(self):
        popen = mock.Mock()
        handle = mock.Mock()
        log_path = mock.Mock()
        log_path.parent = mock.Mock()
        log_path.open.return_value = handle
        with mock.patch.object(subprocess, "Popen", popen):
            with mock.patch.object(service, "service_log_path", return_value=log_path):
                with mock.patch.object(service, "python_executable", return_value="/usr/bin/python3"):
                    service._spawn("wayvoice.daemon")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["/usr/bin/python3", "-m", "wayvoice.daemon"])
        self.assertNotIn("shell", kwargs)
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], handle)
        self.assertIn("PYTHONPATH", kwargs["env"])
        log_path.parent.mkdir.assert_called_once_with(parents=True, exist_ok=True)
        self.assertTrue(handle.close.called)


class RestartDaemonTests(unittest.TestCase):
    def test_systemd_path_restarts_the_unit(self):
        with mock.patch.object(service, "systemd_available", return_value=True):
            with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as popen, mock.patch.object(service, "daemon_socket_alive", return_value=True):
                self.assertTrue(service.restart_daemon())
        popen.assert_called_once()
        self.assertEqual(
            popen.call_args[0][0], ["systemctl", "--user", "restart", "wayvoice.service"]
        )

    def test_direct_path_quits_and_starts_again(self):
        # Alive, then dead after the quit, then alive again after the start.
        alive = mock.Mock(side_effect=[True, False, False, True])
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "daemon_socket_alive", alive):
                with mock.patch.object(service, "_request", return_value={"ok": True}) as request:
                    with mock.patch.object(service, "start_daemon", return_value=True) as start:
                        self.assertTrue(service.restart_daemon(wait=0.3))
        request.assert_called_once_with("quit", timeout=1.0)
        start.assert_called_once_with(wait=0.3)

    def test_direct_path_terminates_a_daemon_that_ignores_quit(self):
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "daemon_socket_alive", return_value=True):
                with mock.patch.object(service, "_request", return_value={"ok": True}):
                    with mock.patch.object(service, "_force_stop_daemon") as force:
                        with mock.patch.object(service, "start_daemon", return_value=True):
                            self.assertTrue(service.restart_daemon(wait=0.1))
        force.assert_called_once()

    def test_direct_path_without_a_daemon_only_starts(self):
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "daemon_socket_alive", return_value=False):
                with mock.patch.object(service, "_request") as request:
                    with mock.patch.object(service, "start_daemon", return_value=True) as start:
                        self.assertTrue(service.restart_daemon())
        request.assert_not_called()
        start.assert_called_once_with(wait=5.0)


class EngineSetupRequestTests(unittest.TestCase):
    def test_systemd_path_starts_the_setup_unit(self):
        with mock.patch.object(service, "systemd_available", return_value=True):
            with mock.patch.object(subprocess, "Popen") as popen:
                self.assertIsNone(service.request_engine_setup())
        popen.assert_called_once()
        self.assertEqual(
            popen.call_args[0][0],
            ["systemctl", "--user", "--no-block", "start", "wayvoice-engine-setup.service"],
        )

    def test_direct_path_spawns_the_module(self):
        # The spawn is real (only Popen is mocked) so that what is checked is the
        # command line the user would get. Mocking the spawn too made "Popen was not
        # called" unreachable rather than true.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "state" / "engine-setup.log"
            with mock.patch.object(service, "systemd_available", return_value=False), \
                 mock.patch.object(subprocess, "Popen") as popen, \
                 mock.patch.object(service, "service_log_path", return_value=log), \
                 mock.patch.object(service, "python_executable", return_value="/usr/bin/python3"):
                self.assertIsNone(service.request_engine_setup())
        self.assertEqual(
            popen.call_args[0][0],
            ["/usr/bin/python3", "-m", "wayvoice.engine_setup"],
        )

    def test_failures_never_escape(self):
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "_spawn", side_effect=OSError("boom")):
                self.assertIsNone(service.request_engine_setup())


class ApplyShortcutTests(unittest.TestCase):
    def test_does_nothing_when_a_user_manager_is_there(self):
        with mock.patch.object(service, "systemd_available", return_value=True):
            with mock.patch.object(service, "apply_shortcut") as apply_shortcut:
                self.assertEqual(service.apply_shortcut_now(), (True, ""))
        apply_shortcut.assert_not_called()

    def test_applies_the_configured_shortcut_directly(self):
        cfg = {"shortcut": "F9"}
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "load_config", return_value=cfg):
                with mock.patch.object(service, "apply_shortcut", return_value=(True, "ok")) as apply_shortcut:
                    self.assertEqual(service.apply_shortcut_now(), (True, "ok"))
        apply_shortcut.assert_called_once_with("F9", None)

    def test_reports_a_failure_instead_of_raising(self):
        with mock.patch.object(service, "systemd_available", return_value=False):
            with mock.patch.object(service, "load_config", side_effect=OSError("boom")):
                ok, _ = service.apply_shortcut_now()
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
