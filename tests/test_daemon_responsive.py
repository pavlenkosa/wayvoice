"""The daemon has to answer while it is doing something else.

The accept loop serves one client at a time, and the settings window asks for
the status every 650 ms with a timeout of 0.12 s.  So anything the loop does
inline is a window of time in which the window decides the daemon is dead,
disables the microphone button and says "starting" - and the hot key, which
allows 1.5 s, silently does nothing.

Two things used to make that window seconds wide: the desktop notification, a
subprocess with a five second deadline, was sent inline while the daemon held
its own lock; and every command was dispatched on the loop itself, so a
``stop_recording`` that waits for the recorder to flush took the whole daemon
with it.

The tests below pin the design rather than an implementation: ``ping`` and
``status`` are answered on the loop, everything else is handed to one worker,
and a notification is never waited for.
"""

import json
import socket
import threading
import time
import unittest
from unittest import mock

from wayvoice import daemon as daemon_mod
from wayvoice import notify as notify_mod
from wayvoice.daemon import WayVoiceDaemon
from wayvoice.protocol import socket_path

from support import isolate_engine, isolate_environment

#: The real dispatcher, captured before any test patches the class: the tests
#: below make one command slow and need every other one to behave normally.
REAL_DISPATCH = WayVoiceDaemon.dispatch


def _ask(command: str, timeout: float = 5.0) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(socket_path()))
        client.sendall(f"{command}\n".encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode()) if data else {}
    except (OSError, ValueError):
        return {}
    finally:
        client.close()


class _Server:
    """A real daemon serving in a thread, with its engine and recorder faked out."""

    def __init__(self, test):
        test.root = isolate_environment(test)
        isolate_engine(test)
        self.daemon = WayVoiceDaemon()
        self.daemon.recorder = mock.Mock(recording=False)
        self.daemon._prepare_engine = mock.Mock()
        for target, value in (
            ("wayvoice.daemon.load_config", {"model": "small", "notify": False}),
            ("wayvoice.daemon.engine_status", {"state": "ready", "message": "Ready"}),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            test.addCleanup(patcher.stop)
        self.thread = threading.Thread(target=self.daemon.serve, daemon=True)
        test.addCleanup(self._stop)
        self.thread.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if _ask("ping", 0.5).get("ok"):
                return
            time.sleep(0.02)
        test.fail("the daemon never started serving")

    def _stop(self):
        self.daemon._shutdown.set()
        self.thread.join(timeout=20.0)


class SlowCommand:
    """One command that blocks until the test lets it go."""

    def __init__(self, slow: str = "start"):
        self.slow = slow
        self.running = threading.Event()
        self.release = threading.Event()

    def __call__(self, daemon, command):
        if str(command) != self.slow:
            return REAL_DISPATCH(daemon, command)
        self.running.set()
        self.release.wait(20.0)
        return {"ok": True, "state": "recording"}

    def wait_until_running(self, timeout: float = 10.0) -> None:
        if not self.running.wait(timeout):
            raise AssertionError(f"{self.slow!r} never started")


class InlineAnswersTests(unittest.TestCase):
    """While one command is slow, the questions must still be answered."""

    def setUp(self):
        self.server = _Server(self)

    def _with_slow_command(self, slow: str, check):
        """Request ``slow`` the way a client does, then run ``check`` while it runs.

        The request goes over the socket on purpose: calling ``dispatch``
        directly would prove nothing, because the whole question is whether the
        loop that serves clients is the thread doing the slow work.
        """
        blocker = SlowCommand(slow)
        replies = []
        with mock.patch.object(
            daemon_mod.WayVoiceDaemon, "dispatch", autospec=True, side_effect=blocker
        ):
            client = threading.Thread(
                target=lambda: replies.append(_ask(slow, 20.0)), daemon=True
            )
            client.start()
            blocker.wait_until_running()
            try:
                check()
            finally:
                blocker.release.set()
                client.join(timeout=25.0)
        self.assertEqual(len(replies), 1, "the slow command was never answered")

    def test_the_status_poll_is_answered_while_a_command_runs(self):
        # The window's own numbers: 0.12 s, every 650 ms. A failure here is the
        # window showing "starting" with a disabled microphone.
        def check():
            reply = _ask("status", 0.12)
            self.assertTrue(reply.get("ok"), f"the window would have seen: {reply}")
            self.assertIn("recording", reply)

        self._with_slow_command("start", check)

    def test_a_ping_is_answered_while_a_command_runs(self):
        # The shortcut's liveness probe allows 0.2 s, and a slow answer here is
        # what makes service.py believe the daemon is gone.
        self._with_slow_command("start", lambda: self.assertTrue(_ask("ping", 0.2).get("ok")))

    def test_a_stopping_daemon_does_not_stop_answering(self):
        # stop_recording waits for the recorder to flush its WAV header, which is
        # the longest inline operation the daemon has.
        self._with_slow_command(
            "stop", lambda: self.assertTrue(_ask("status", 0.12).get("ok"))
        )

    def test_the_slow_command_still_gets_its_own_answer(self):
        # Being slow must not mean being unanswered.  The reply has to reach the
        # client that asked, on the connection it asked on - the whole point of
        # moving the work to another thread is that the reply still travels back
        # to the right place.
        blocker = SlowCommand("start")
        replies = []
        with mock.patch.object(
            daemon_mod.WayVoiceDaemon, "dispatch", autospec=True, side_effect=blocker
        ):
            client = threading.Thread(
                target=lambda: replies.append(_ask("start", 20.0)), daemon=True
            )
            client.start()
            blocker.wait_until_running()
            blocker.release.set()
            client.join(timeout=25.0)
        self.assertEqual(len(replies), 1, "the slow command was never answered")
        self.assertTrue(replies[0].get("ok"), replies[0])


class CommandWorkerTests(unittest.TestCase):
    """The worker is one thread serving every slow command: it has to survive one."""

    def setUp(self):
        self.server = _Server(self)

    def test_a_command_that_raises_answers_the_client_and_keeps_the_daemon(self):
        # A crash in the worker would take the hot key, the window and the CLI
        # with it, and every client behind it would hang until it timed out.
        calls = {"n": 0}

        def dispatch(daemon, command):
            if command == "prepare-model" and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("engine exploded")
            return REAL_DISPATCH(daemon, command)

        with mock.patch.object(
            daemon_mod.WayVoiceDaemon, "dispatch", autospec=True, side_effect=dispatch
        ):
            reply = _ask("prepare-model", 10.0)
        self.assertFalse(reply.get("ok"), reply)
        self.assertIn("engine exploded", str(reply.get("error")))
        self.assertTrue(_ask("ping", 1.0).get("ok"), "the worker did not survive")

    def test_a_client_that_hangs_up_mid_command_does_not_break_the_worker(self):
        # The reply goes to a socket nobody is on; the worker must move on to the
        # next client rather than treat it as its own failure.
        with mock.patch.object(
            daemon_mod.WayVoiceDaemon,
            "dispatch",
            autospec=True,
            side_effect=lambda d, c: REAL_DISPATCH(d, c),
        ):
            rude = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            rude.settimeout(5.0)
            rude.connect(str(socket_path()))
            rude.sendall(b"prepare-model\n")
            rude.close()
            self.assertTrue(_ask("ping", 2.0).get("ok"))


class NotifyTests(unittest.TestCase):
    """A notification is decoration, and the caller must not wait for one."""

    class _Stuck:
        """A notification program that never finishes on its own."""

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.returncode = None
            self.released = threading.Event()

        def wait(self, timeout=None):
            self.released.wait(30.0)
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            # The signature matters: subprocess.run() calls
            # ``process.communicate(input, timeout=timeout)``, and a stub with
            # fewer parameters raises TypeError inside notify(), which notify()
            # then swallows - so the test would pass on code that does exactly
            # what it is meant to catch.
            self.wait(timeout)

        def poll(self):
            return self.returncode

        def kill(self):
            self.released.set()
            self.returncode = -9

        def __enter__(self):
            # subprocess.run() does `with Popen(...) as process`; without this
            # the fake raises inside notify(), which swallows it, and the test
            # would be measuring its own broken stub instead of the code.
            return self

        def __exit__(self, *_exc):
            return False

    def setUp(self):
        patcher = mock.patch.object(
            notify_mod.shutil, "which", return_value="/usr/bin/notify-send"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.procs = []
        popen = mock.patch.object(
            notify_mod.subprocess,
            "Popen",
            side_effect=lambda *a, **k: self.procs.append(self._Stuck(*a, **k)) or self.procs[-1],
        )
        popen.start()
        self.addCleanup(popen.stop)

    def _release_all(self):
        for proc in self.procs:
            proc.released.set()

    def test_notify_returns_before_the_program_finishes(self):
        # Otherwise every dictation ends with a pause of up to the notification's
        # own deadline, and a stalled session bus makes it the longest part of
        # pressing the key.
        self.addCleanup(self._release_all)
        began = time.monotonic()
        notify_mod.notify("WayVoice", "recording started")
        elapsed = time.monotonic() - began
        self.assertLess(
            elapsed, 2.0, "notify() waited for the program it had just started"
        )
        self.assertEqual(len(self.procs), 1, "the notification was never started")

    def test_a_notification_that_never_returns_is_abandoned_not_waited_for(self):
        # Three in a row, because one would also pass if the wait were merely
        # short: the point is that the caller is not held at all. The programs
        # are still running, which is the "abandoned" part - and nothing may have
        # released them, or the test would be measuring its own cleanup.
        self.addCleanup(self._release_all)
        began = time.monotonic()
        for _ in range(3):
            notify_mod.notify("WayVoice", "text")  # must not raise
        self.assertLess(
            time.monotonic() - began, 2.0, "three notifications took too long"
        )
        self.assertEqual(len(self.procs), 3)
        self.assertTrue(
            all(not p.released.is_set() for p in self.procs),
            "the programs were given up on before the test let them go",
        )

    def test_a_notification_that_cannot_start_is_not_an_error(self):
        # which() said yes and the program is gone: the race that used to abort a
        # recording that had already started. Nothing may be started, and nothing
        # may be raised.
        self.addCleanup(self._release_all)
        with mock.patch.object(notify_mod.subprocess, "Popen", side_effect=OSError):
            notify_mod.notify("WayVoice", "text")  # must not raise
        self.assertEqual(self.procs, [], "a program was left half-started")

    def test_a_missing_binary_is_still_not_an_error(self):
        with mock.patch.object(notify_mod.shutil, "which", return_value=None):
            notify_mod.notify("WayVoice", "text")  # must not raise
        self.assertEqual(self.procs, [], "a program was started without a binary")

    def test_a_lookup_that_fails_is_still_not_an_error(self):
        # shutil.which is a filesystem lookup and can raise on its own; the two
        # steps being separate is what makes this reachable at all.
        with mock.patch.object(
            notify_mod.shutil, "which", side_effect=OSError("no PATH")
        ):
            notify_mod.notify("WayVoice", "text")  # must not raise
        self.assertEqual(self.procs, [])

    def test_notifications_can_be_switched_off(self):
        with mock.patch.object(notify_mod.shutil, "which", return_value=None):
            notify_mod.notify("WayVoice", "text", enabled=False)
        self.assertEqual(self.procs, [])


if __name__ == "__main__":
    unittest.main()
