"""Failure paths that used to end in "nothing happened".

Four of them, one per layer:

* a cancelled or timed-out recognition could block forever on the pipes of a
  child that ignored both signals;
* a package manager that hung was killed but its ``apt`` kept running and kept
  the dpkg lock, so the user's own retry failed with a message about a lock;
* ``setup-user`` reported success whatever it did, so a hotkey that was never
  applied looked like a working installation;
* the engine setup waited on its lock forever and ran pip without a deadline.

The processes here are fakes: what is under test is the reaction to a failure,
not the failing program.
"""

import subprocess
import unittest
from unittest import mock

from wayvoice import engine, pkgsys


class FakeChild:
    """A process that ignores everything, so the kill path has to give up."""

    def __init__(self, stdout="", stderr=""):
        self.stdout = mock.Mock()
        self.stderr = mock.Mock()
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = None
        self.signals = []
        self.waits = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("child", timeout or 0)
        return self.returncode

    def communicate(self, timeout=None):
        if self.returncode is None and timeout:
            raise subprocess.TimeoutExpired("child", timeout)
        return self._stdout, self._stderr

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")


class RunCancelableTests(unittest.TestCase):
    """The reason the user gets "timed out" has to be the actual reason."""

    def test_a_child_that_ignores_everything_does_not_block_forever(self):
        # The regression: after terminating, the old code called communicate()
        # with no timeout. A child that survived both signals left the daemon
        # busy for the rest of the session - it refused recording and only a
        # restart cleared it.
        child = FakeChild()
        with mock.patch.object(engine.subprocess, "Popen", return_value=child):
            with mock.patch.object(engine.os, "killpg", side_effect=OSError("no such process")):
                with self.assertRaises(engine.TranscriptionTimeout):
                    engine._run_cancelable(["x"], timeout=0.01, cancel_event=None)

    def test_cancellation_also_gives_up_on_an_unkillable_child(self):
        child = FakeChild()
        cancel = mock.Mock()
        cancel.is_set.side_effect = [True]
        with mock.patch.object(engine.subprocess, "Popen", return_value=child):
            with mock.patch.object(engine.os, "killpg", side_effect=OSError("no such process")):
                with self.assertRaises(engine.TranscriptionCancelled):
                    engine._run_cancelable(["x"], timeout=30.0, cancel_event=cancel)

    def test_a_child_that_floods_its_pipes_still_returns(self):
        # ctranslate2 is not quiet about a model it dislikes. Nothing drained the
        # pipes while the loop polled the child, so a full pipe buffer stopped it
        # from exiting and the user was told the recognition had timed out.
        script = (
            "import sys\n"
            "sys.stdout.write('x' * 400000)\n"
            "sys.stdout.write('done')\n"
        )
        result = engine._run_cancelable(
            [__import__("sys").executable, "-c", script],
            timeout=30.0,
            cancel_event=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), 400000 + 4)
        self.assertTrue(result.stdout.endswith("done"))


class InstallTimeoutTests(unittest.TestCase):
    """A timeout has to take the package manager's children with it.

    The privilege question is answered here rather than inherited: these tests
    are about what happens when the manager does not finish, and whether the
    caller is root, and whether pkexec is installed, are three different
    questions. On a machine where the answer was "not root and no pkexec" the
    code returned before starting anything, and the tests failed on a missing
    attribute instead of on the behaviour they are about.
    """

    ARGV = ["apt-get", "install", "-y", "wl-clipboard"]

    def _run(self, stderr: str):
        child = FakeChild(stderr=stderr)

        def fake_popen(command, **kwargs):
            child.command = command
            child.kwargs = kwargs
            return child

        def kill_tree(proc):
            proc.returncode = -9

        with mock.patch.object(pkgsys.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(pkgsys, "_kill_tree", side_effect=kill_tree), \
             mock.patch.object(pkgsys, "requires_privilege", return_value=False), \
             mock.patch.object(pkgsys, "pkexec_path", return_value=None) as pkexec, \
             mock.patch.object(pkgsys, "dry_run_command", return_value=list(self.ARGV)):
            ok, message = pkgsys.install_packages(
                ["wl-clipboard"], timeout=0.1, language="en"
            )
        # Proof that the path under test is the one these tests are about: had
        # the code asked for pkexec, the answer above would have turned the run
        # into "needs root privileges", and the tests would have been reporting
        # whatever this machine happens to have installed.
        self.assertFalse(pkexec.called, "the privileged path was taken")
        return ok, message, child

    def test_the_manager_runs_in_its_own_session(self):
        # Killing only pkexec leaves apt-get running, and apt still holds
        # /var/lib/dpkg/lock - which is what the user sees on the next attempt.
        _ok, _message, child = self._run("")
        self.assertEqual(child.command, self.ARGV)
        self.assertTrue(child.kwargs.get("start_new_session"))

    def test_a_timed_out_install_says_so_in_seconds(self):
        # The message has to name the deadline that was hit. "It contains a 1"
        # - which is what this used to assert - is true of half of apt's output.
        ok, message, _child = self._run("")
        self.assertFalse(ok)
        self.assertIn("1s", message)
        self.assertNotIn("0s", message)

    def test_the_reason_the_manager_gave_is_kept(self):
        _ok, message, _child = self._run("E: Could not get lock /var/lib/dpkg/lock")
        self.assertIn("dpkg/lock", message)


class SetupUserTests(unittest.TestCase):
    """The desktop integration must be able to report that it did not happen."""

    def _run_with(self, completed=None, side_effect=None):
        from wayvoice import cli

        with mock.patch.object(cli, "setup_user_script", return_value="/usr/bin/setup-user"):
            with mock.patch.object(
                cli.subprocess,
                "run",
                side_effect=side_effect,
                return_value=completed,
            ) as run:
                ok, detail = cli._run_setup_user()
        return ok, detail, run

    def test_a_failing_script_is_reported_with_its_reason(self):
        ok, detail, _run = self._run_with(
            subprocess.CompletedProcess(
                ["setup-user"], 1, "", "could not apply the global shortcut"
            )
        )
        self.assertFalse(ok)
        self.assertIn("global shortcut", detail)

    def test_a_successful_script_reports_success(self):
        ok, detail, _run = self._run_with(
            subprocess.CompletedProcess(["setup-user"], 0, "", "")
        )
        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_a_missing_script_is_reported_rather_than_silent(self):
        from wayvoice import cli

        with mock.patch.object(cli, "setup_user_script", return_value=None):
            ok, detail = cli._run_setup_user()
        self.assertFalse(ok)
        self.assertIn("setup-user", detail)

    def test_a_hanging_script_does_not_hang_the_install(self):
        ok, detail, _run = self._run_with(
            side_effect=subprocess.TimeoutExpired("setup-user", 30)
        )
        self.assertFalse(ok)
        self.assertIn("30s", detail)

    def test_the_script_runs_in_a_finite_time(self):
        # Without a timeout, a stuck setup-user would hang the dependency
        # install with no way out.
        _ok, _detail, run = self._run_with(
            subprocess.CompletedProcess(["setup-user"], 0, "", "")
        )
        self.assertGreater(run.call_args.kwargs["timeout"], 0)


class EngineSetupLockTests(unittest.TestCase):
    """A second setup process must not wait for a first one that never ends."""

    def test_a_held_lock_makes_the_second_process_leave(self):
        import fcntl
        import tempfile
        from pathlib import Path

        from wayvoice import engine_setup

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "engine-setup.lock"
            holder = lock_path.open("w")
            self.addCleanup(holder.close)
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with lock_path.open("w") as taken:
                # The handle the code under test is handed is closed here, not
                # left to the garbage collector: an unclosed file is a warning
                # in every run, and a warning nobody reads hides the ones that
                # are about production code.
                with mock.patch.object(engine_setup, "LOCK_TIMEOUT", 0.1):
                    self.assertFalse(engine_setup._take_lock(taken))

    def test_a_free_lock_is_taken(self):
        import tempfile
        from pathlib import Path

        from wayvoice import engine_setup

        with tempfile.TemporaryDirectory() as tmp:
            handle = (Path(tmp) / "engine-setup.lock").open("w")
            self.addCleanup(handle.close)
            self.addCleanup(handle.close)
            self.assertTrue(engine_setup._take_lock(handle))

    def test_pip_has_a_deadline(self):
        # A network that stops delivering used to leave the status at
        # "installing" forever: the window spun with no cancel button and every
        # dictation attempt spawned another setup process waiting on the lock.
        from wayvoice import engine_setup

        self.assertGreater(engine_setup.PIP_TIMEOUT, 0)
        self.assertGreater(engine_setup.LOCK_TIMEOUT, 0)

    def test_the_log_names_the_running_version(self):
        from wayvoice import __version__, engine_setup

        source = (engine_setup.__file__ or "")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("engine setup {__version__}", text)
        # A hardcoded version here was 0.5.0 while the app was 0.5.1, and
        # check-version.py has no way to see it.
        self.assertNotIn("engine setup 0.", text.replace("{__version__}", ""))


if __name__ == "__main__":
    unittest.main()
