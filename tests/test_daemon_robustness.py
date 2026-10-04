"""The daemon must survive the clients and the exits it did not plan for.

Each test here guards a specific way this daemon used to become unusable while
still looking alive:

* a client that connects and says nothing took the whole accept loop with it,
  because the accepted socket is blocking again after ``accept()``;
* a second daemon then judged the wedged one dead, unlinked its socket and left
  it running with the microphone;
* a daemon that exited mid-dictation left ``pw-record`` holding the device;
* a notification that failed turned a finished recording into an error.

No microphone, no notification daemon and no real hot key are needed.
"""

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import daemon as daemon_mod
from wayvoice.daemon import CLIENT_TIMEOUT, MAX_REQUEST_BYTES, WayVoiceDaemon
from wayvoice.protocol import owner_lock_path, socket_path

from support import isolate_engine, isolate_environment


def _ping(path, timeout=2.0):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall(b"ping\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        client.close()


class _ServerFixture:
    """A daemon serving in a thread, with its recorder faked out."""

    def __init__(self, test):
        self.test = test
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        runtime = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)})
        patch.start()
        test.addCleanup(patch.stop)
        # The deadline is what is being tested, not its length; a real five
        # seconds would make every one of these tests wait for it.
        short = mock.patch.object(daemon_mod, "CLIENT_TIMEOUT", 0.3)
        short.start()
        test.addCleanup(short.stop)
        self.path = socket_path()
        self.daemon = WayVoiceDaemon()
        # The real recorder would want a microphone; the shutdown behaviour is
        # tested separately below.
        self.daemon.recorder = mock.Mock()
        self.daemon._prepare_engine = mock.Mock()
        self.thread = threading.Thread(target=self.daemon.serve, daemon=True)
        self.cancelled = threading.Event()
        test.addCleanup(self._stop)
        self.thread.start()

    def _stop(self):
        self.daemon._shutdown.set()
        self.thread.join(timeout=10.0)

    def wait_until_serving(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if _ping(self.path, timeout=0.5):
                    return True
            except OSError:
                time.sleep(0.02)
        return False


class ServeRobustnessTests(unittest.TestCase):
    def setUp(self):
        isolate_engine(self)
        isolate_environment(self)
        self.server = _ServerFixture(self)
        self.assertTrue(self.server.wait_until_serving(), "the daemon did not start")
        self.path = self.server.path

    def _still_answers(self):
        # While the daemon is still inside CLIENT_TIMEOUT it does not answer
        # yet; that is the state this test is waiting out, not a failure.
        try:
            return bool(_ping(self.path, timeout=2.0))
        except OSError:
            return False

    def test_client_that_never_sends_anything_does_not_take_the_daemon_down(self):
        # The regression this guards: server.settimeout() covers accept() only,
        # so recv() on the accepted socket blocked until the client felt like
        # finishing. One such client disabled the hot key for good.
        quiet = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        quiet.settimeout(2.0)
        self.addCleanup(quiet.close)
        quiet.connect(str(self.path))
        quiet.sendall(b"")  # connected, sent nothing, stays open

        deadline = time.monotonic() + daemon_mod.CLIENT_TIMEOUT + 5.0
        while time.monotonic() < deadline and not self._still_answers():
            time.sleep(0.1)
        self.assertTrue(
            self._still_answers(),
            "the daemon never recovered from a client that said nothing",
        )

    def test_client_that_never_ends_its_line_does_not_take_the_daemon_down(self):
        partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        partial.settimeout(2.0)
        self.addCleanup(partial.close)
        partial.connect(str(self.path))
        partial.sendall(b"tog")  # a command without its newline

        deadline = time.monotonic() + daemon_mod.CLIENT_TIMEOUT + 5.0
        while time.monotonic() < deadline and not self._still_answers():
            time.sleep(0.1)
        self.assertTrue(self._still_answers())

    def test_client_cannot_make_the_daemon_buffer_without_bound(self):
        flooder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        flooder.settimeout(5.0)
        self.addCleanup(flooder.close)
        flooder.connect(str(self.path))
        block = b"x" * 4096
        sent = 0
        try:
            while sent < MAX_REQUEST_BYTES * 2:
                flooder.sendall(block)
                sent += len(block)
        except OSError:
            pass  # the daemon closing the connection is a valid answer
        self.assertTrue(self._still_answers(), "an oversized request broke the daemon")

    def test_the_daemon_still_answers_after_all_of_that(self):
        for _ in range(3):
            rude = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            rude.settimeout(2.0)
            rude.connect(str(self.path))
            rude.close()
        self.assertTrue(self._still_answers())


class OwnershipTests(unittest.TestCase):
    """Only one daemon may own the session, and it must be provable."""

    def setUp(self):
        isolate_engine(self)
        isolate_environment(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)

    def _serve_in_thread(self):
        daemon = WayVoiceDaemon()
        daemon.recorder = mock.Mock()
        daemon._prepare_engine = mock.Mock()
        thread = threading.Thread(target=daemon.serve, daemon=True)
        thread.start()
        return daemon, thread

    def test_a_second_daemon_is_turned_away_by_the_lock(self):
        first, thread = self._serve_in_thread()
        self.addCleanup(thread.join, 10.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not socket_path().exists():
            time.sleep(0.02)
        self.assertTrue(socket_path().exists())

        second = WayVoiceDaemon()
        second.recorder = mock.Mock()
        second._prepare_engine = mock.Mock()
        errors = []
        with mock.patch("sys.stderr") as err:
            second.serve()
            errors = err.write.call_args_list
        printed = " ".join(str(c) for c in errors)
        self.assertIn("already owns this session", printed)
        # The running daemon keeps its socket, and still answers.
        self.assertTrue(_ping(socket_path(), timeout=2.0))
        first._shutdown.set()

    def test_the_lock_is_released_when_the_daemon_stops(self):
        first, thread = self._serve_in_thread()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not socket_path().exists():
            time.sleep(0.02)
        first._shutdown.set()
        thread.join(timeout=10.0)
        self.assertFalse(thread.is_alive())
        # Nothing holds the lock any more, so a fresh daemon may start.
        import fcntl

        with open(owner_lock_path(), "w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class ShutdownTests(unittest.TestCase):
    """Leaving must not leave the microphone behind."""

    def setUp(self):
        isolate_engine(self)
        isolate_environment(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.daemon = WayVoiceDaemon()
        self.daemon.recorder = mock.Mock()
        self.daemon._prepare_engine = mock.Mock()
        self.thread = threading.Thread(target=self.daemon.serve, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not socket_path().exists():
            time.sleep(0.02)

    def test_quitting_stops_the_recorder(self):
        self.daemon.dispatch("quit")
        self.thread.join(timeout=10.0)
        self.assertFalse(self.thread.is_alive())
        # cancel() is what releases pw-record and removes the temporary file.
        self.daemon.recorder.cancel.assert_called_once()

    def test_an_exception_in_the_loop_still_stops_the_recorder(self):
        with mock.patch.object(
            self.daemon, "dispatch", side_effect=RuntimeError("boom")
        ):
            self.daemon._shutdown.set()
            self.thread.join(timeout=10.0)
        self.assertFalse(self.thread.is_alive())
        self.daemon.recorder.cancel.assert_called()


class NotifyTests(unittest.TestCase):
    """A notification is decoration and must never become an error."""

    def setUp(self):
        isolate_engine(self)
        isolate_environment(self)
        from wayvoice import notify as notify_mod

        self.notify_mod = notify_mod

    def test_a_missing_binary_is_not_an_error(self):
        with mock.patch.object(self.notify_mod.shutil, "which", return_value=None):
            self.notify_mod.notify("t", "b")  # must not raise

    def test_a_binary_that_vanished_between_lookup_and_run_is_not_an_error(self):
        # which() said yes, run() says no: this is the race that used to abort
        # a recording that had already started.
        with mock.patch.object(
            self.notify_mod.shutil, "which", return_value="/usr/bin/notify-send"
        ):
            with mock.patch.object(
                self.notify_mod.subprocess,
                "run",
                side_effect=FileNotFoundError("notify-send"),
            ):
                self.notify_mod.notify("t", "b")  # must not raise

    def test_a_hanging_notification_is_given_up_on(self):
        with mock.patch.object(
            self.notify_mod.shutil, "which", return_value="/usr/bin/notify-send"
        ):
            with mock.patch.object(
                self.notify_mod.subprocess,
                "run",
                side_effect=__import__("subprocess").TimeoutExpired("notify-send", 5),
            ):
                self.notify_mod.notify("t", "b")  # must not raise

    def test_a_failing_notification_does_not_break_a_recording(self):
        # The regression: notify() sat inside the try block of start_recording,
        # so its failure reported "could not start recording" while the
        # microphone was in fact open and the timer running.
        daemon = WayVoiceDaemon()
        daemon._prepare_engine = mock.Mock()
        daemon.recorder = mock.Mock()
        daemon._cancel_record_timer = mock.Mock()
        timer = mock.Mock()
        timer.daemon = True
        with mock.patch.object(daemon_mod, "notify", side_effect=FileNotFoundError):
            with mock.patch.object(daemon_mod, "threading") as threading_mod:
                threading_mod.Timer.return_value = timer
                threading_mod.Event.return_value = mock.Mock()
                reply = daemon.dispatch("start")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply.get("state"), "recording")


if __name__ == "__main__":
    unittest.main()
