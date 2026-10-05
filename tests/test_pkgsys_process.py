"""Installing packages: what the process is started with, and what happens on the way out.

Three defects, all found by reading :mod:`wayvoice.pkgsys`:

* No serialisation at all. The settings window guards only against starting the same
  dependency twice, so clicking Install on two different missing dependencies ran two
  managers at once and the loser failed with "Unable to acquire dpkg frontend lock".
  The comment in this module was already worried about exactly that failure - for the
  timeout case, which had protection.

* The manager inherited stdin and the process locale. With no ``DEBIAN_FRONTEND``, a
  maintainer script asking a debconf question read the inherited terminal (or, in a
  service, ``/dev/null``) and waited for the full timeout before dpkg was killed. And
  because apt translates its output, the tail handed to the window came back in the
  system's language inside an interface string in the user's language.

* The escalation after a timeout followed the process, not the group: it waited for the
  direct child to exit and returned. dpkg, a grandchild, ignores SIGTERM - so it never
  saw the SIGKILL and survived holding the dpkg lock, which is the failure the wait was
  written to prevent.

Nothing here runs a package manager: ``Popen`` is replaced throughout.
"""

import itertools
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import pkgsys


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
            return pkgsys.install_packages(["wl-clipboard"], language="en")

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
            ok, _message = pkgsys.install_packages(["wl-clipboard"], language="en")
        self.assertFalse(ok)
        with pkgsys._install_lock(wait=0.1) as ours:
            self.assertTrue(ours, "a failed install left the lock held")

    def test_an_unwritable_runtime_directory_does_not_block_an_install(self):
        with mock.patch.object(pkgsys, "_install_lock_path",
                               return_value=Path("/proc/opencode/nope/lock")):
            ok, message = self._install()
        self.assertTrue(ok, message)
        self.assertEqual(len(self.ran), 1)


class KillTreeTests(unittest.TestCase):
    """Escalation has to follow the group, or dpkg outlives it."""

    def test_a_grandchild_is_signed_off_too(self):
        proc = _FakeProc(pid=4321)
        sent = []
        group = {"alive": True}

        def fake_killpg(pgid, sig):
            if sig == 0:
                return  # the liveness probe
            sent.append(sig)
            # dpkg ignores SIGTERM, so the group is still there after it and only
            # SIGKILL empties it.
            group["alive"] = sig != signal.SIGKILL

        with mock.patch.object(pkgsys.os, "killpg", side_effect=fake_killpg), \
             mock.patch.object(pkgsys, "_group_alive",
                               side_effect=lambda pgid: group["alive"]), \
             mock.patch.object(pkgsys.time, "sleep"), \
             mock.patch.object(pkgsys.time, "monotonic",
                               side_effect=itertools.count(0.0, 0.5)):
            pkgsys._kill_tree(proc)
        self.assertEqual(sent, [signal.SIGTERM, signal.SIGKILL],
                         "the group was left after SIGTERM")

    def test_a_gone_group_is_not_signed_again(self):
        proc = _FakeProc(pid=4321)
        sent = []
        with mock.patch.object(pkgsys.os, "killpg",
                               side_effect=lambda pgid, sig: sent.append(sig)), \
             mock.patch.object(pkgsys, "_group_alive", return_value=False), \
             mock.patch.object(pkgsys.time, "sleep"):
            pkgsys._kill_tree(proc)
        self.assertEqual(sent, [signal.SIGTERM])

    def test_a_permission_error_is_not_taken_for_a_gone_group(self):
        # EPERM means the process is there and belongs to someone else.
        with mock.patch.object(pkgsys.os, "killpg", side_effect=PermissionError):
            self.assertTrue(pkgsys._group_alive(999))


if __name__ == "__main__":
    unittest.main()