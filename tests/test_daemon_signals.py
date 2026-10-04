"""Stopping the daemon has to stop the recording, however it was stopped.

A recorder runs in its own session, which is what lets it finalise its WAV header on a
deliberate signal - and also means no signal aimed at the daemon reaches it. The daemon
therefore has to end the recording itself on every way out: an orderly ``quit``, an
exception, and the signal systemd and ``_force_stop_daemon`` send.

These tests run the real daemon as a subprocess and signal it. Nothing is mocked on
purpose: the question is what survives a signal, and a mocked daemon cannot answer it.
The recorder is a stand-in that keeps writing until it is killed.
"""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from wayvoice import engine
from wayvoice.paths import app_src_dir, python_executable

from support import isolate_environment

#: Stands in for pw-record: writes the file, then keeps appending to it for as long
#: as it lives.
#:
#: The output path is the *last* argument. The real command line is
#: ``pw-record --rate=16000 --channels=1 --channel-map=mono <path>``, and a stand-in
#: that took the first argument wrote its recording into a file called ``--rate=16000``
#: in whatever directory the test ran from.
FAKE_PW_RECORD = """#!/bin/sh
for path do last="$path"; done
echo RIFF > "$last"
while true; do
    sleep 0.2
    printf x >> "$last"
done
"""


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a process that still holds its resources.

    A zombie is still a pid but holds nothing; what matters is whether the device and the
    file are still open.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def _children_of(pid: int) -> list[int]:
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            fields = Path(f"/proc/{name}/stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields and fields[1] == str(pid):
            found.append(int(name))
    return found


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return ""


class SignalShutdownTests(unittest.TestCase):
    """A daemon killed with a signal must not leave the recorder running."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The XDG variables have to be redirected in *this* process as well: the
        # configuration below is written from here.
        self.root = isolate_environment(self)
        # The runtime directory the redirect gave us, not one of our own: the child
        # reads the same variables this process just wrote the configuration into, and
        # two spellings of the same directory is how a test ends up talking to an empty
        # config.
        self.run_dir = Path(os.environ["XDG_RUNTIME_DIR"])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # The stand-in has to win the PATH lookup that audio.py does.
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        fake = self.bin_dir / "pw-record"
        fake.write_text(FAKE_PW_RECORD, encoding="utf-8")
        fake.chmod(0o755)
        self.socket_path = self.run_dir / "wayvoice.sock"
        self.daemon: subprocess.Popen | None = None
        self.addCleanup(self._cleanup)
        self._write_config()

    def _fake_runtime(self) -> Path:
        """A runtime that says "ready" and holds nothing.

        The daemon under test is a real process, so it asks the real engine whether it is
        prepared. With the data home pointed at an empty directory the answer is "missing", and
        the daemon then asks for the engine to be prepared - a detached ``pip install`` of
        faster-whisper into the test's own temporary directory, on every run of this file.

        The status looks for a stamp beside an interpreter, so a stamp beside a two-line script
        is a runtime that is ready and does nothing.
        """
        runtime = Path(os.environ["WAYVOICE_RUNTIME"])
        (runtime / "bin").mkdir(parents=True, exist_ok=True)
        interpreter = runtime / "bin" / "python"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)
        (runtime / engine.RUNTIME_STAMP).touch()
        return runtime

    def _env(self) -> dict:
        # Only the PATH and the runtime are changed on top of the redirect: every
        # XDG variable is already pointed at this test's own directory, and the
        # child must see exactly what this process sees.
        return {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "WAYVOICE_RUNTIME": str(self._fake_runtime()),
            "GSETTINGS_BACKEND": "memory",
            "PYTHONPATH": str(app_src_dir()),
            "PYTHONUNBUFFERED": "1",
        }

    def _cleanup(self) -> None:
        """End the recorder first, then the daemon.

        The order matters for the cleanup: once the daemon is gone the recorder is reparented
        and can no longer be found among its children, which is how a test run left nine of
        them behind, each holding a file open and writing to it forever.
        """
        proc = self.daemon
        self.daemon = None
        if proc is None:
            return
        recorder = self._recorder_of(proc.pid)
        if recorder:
            try:
                os.kill(recorder, signal.SIGKILL)
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        # The pipes belong to the test, not to the daemon: a Popen that is waited
        # for still holds its streams open, and every run would warn about two
        # leaked files until the collector got round to them.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for child in _children_of(proc.pid):
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass

    def _write_config(self, **settings) -> None:
        """A configuration the daemon under test cannot argue with.

        ``engine_worker`` is off on purpose: a warm worker is a child of the daemon that
        deliberately outlives it, and this file is about what the daemon must not leave behind.

        ``model`` is a local directory. The model cache is redirected too, so a catalogue model
        would be missing there and the daemon would refuse to record - correctly, and for a
        reason unrelated to these tests.

        The write happens with the XDG variables already redirected by ``isolate_environment``,
        and the assertion below is the proof: a test process that could still see the user's
        config fails here instead of quietly replacing their settings.
        """
        from wayvoice.config import DEFAULTS, config_path, save_config

        local_model = self.root / "local-model"
        local_model.mkdir(exist_ok=True)
        config = dict(DEFAULTS)
        config.update({
            "engine_worker": False,
            "notify": False,
            "model": str(local_model),
        })
        config.update(settings)
        save_config(config)
        self.assertIn(
            str(self.root), str(config_path()),
            "the test wrote outside its own directory",
        )

    def _recorder_of(self, daemon_pid: int) -> int:
        """The recorder among that daemon's children, found by its command."""
        for child in _children_of(daemon_pid):
            if "pw-record" in _cmdline(child):
                return child
        return 0

    def _recorder_pid(self) -> int:
        """The recorder of the daemon this test is talking to."""
        return self._recorder_of(self.daemon.pid) if self.daemon else 0

    def _ask(self, command: str, timeout: float = 5.0) -> dict:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        try:
            client.connect(str(self.socket_path))
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

    def _start_recording_daemon(self) -> int:
        """Start the real daemon and make it record; return the recorder's pid."""
        self.daemon = subprocess.Popen(
            [python_executable(), "-m", "wayvoice.daemon"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(),
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not self.socket_path.exists():
            time.sleep(0.05)
        self.assertTrue(self.socket_path.exists(), "the daemon never started serving")
        self.assertEqual(self._ask("start").get("ok"), True, "recording did not start")

        deadline = time.monotonic() + 10.0
        recorder = 0
        while time.monotonic() < deadline:
            recorder = self._recorder_pid()
            if recorder:
                break
            time.sleep(0.05)
        self.assertNotEqual(recorder, 0, "no recorder process was started")
        return recorder

    def _wait_gone(self, pid: int, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not _alive(pid):
                return True
            time.sleep(0.05)
        return not _alive(pid)

    def test_sigterm_does_not_leave_the_recorder_running(self):
        # Nothing handled SIGTERM, so neither the atexit hook nor the serve loop's
        # finally ran, and the recorder - in a session of its own - survived with the
        # microphone open. A cgroup hides this under systemd; a directly spawned daemon
        # has no such net.
        recorder = self._start_recording_daemon()
        self.assertTrue(_alive(recorder))

        os.kill(self.daemon.pid, signal.SIGTERM)
        self.assertTrue(self._wait_gone(self.daemon.pid), "the daemon ignored SIGTERM")
        self.assertTrue(
            self._wait_gone(recorder),
            "the recorder outlived the daemon and still holds the device",
        )

    def test_sigterm_ends_the_daemon_promptly(self):
        # Promptness is what keeps the SIGKILL escalation in service._force_stop_daemon
        # unreachable: if TERM were slow, the kill would arrive first.
        self._start_recording_daemon()
        started = time.monotonic()
        os.kill(self.daemon.pid, signal.SIGTERM)
        self.assertTrue(self._wait_gone(self.daemon.pid))
        self.assertLess(
            time.monotonic() - started, 5.0,
            "the daemon took longer than the escalation window to notice SIGTERM",
        )

    def test_the_daemon_leaves_no_socket_behind(self):
        # A daemon that cannot answer its socket is one the hot key and the
        # window both treat as dead, and the stale file is what the next start
        # has to clean up before it can serve again.
        self._start_recording_daemon()
        os.kill(self.daemon.pid, signal.SIGTERM)
        self.assertTrue(self._wait_gone(self.daemon.pid))
        self.assertFalse(
            self.socket_path.exists(),
            "the socket outlived the daemon",
        )

    def test_the_recorder_is_stopped_on_an_orderly_quit_too(self):
        # The signal path must not be the only one that works: this is the path the
        # window and the hot key use.
        recorder = self._start_recording_daemon()
        self.assertEqual(self._ask("quit").get("ok"), True)
        self.assertTrue(self._wait_gone(recorder))
        self.assertTrue(self._wait_gone(self.daemon.pid))

    def test_no_recording_survives_a_signal_that_never_happens(self):
        # A daemon that is only serving has nothing to clean up and must not invent
        # work: this is the state the system spends most of its time in.
        self.daemon = subprocess.Popen(
            [python_executable(), "-m", "wayvoice.daemon"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._env(),
        )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not self.socket_path.exists():
            time.sleep(0.05)
        self.assertTrue(self._ask("ping").get("ok"))
        os.kill(self.daemon.pid, signal.SIGTERM)
        self.assertTrue(self._wait_gone(self.daemon.pid))


if __name__ == "__main__":
    unittest.main()
