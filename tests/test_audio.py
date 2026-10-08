"""The recorder is where a failure turns into "it did not work".

The microphone is opened by an external program (``pw-record``) whose failure modes the
daemon only learns about afterwards, and they are the paths a user hits when PipeWire is
not ready yet: a missing binary, a program that dies immediately with a message, one
that dies later leaving a half-written file, and a cancel that has to clean up both the
process and the temporary recording.
"""

import ctypes
import ctypes.util
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import audio


class FakeProc:
    """``Popen`` stand-in whose lifetime the test controls."""

    def __init__(self, *, alive=True, exit_code=1, stderr="", hang=False,
                 hang_first=False):
        self.alive = alive
        self.exit_code = exit_code
        self.stderr_text = stderr
        # ``hang``: never let a wait() succeed.  ``hang_first``: only the startup
        # probe's wait() times out, which is how a recorder that started fine
        # looks - the 80 ms check in AudioRecorder.start expects no exit yet.
        self.hang = hang
        self.hang_first = hang_first
        self.signals: list[int] = []
        self.killed = False
        self.terminated = False
        self.waited = 0
        self.stderr = mock.Mock()
        self.stderr.read.return_value = stderr
        self.stdin = None
        self.pid = 1234
        self.poll_count = 0

    def poll(self):
        self.poll_count += 1
        return None if self.alive else self.exit_code

    def wait(self, timeout=None):
        self.waited += 1
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("pw-record", timeout or 0)
        if self.hang_first and self.waited == 1:
            raise subprocess.TimeoutExpired("pw-record", timeout or 0)
        self.alive = False
        return self.exit_code

    def terminate(self):
        self.signals.append(15)
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def send_signal(self, sig):
        self.signals.append(sig)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.recorder = audio.AudioRecorder()
        self.addCleanup(self.recorder.cancel)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths: list[Path] = []

    def _patch_which(self, present=True):
        return mock.patch.object(
            audio.shutil, "which", return_value="/usr/bin/pw-record" if present else None
        )

    def _patch_popen(self, proc):
        real = audio.subprocess.Popen

        def factory(cmd, **kwargs):
            self.cmd = cmd
            self.kwargs = kwargs
            return proc

        return mock.patch.object(audio.subprocess, "Popen", side_effect=factory)

    def test_backend_truncation_keeps_recording_private_for_any_umask(self):
        library = ctypes.util.find_library("sndfile")
        if not library:
            self.skipTest("libsndfile unavailable")
        backend = ctypes.CDLL(library)

        class Info(ctypes.Structure):
            _fields_ = [("frames", ctypes.c_int64), ("samplerate", ctypes.c_int),
                        ("channels", ctypes.c_int), ("format", ctypes.c_int),
                        ("sections", ctypes.c_int), ("seekable", ctypes.c_int)]

        backend.sf_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(Info)]
        backend.sf_open.restype = ctypes.c_void_p
        backend.sf_write_short.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_short),
                                          ctypes.c_int64]
        backend.sf_write_short.restype = ctypes.c_int64
        backend.sf_close.argtypes = [ctypes.c_void_p]
        backend.sf_close.restype = ctypes.c_int
        for mask in (0o000, 0o002, 0o022):
            with self.subTest(umask=mask):
                proc = FakeProc(alive=True, hang_first=True)

                def open_backend(cmd, **kwargs):
                    path = Path(cmd[-1])
                    self.assertTrue(path.is_file(), "private inode must exist before backend opens it")
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    previous = os.umask(mask)
                    try:
                        info = Info(0, 16000, 1, 0x010002, 0, 0)  # WAV/PCM16
                        handle = backend.sf_open(os.fsencode(path), 0x20, ctypes.byref(info))
                        self.assertTrue(handle, "libsndfile cannot open precreated WAV")
                        try:
                            samples = (ctypes.c_short * 1024)()
                            self.assertEqual(backend.sf_write_short(handle, samples, 1024), 1024)
                        finally:
                            self.assertEqual(backend.sf_close(handle), 0)
                    finally:
                        os.umask(previous)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    return proc

                with self._patch_which(), mock.patch.object(audio.subprocess, "Popen", side_effect=open_backend):
                    self.recorder.start()
                    path = self.recorder.stop_to_wav()
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.recorder.cancel()
                self.assertTrue(path.exists(), "ASR owns a handed-off recording")
                path.unlink()

    def test_failed_spawn_removes_private_file_and_handles(self):
        paths = []

        def fail(cmd, **kwargs):
            paths.append(Path(cmd[-1]))
            raise OSError("spawn denied")

        with self._patch_which(), mock.patch.object(audio.subprocess, "Popen", side_effect=fail):
            with self.assertRaisesRegex(OSError, "spawn denied"):
                self.recorder.start()
        self.assertFalse(paths[0].exists())
        self.assertIsNone(self.recorder._proc)
        self.assertIsNone(self.recorder._path)

    def test_cancel_removes_private_file_even_if_termination_fails(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        with mock.patch.object(proc, "terminate", side_effect=ProcessLookupError()):
            with self.assertRaises(ProcessLookupError):
                self.recorder.cancel()
        self.assertFalse(path.exists())
        self.assertIsNone(self.recorder._proc)
        self.assertIsNone(self.recorder._path)

    def test_a_missing_pw_record_is_reported_by_name(self):
        with self._patch_which(present=False):
            with self.assertRaises(RuntimeError) as caught:
                self.recorder.start()
        self.assertIn("pw-record", str(caught.exception))

    def test_a_recorder_that_dies_immediately_reports_its_message(self):
        # PipeWire not running yet looks exactly like this: pw-record exits in
        # milliseconds with the reason on stderr.
        proc = FakeProc(alive=False, stderr="Cannot connect to PipeWire")
        with self._patch_which(), self._patch_popen(proc):
            with self.assertRaises(RuntimeError) as caught:
                self.recorder.start()
        self.assertIn("Cannot connect to PipeWire", str(caught.exception))
        self.assertFalse(self.recorder.recording)

    def test_a_recorder_that_dies_immediately_leaves_no_file_behind(self):
        tmpdir = Path(tempfile.gettempdir())
        before = set(tmpdir.glob("wayvoice-*.wav"))
        proc = FakeProc(alive=False, stderr="nope")
        with self._patch_which(), self._patch_popen(proc):
            with self.assertRaises(RuntimeError):
                self.recorder.start()
        # The recorder must not leave a file for a recording that never began;
        # a stale one here is a file in /tmp that nobody will ever clean up.
        self.assertEqual(set(tmpdir.glob("wayvoice-*.wav")) - before, set())
        self.assertIsNone(self.recorder._path)

    def test_a_healthy_recorder_reports_itself_as_recording(self):
        proc = FakeProc(alive=True, hang_first=True)  # the probe times out: it started
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        self.assertTrue(self.recorder.recording)
        self.assertIsNotNone(self.recorder._path)

    def test_the_recorder_runs_in_its_own_session(self):
        # Without this, killing the daemon leaves pw-record alive: it keeps the
        # microphone and keeps writing to /tmp, and nothing knows it is there.
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        self.assertTrue(self.kwargs.get("start_new_session"))

    def test_stopping_returns_the_recording(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
            path = self.recorder._path
            path.write_bytes(b"\0" * 4096)
            result = self.recorder.stop_to_wav()
        self.assertEqual(result, path)
        # SIGINT is what lets pw-record finalise its WAV header.
        self.assertEqual(proc.signals, [2])

    def test_an_empty_recording_is_reported_and_removed(self):
        proc = FakeProc(alive=True, stderr="no streams", hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
            path = self.recorder._path
            path.write_bytes(b"\0" * 16)
            with self.assertRaises(RuntimeError) as caught:
                self.recorder.stop_to_wav()
        self.assertIn("no streams", str(caught.exception))
        self.assertFalse(path.exists(), "the empty recording was left on disk")

    def test_stopping_without_a_recording_is_an_error_not_a_crash(self):
        with self.assertRaises(RuntimeError):
            self.recorder.stop_to_wav()

    def test_cancel_stops_the_process_and_removes_the_file(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
            path = self.recorder._path
            path.write_bytes(b"\0" * 2048)
            self.recorder.cancel()
        self.assertTrue(proc.terminated or proc.killed)
        self.assertFalse(path.exists())
        self.assertFalse(self.recorder.recording)

    def test_cancel_is_safe_when_nothing_is_running(self):
        # Called by the exit hook of a daemon that never recorded anything, and
        # again from the timer of a recording that has already stopped.
        self.recorder.cancel()  # must not raise
        self.recorder.cancel()
        self.assertFalse(self.recorder.recording, "the recorder still believes it runs")
        self.assertIsNone(self.recorder._proc, "a process was left behind")

    def test_cancel_reaps_a_process_that_ignores_the_signal(self):
        proc = FakeProc(alive=True, hang=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
            self.recorder.cancel()
        self.assertTrue(proc.killed)
        self.assertGreaterEqual(proc.waited, 2, "the killed process was never reaped")

    def test_a_recorder_that_died_on_its_own_still_has_its_file_cleaned(self):
        # pw-record can die mid-recording (PipeWire restarted, device unplugged). The
        # daemon then sees "not recording" while a half-written file is still on disk,
        # and cancel() is the only thing that will remove it.
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
            path = self.recorder._path
            path.write_bytes(b"\0" * 2048)
            proc.alive = False  # it exited on its own
            self.assertFalse(self.recorder.recording)
            self.recorder.cancel()
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
