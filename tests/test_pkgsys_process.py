"""Package transactions retain independent ownership until manager completion.

No real package managers or privilege changes are used.
"""
import os
import json
import shutil
import sys
import time
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import pkgsys, pkgsys_owner


class _FakeProc:
    def __init__(self, pid=4242, returncode=0, output=("", "")):
        self.pid = pid
        self.returncode = returncode
        self._output = output

    def communicate(self, timeout=None):
        return self._output

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass


class ManagerEnvironmentTests(unittest.TestCase):
    """What the child process inherits."""

    def _start(self):
        seen = {}

        def fake_popen(command, **kwargs):
            seen.update(kwargs)
            seen["command"] = list(command)
            return _FakeProc()

        with mock.patch.object(pkgsys.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(pkgsys, "requires_privilege", return_value=False), \
             mock.patch.object(pkgsys, "package_available", return_value=True):
            ok, message = pkgsys.install_packages(["wl-clipboard"], language="en")
        self.assertTrue(ok, message)
        return seen

    def test_nothing_may_read_the_users_terminal(self):
        self.assertEqual(self._start()["stdin"], subprocess.DEVNULL)

    def test_no_debconf_question_is_asked(self):
        self.assertEqual(self._start()["env"]["DEBIAN_FRONTEND"], "noninteractive")

    def test_the_output_is_read_in_the_c_locale(self):
        # apt translates; the availability check already forced C, the message shown to
        # the user did not.
        env = self._start()["env"]
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["LANG"], "C")

    def test_the_rest_of_the_environment_survives(self):
        with mock.patch.dict(os.environ, {"WAYVOICE_TEST_MARKER": "kept"}):
            self.assertEqual(self._start()["env"]["WAYVOICE_TEST_MARKER"], "kept")


class SerialisedInstallsTests(unittest.TestCase):
    """Two managers at once is how dpkg ends up locked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ,
                                  {"XDG_RUNTIME_DIR": str(Path(tmp.name))})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ran = []

    def _install(self):
        def fake_popen(command, **kwargs):
            self.ran.append(list(command))
            return _FakeProc()

        with mock.patch.object(pkgsys.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(pkgsys, "requires_privilege", return_value=False), \
             mock.patch.object(pkgsys, "package_available", return_value=True):
            code, out, err = pkgsys_owner.run(["fake-manager", "wl-clipboard"], "en")
            return pkgsys._result(code, ["wl-clipboard"], out, err, "en")

    def test_a_second_install_while_one_is_running_is_refused(self):
        # Held here the way another process would hold it.
        with pkgsys._install_lock(wait=0.1) as held:
            self.assertTrue(held)
            ok, message = self._install()
        self.assertFalse(ok, "two package managers were started at once")
        self.assertEqual(self.ran, [])
        self.assertIn("already running", message.lower())

    def test_the_lock_is_released_afterwards(self):
        ok, _message = self._install()
        self.assertTrue(ok)
        with pkgsys._install_lock(wait=0.1) as ours:
            self.assertTrue(ours, "the lock was not released")

    def test_the_lock_is_released_when_the_install_fails(self):
        def failing(command, **kwargs):
            raise OSError("no such manager")

        with mock.patch.object(pkgsys.subprocess, "Popen", side_effect=failing), \
             mock.patch.object(pkgsys, "requires_privilege", return_value=False), \
             mock.patch.object(pkgsys, "package_available", return_value=True):
            code, _out, _err = pkgsys_owner.run(["fake-manager", "wl-clipboard"], "en")
            ok = code == 0
        self.assertFalse(ok)
        with pkgsys._install_lock(wait=0.1) as ours:
            self.assertTrue(ours, "a failed install left the lock held")

    def test_an_unwritable_runtime_directory_refuses_unowned_install(self):
        with mock.patch.object(pkgsys, "_install_lock_path",
                               return_value=Path("/proc/opencode/nope/lock")):
            ok, message = self._install()
        self.assertFalse(ok, message)
        self.assertEqual(self.ran, [])


class TransactionOwnershipTests(unittest.TestCase):
    def test_timeout_does_not_signal_or_report_unfinished_transaction(self):
        proc = mock.Mock(returncode=0)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("owner", .1), ("", "")]
        with (mock.patch.object(pkgsys.subprocess, "Popen", return_value=proc),
              mock.patch.object(pkgsys.os, "killpg") as killpg):
            ok, _ = pkgsys._run_manager(["fake-manager"], ["x"], .1, "en")
        self.assertTrue(ok)
        self.assertEqual(proc.communicate.call_count, 2)
        proc.kill.assert_not_called()
        proc.terminate.assert_not_called()
        killpg.assert_not_called()

    def test_keyboard_interrupt_does_not_kill_independent_owner(self):
        proc = mock.Mock()
        proc.communicate.side_effect = KeyboardInterrupt
        with mock.patch.object(pkgsys.subprocess, "Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                pkgsys._run_manager(["fake-manager"], ["x"], .1, "en")
        proc.kill.assert_not_called()
        proc.terminate.assert_not_called()

    def test_caller_exit_keeps_owner_lock_until_fake_manager_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker, release, finished = (root / name for name in ("started", "release", "finished"))
            fake_manager = (
                "from pathlib import Path; import time; "
                f"Path({str(marker)!r}).touch(); "
                f"release=Path({str(release)!r})\n"
                "deadline=time.monotonic()+10\n"
                "while not release.exists() and time.monotonic()<deadline: time.sleep(.02)\n"
                f"Path({str(finished)!r}).touch()\n"
            )
            command = [sys.executable, "-c", fake_manager]
            caller_code = (
                "from wayvoice import pkgsys; "
                f"pkgsys._run_manager({command!r}, ['fake'], .1, 'en')"
            )
            environment = dict(os.environ, XDG_RUNTIME_DIR=directory)
            caller = subprocess.Popen([sys.executable, "-c", caller_code], env=environment,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "fake manager did not start")
                caller.terminate()
                caller.wait(timeout=5)
                with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                    with pkgsys._install_lock(wait=.1) as ours:
                        self.assertFalse(ours, "caller exit released a live transaction's lock")
                    release.touch()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with pkgsys._install_lock(wait=.1) as ours:
                            if ours:
                                break
                    self.assertTrue(ours, "completed owner did not release lock")
                self.assertTrue(finished.exists())
            finally:
                release.touch()
                if caller.poll() is None:
                    caller.terminate()
                caller.wait(timeout=5)

    def test_sanitized_elevation_still_sets_target_environment(self):
        # Fake elevation discards all inherited environment, just like pkexec.
        sanitize = "import os,sys; os.execve(sys.argv[1], sys.argv[1:], {})"
        report = "import os,json; print(json.dumps({k:os.environ.get(k) for k in ['DEBIAN_FRONTEND','LC_ALL','LANG']}))"
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            code, stdout, stderr = pkgsys_owner.run([
                sys.executable, "-c", sanitize, shutil.which("env"),
                "DEBIAN_FRONTEND=noninteractive", "LC_ALL=C", "LANG=C",
                sys.executable, "-c", report,
            ], "en")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout), {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C", "LANG": "C"})

    def test_owner_bounds_and_decodes_output_after_manager_exit(self):
        script = "import os; os.write(1, b'x'*100000 + b'last'); os.write(2, bytes([255]))"
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            code, stdout, stderr = pkgsys_owner.run([sys.executable, "-c", script], "en")
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout), 8192)
        self.assertTrue(stdout.endswith("last"))
        self.assertEqual(stderr, "\ufffd")

    def test_environment_is_set_after_pkexec(self):
        with (mock.patch.object(pkgsys, "detect_manager", return_value="apt"),
              mock.patch.object(pkgsys, "requires_privilege", return_value=True),
              mock.patch.object(pkgsys, "pkexec_path", return_value="/fake/pkexec"),
              mock.patch.object(pkgsys.shutil, "which", side_effect=lambda name: "/fake/" + name),
              mock.patch.object(pkgsys, "package_available", return_value=True),
              mock.patch.object(pkgsys, "_run_manager", return_value=(True, "")) as run):
            pkgsys.install_packages(["wl-clipboard"], language="en")
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["/fake/pkexec", "/fake/env", "DEBIAN_FRONTEND=noninteractive", "LC_ALL=C", "LANG=C"])
        self.assertIn("apt-get", command)


if __name__ == "__main__":
    unittest.main()
