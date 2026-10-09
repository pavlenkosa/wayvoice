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
import sys
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from wayvoice import audio


class FakeProc:
    """``Popen`` stand-in whose lifetime the test controls."""

    def __init__(self, *, alive=True, exit_code=0, stderr="", hang=False,
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

    def test_cancel_cleans_take_when_process_exits_before_termination(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        def already_gone():
            proc.alive = False
            raise ProcessLookupError()
        with mock.patch.object(proc, "terminate", side_effect=already_gone):
            with self.assertRaises(ProcessLookupError):
                self.recorder.cancel()
        self.assertFalse(path.exists())
        self.assertIsNone(self.recorder._proc)
        self.assertIsNone(self.recorder._path)

    def test_cancel_retains_live_process_and_private_take_on_signal_failure(self):
        for signal_name in ("terminate", "kill"):
            with self.subTest(signal=signal_name):
                proc = FakeProc(alive=True, hang_first=True)
                with self._patch_which(), self._patch_popen(proc):
                    self.recorder.start()
                path = self.recorder._path
                self.addCleanup(path.unlink, missing_ok=True)
                patches = [mock.patch.object(proc, signal_name, side_effect=PermissionError("denied"))]
                if signal_name == "kill":
                    patches.append(mock.patch.object(proc, "terminate"))
                    patches.append(mock.patch.object(proc, "wait", side_effect=subprocess.TimeoutExpired("pw-record", 1)))
                with patches[0]:
                    if signal_name == "kill":
                        with patches[1], patches[2]:
                            with self.assertRaises(PermissionError):
                                self.recorder.cancel()
                    else:
                        with self.assertRaises(PermissionError):
                            self.recorder.cancel()
                self.assertIs(self.recorder._proc, proc)
                self.assertEqual(self.recorder._path, path)
                self.assertTrue(self.recorder.recording)
                self.assertTrue(path.exists())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                proc.stderr.close.assert_not_called()
                self.recorder.cancel()
                self.assertFalse(self.recorder.recording)
                self.assertFalse(path.exists())
                proc.stderr.close.assert_called_once()

    def test_cancel_timeout_retains_owner_until_retry(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.object(proc, "terminate"), mock.patch.object(proc, "kill"), \
             mock.patch.object(proc, "wait", side_effect=subprocess.TimeoutExpired("pw-record", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.recorder.cancel()
        self.assertIs(self.recorder._proc, proc)
        self.assertTrue(path.exists())
        proc.stderr.close.assert_not_called()
        self.recorder.cancel()
        self.assertFalse(path.exists())

    def test_failed_stop_and_cancel_keep_live_recorder_for_retry(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        self.addCleanup(path.unlink, missing_ok=True)
        with mock.patch.object(proc, "send_signal", side_effect=PermissionError("stop denied")), \
             mock.patch.object(proc, "terminate", side_effect=PermissionError("cancel denied")):
            with self.assertRaisesRegex(RuntimeError, "stop denied.*cancel denied"):
                self.recorder.stop_to_wav()
        self.assertIs(self.recorder._proc, proc)
        self.assertEqual(self.recorder._path, path)
        self.assertTrue(path.exists())
        proc.stderr.close.assert_not_called()
        self.recorder.cancel()
        self.assertFalse(path.exists())
        proc.stderr.close.assert_called_once()

    def test_unlink_failure_blocks_new_spawn_until_cleanup_succeeds(self):
        old = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(old):
            self.recorder.start()
        path = self.recorder._path
        self.addCleanup(path.unlink, missing_ok=True)
        unlink = Path.unlink
        def deny(take, *args, **kwargs):
            if take == path:
                raise PermissionError("cleanup denied")
            return unlink(take, *args, **kwargs)
        with mock.patch.object(Path, "unlink", deny):
            with self.assertRaises(PermissionError):
                self.recorder.cancel()
            self.assertIsNone(self.recorder._proc)
            self.assertEqual(self.recorder._path, path)
            with mock.patch.object(audio.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(PermissionError, "cleanup denied"):
                    self.recorder.start()
            spawn.assert_not_called()
        fresh = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(fresh):
            self.recorder.start()
        self.assertFalse(path.exists())
        self.assertIs(self.recorder._proc, fresh)
        self.assertNotEqual(self.recorder._path, path)

    def test_failed_spawn_retains_path_if_cleanup_also_fails(self):
        with self._patch_which(), mock.patch.object(audio.subprocess, "Popen", side_effect=OSError("spawn denied")), \
             mock.patch.object(Path, "unlink", side_effect=PermissionError("cleanup denied")):
            with self.assertRaisesRegex(RuntimeError, "spawn denied.*cleanup denied"):
                self.recorder.start()
        path = self.recorder._path
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())
        self.assertIsNone(self.recorder._proc)
        self.recorder.cancel()
        self.assertFalse(path.exists())
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

    def test_pipewire_signal_exit_one_requires_complete_wav(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                proc = FakeProc(alive=True, hang_first=True, exit_code=1)
                with self._patch_which(), self._patch_popen(proc):
                    self.recorder.start()
                    path = self.recorder._path
                    if valid:
                        with wave.open(str(path), "wb") as wav:
                            wav.setnchannels(1)
                            wav.setsampwidth(2)
                            wav.setframerate(16000)
                            wav.writeframes(b"\0" * 3200)
                        self.assertEqual(self.recorder.stop_to_wav(), path)
                        path.unlink()
                    else:
                        path.write_bytes(b"\0" * 3200)
                        with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                            self.recorder.stop_to_wav()
                        self.assertFalse(path.exists())

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

    def test_late_exit_is_reported_once_and_closes_file_and_stderr(self):
        proc = FakeProc(alive=True, hang_first=True, stderr="PipeWire disconnected")
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        path.write_bytes(b"partial" * 100)
        proc.alive = False
        self.assertEqual(self.recorder.take_failure(), "PipeWire disconnected")
        self.assertIsNone(self.recorder.take_failure())
        self.assertFalse(path.exists())
        self.assertIsNone(self.recorder._proc)
        proc.stderr.close.assert_called_once()

    def test_direct_restart_reports_and_cleans_dead_take_before_new_spawn(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        proc.alive = False
        healthy = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(healthy):
            with self.assertRaisesRegex(RuntimeError, "unexpectedly"):
                self.recorder.start()
            self.assertFalse(path.exists())
            self.recorder.start()
        self.assertTrue(self.recorder.recording)

    def test_stop_rejects_partial_wav_from_dead_recorder(self):
        proc = FakeProc(alive=True, hang_first=True, stderr="device removed")
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        path.write_bytes(b"partial" * 100)
        proc.alive = False
        with self.assertRaisesRegex(RuntimeError, "device removed"):
            self.recorder.stop_to_wav()
        self.assertFalse(path.exists())

    def test_death_between_probe_and_sigint_does_not_hand_partial_audio_to_asr(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        path.write_bytes(b"partial" * 100)
        def died(sig):
            proc.alive = False
            proc.exit_code = 1
        proc.send_signal = died
        with self.assertRaisesRegex(RuntimeError, "finish"):
            self.recorder.stop_to_wav()
        self.assertFalse(path.exists())
        proc.stderr.close.assert_called()

    def test_stderr_read_does_not_wait_for_inherited_open_pipe(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        os.write(write_fd, b"late failure")
        proc = FakeProc(alive=False)
        proc.stderr = os.fdopen(read_fd, "r")
        self.recorder._proc = proc
        self.assertEqual(self.recorder.take_failure(), "late failure")
        self.assertTrue(proc.stderr.closed)

    def test_unlink_error_retains_cleanup_path_without_dead_process(self):
        proc = FakeProc(alive=False, stderr="lost device")
        self.recorder._proc = proc
        self.recorder._path = mock.Mock()
        self.recorder._path.unlink.side_effect = PermissionError("denied")
        message = self.recorder.take_failure()
        self.assertIn("lost device", message)
        self.assertIn("denied", message)
        pending = self.recorder._path
        self.assertIsNotNone(pending)
        self.assertIsNone(self.recorder._proc)
        proc.stderr.close.assert_called_once()
        pending.unlink.side_effect = None
        self.recorder.cancel()
        self.assertIsNone(self.recorder._path)

    def test_successful_handoff_closes_stderr_and_is_not_reconciled_or_deleted(self):
        proc = FakeProc(alive=True, hang_first=True)
        with self._patch_which(), self._patch_popen(proc):
            self.recorder.start()
        path = self.recorder._path
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_bytes(b"audio" * 100)
        self.assertEqual(self.recorder.stop_to_wav(), path)
        proc.stderr.close.assert_called_once()
        self.assertIsNone(self.recorder.take_failure())
        self.recorder.cancel()
        self.assertTrue(path.exists())

    def test_real_late_exit_after_start_probe_is_reaped_and_cleaned(self):
        popen = subprocess.Popen
        def spawn(cmd, **kwargs):
            code = "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*512); time.sleep(0.15); print('PipeWire vanished',file=sys.stderr); sys.exit(1)"
            return popen([sys.executable, "-c", code, cmd[-1]], **kwargs)
        with self._patch_which(), mock.patch.object(audio.subprocess, "Popen", side_effect=spawn):
            self.recorder.start()
        proc, path = self.recorder._proc, self.recorder._path
        self.assertTrue(self.recorder.recording)
        proc.wait(timeout=2)
        self.assertEqual(self.recorder.take_failure(), "PipeWire vanished")
        self.assertFalse(path.exists())
        self.assertTrue(proc.stderr.closed)


if __name__ == "__main__":
    unittest.main()
