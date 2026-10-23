"""Three ways a dictation could leave the daemon stuck, and one file unlinked too early.

All three were found by reading the code rather than by a failing test, which is
the point of this file: each of them needs either an unusual configuration value or
a shutdown at an unlucky moment, and neither happens while a suite runs.

1. Shutdown deleted the WAV before the recognizer was told to stop. ``cancel()``
   unlinked the file that ``stop_to_wav()`` had handed to the transcription thread,
   and only the line after it set the cancel flag - so ``quit``, SIGTERM or SIGHUP
   during a dictation produced ``FileNotFoundError`` in the worker and a raw errno
   in ``last_error``.
2. ``busy`` survived an unlink that raised. ``missing_ok=True`` covers exactly one
   exception, the state reset sat *after* the unlink, and a ``PermissionError``
   there left the daemon refusing every hot-key press until it was restarted.
3. A non-numeric ``max_recording_sec`` opened the microphone with no limit at all:
   the recorder was started, and only then did ``int()`` raise - leaving pw-record
   holding the device until the next keypress, which then transcribed minutes of
   room noise.
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import daemon as daemon_mod
from wayvoice.audio import AudioRecorder
from wayvoice.daemon import WayVoiceDaemon

# Import support module for test isolation
from tests import support


class DaemonCase(unittest.TestCase):
    """Base that owns the patches, so a test body is only about the daemon."""

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def settle(self, daemon, timeout: float = 10.0) -> None:
        """Wait until the preparation thread the constructor started has ended."""
        deadline = time.monotonic() + timeout
        while daemon.preparation.thread is not None and time.monotonic() < deadline:
            time.sleep(0.02)

CONFIG = {
    "model": "small",
    "engine_worker": True,
    "notify": False,
    "ui_language": "en",
    "max_recording_sec": 120,
}


def _daemon(test, recorder=None):
    """A daemon built the way the application builds it, minus the network.

    ``WayVoiceDaemon()`` does the real construction - locks, timers, the engine
    hooks - because a hand-built ``__new__`` object misses state that the very
    methods under test read, and a test that has to add that state by hand is
    testing the harness rather than the daemon.
    """
    test.patch("wayvoice.daemon.engine_from_config", return_value=mock.Mock())
    test.patch("wayvoice.runtime.worker_client.warm_worker", return_value=True)
    d = WayVoiceDaemon()
    d.recorder = recorder if recorder is not None else mock.Mock(recording=False)
    d._prepare_engine = mock.Mock()
    return d


class HandedOutFileTests(DaemonCase):
    """A file that was handed to a caller is the caller's to delete.

    ``cancel()`` used to unlink it as well, on the theory that the process might die
    before the caller's cleanup ran. It runs first in the shutdown path, so on every
    ``quit`` during a dictation the recognizer was reading a file that had just been
    deleted underneath it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.recorder = AudioRecorder()
        self.recorder._proc = None
        self.recorder._path = None

    def _handed_out(self) -> Path:
        path = Path(self.tmp.name) / "take.wav"
        path.write_bytes(b"RIFF" + b"\x00" * 200)
        return path

    def test_cancel_leaves_a_file_it_handed_out(self):
        path = self._handed_out()
        self.recorder.cancel()
        self.assertTrue(path.exists(),
                        "the file the transcription thread is reading was deleted")

    def test_cancel_still_removes_the_recording_in_progress(self):
        # The other half: a recording nobody ever took is ours, and leaving it would
        # put a half-written WAV in /tmp for every abandoned dictation.
        path = Path(self.tmp.name) / "in-progress.wav"
        path.write_bytes(b"RIFF")
        self.recorder._path = path
        self.recorder.cancel()
        self.assertFalse(path.exists())

    def test_the_recognizer_is_told_to_stop_before_the_recorder_is_cancelled(self):
        # Order matters as much as ownership: the flag is what makes the worker give
        # up, and it used to be set after the file it was reading had been deleted.
        order = []
        recorder = mock.Mock(recording=True)
        recorder.cancel.side_effect = lambda: order.append("cancel")
        self.patch("wayvoice.daemon.load_config", return_value=dict(CONFIG))
        d = _daemon(self, recorder)

        real_event = d._transcribe_cancel

        class Watched(threading.Event):
            def set(self):
                order.append("flag")
                real_event.set()

        d._transcribe_cancel = Watched()
        d._stop_work()
        self.assertEqual(order, ["flag", "cancel"],
                         f"the recognizer was not told first: {order}")

    def test_the_file_survives_the_shutdown_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wayvoice-take.wav"
            path.write_bytes(b"RIFF" + b"\x00" * 300)
            recorder = AudioRecorder()
            recorder._proc = None
            recorder._path = None
            d = _daemon(self, recorder)
            self.patch("wayvoice.daemon.load_config", return_value=dict(CONFIG))
            d._stop_work()
            self.assertTrue(path.exists(),
                            "shutdown deleted the audio the recognizer was reading")


class BusySurvivesAFailedUnlinkTests(DaemonCase):
    """The state reset cannot be stranded by the cleanup that follows it."""

    def setUp(self):
        for target, value in (
            ("wayvoice.daemon.load_config", dict(CONFIG)),
            ("wayvoice.runtime.worker_client.warm_worker", True),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("wayvoice.daemon.notify")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _work(self, unlink_error):
        d = _daemon(self)
        d.busy = True
        wav = Path(tempfile.gettempdir()) / "wayvoice-test-busy.wav"
        with mock.patch("wayvoice.daemon.transcribe", return_value="текст"), \
             mock.patch("wayvoice.daemon.inject", return_value=mock.Mock(
                 pasted=True, warning="")), \
             mock.patch.object(daemon_mod.Path, "unlink",
                               side_effect=unlink_error):
            d._transcribe_worker(wav)
        return d

    def test_busy_is_cleared_even_when_the_unlink_fails(self):
        d = self._work(PermissionError(13, "Permission denied"))
        self.assertFalse(d.busy, "the daemon is still busy and refuses every hot key")
        self.assertEqual(d._busy_started, 0.0)

    def test_the_cancel_flag_is_cleared_too(self):
        d = _daemon(self)
        d.busy = True

        def cancel_transcription(*args):
            d._transcribe_cancel.set()
            raise daemon_mod.TranscriptionCancelled("cancelled during recognition")

        wav = Path(tempfile.gettempdir()) / "wayvoice-test-busy.wav"
        with mock.patch.object(daemon_mod, "transcribe", side_effect=cancel_transcription), \
                mock.patch.object(daemon_mod.Path, "unlink", side_effect=PermissionError("denied")):
            d._transcribe_worker(wav)
        self.assertFalse(d.busy)
        self.assertFalse(d._transcribe_cancel.is_set(),
                         "the next dictation would start already cancelled")
        self.assertEqual(d._pending_asr_cleanup, {wav})

    def test_the_text_was_still_delivered(self):
        # The unlink is cleanup; failing it must not cost the user their transcript.
        d = self._work(OSError(5, "Input/output error"))
        self.assertEqual(d.last_text, "текст")
        self.assertEqual(d.last_error, "")

    def test_failed_cleanup_is_retained_then_retried_on_next_start(self):
        d = self._work(PermissionError(13, "denied"))
        wav = Path(tempfile.gettempdir()) / "wayvoice-test-busy.wav"
        self.assertEqual(d._pending_asr_cleanup, {wav})
        # Refuse before microphone preflight; cleanup must still be attempted.
        d._shutdown.set()
        with mock.patch.object(daemon_mod.Path, "unlink") as unlink:
            self.assertFalse(d.start_recording()["ok"])
        unlink.assert_called_once_with(missing_ok=True)
        self.assertFalse(d._pending_asr_cleanup)

    def test_retry_failure_keeps_owner_and_does_not_delete_active_wav(self):
        d = self._work(PermissionError(13, "denied"))
        active = Path("/tmp/wayvoice-active-asr.wav")
        d._transcribe_wav = active
        with mock.patch.object(daemon_mod.Path, "unlink", side_effect=PermissionError("denied")) as unlink:
            d._retry_asr_cleanup()
        self.assertTrue(d._pending_asr_cleanup)
        self.assertEqual(unlink.call_count, 1)
        self.assertEqual(d._transcribe_wav, active)

    def test_idle_cancel_retries_cleanup(self):
        d = self._work(PermissionError("denied"))
        with mock.patch.object(daemon_mod.Path, "unlink") as unlink:
            self.assertTrue(d.cancel()["ok"])
        self.assertTrue(unlink.called)
        self.assertFalse(d._pending_asr_cleanup)

    def test_shutdown_retries_completed_wav(self):
        d = self._work(PermissionError("denied"))
        with mock.patch.object(daemon_mod.Path, "unlink") as unlink:
            d._stop_work()
        self.assertTrue(unlink.called)
        self.assertFalse(d._pending_asr_cleanup)

    def test_thread_creation_failure_retains_wav_and_original_error(self):
        d = _daemon(self)
        wav = Path("/tmp/wayvoice-handoff.wav")
        d.recorder = mock.Mock(recording=True)
        d.recorder.stop_to_wav.return_value = wav
        with mock.patch.object(d, "_reconcile_recorder", return_value=""), \
             mock.patch.object(daemon_mod.threading, "Thread", side_effect=RuntimeError("thread unavailable")), \
             mock.patch.object(daemon_mod.Path, "unlink", side_effect=PermissionError("denied")):
            result = d.stop_recording()
        self.assertEqual(result["error"], "thread unavailable")
        self.assertFalse(d.busy)
        self.assertEqual(d._pending_asr_cleanup, {wav})


class RecordingLimitTests(DaemonCase):
    """A limit that cannot be read is refused before the microphone opens."""

    def setUp(self):
        # Isolate environment to prevent interference with real config
        support.isolate_environment(self)
        support.isolate_engine(self)
        
        patcher = mock.patch("wayvoice.daemon.load_config", return_value={
            **CONFIG, "max_recording_sec": "two minutes"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("wayvoice.daemon.notify")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _start(self):
        recorder = mock.Mock(recording=False)
        d = _daemon(self, recorder)
        reply = d.start_recording()
        return d, recorder, reply

    def test_a_limit_that_is_not_a_number_does_not_start_anything(self):
        # The regression: the recorder was already running when int() raised, so the
        # microphone stayed open with no timer to close it.
        d, recorder, reply = self._start()
        self.assertFalse(reply["ok"], reply)
        self.assertIn("max_recording_sec", reply["error"])
        recorder.start.assert_not_called()
        self.assertFalse(recorder.recording)
        self.assertIsNone(d._record_timer)
        self.assertEqual(d._record_started, 0.0)

    def test_out_of_range_limit_is_refused_before_microphone_or_timer_start(self):
        for limit in (10 ** 100, "1" + "0" * 100, daemon_mod.MAX_RECORDING_SECONDS + 1):
            with self.subTest(limit=limit), mock.patch.object(
                    daemon_mod, "load_config", return_value={**CONFIG, "max_recording_sec": limit}):
                d, recorder, reply = self._start()
                self.assertFalse(reply["ok"], reply)
                self.assertIn("max_recording_sec", reply["error"])
                recorder.start.assert_not_called()
                self.assertIsNone(d._record_timer)

    def test_maximum_limit_can_arm_a_real_timer(self):
        limit = daemon_mod._recording_limit({"max_recording_sec": daemon_mod.MAX_RECORDING_SECONDS})
        timer = threading.Timer(limit, lambda: None)
        timer.start()
        try:
            self.assertTrue(timer.is_alive())
        finally:
            timer.cancel()
            timer.join(timeout=1)

    def test_the_auto_stop_timer_survives_a_bad_value(self):
        # The same int() runs again on the timer thread, where an exception would
        # kill the thread and the limit would never fire.
        d = _daemon(self)
        with mock.patch("wayvoice.daemon.load_config", return_value={
                **CONFIG, "max_recording_sec": "soon"}), \
             mock.patch.object(d, "stop_recording") as stop:
            d.recorder.recording = True
            d._auto_stop_recording()
        self.assertTrue(stop.called, "the recording was never closed")

    def test_a_good_value_still_works(self):
        support.isolate_environment(self)
        support.isolate_engine(self)
        
        patcher = mock.patch("wayvoice.daemon.load_config", return_value=dict(CONFIG))
        patcher.start()
        self.addCleanup(patcher.stop)
        recorder = mock.Mock(recording=False)
        d = _daemon(self, recorder)
        reply = d.start_recording()
        self.assertTrue(reply["ok"], reply)
        self.addCleanup(d._cancel_record_timer)
        self.assertIsNotNone(d._record_timer)
        recorder.start.assert_called_once()

    def test_config_validation_occurs_before_engine_preflight(self):
        """Regression test: config validation must occur before engine preflight."""
        # Patch engine_status to return "missing" state to simulate engine not ready
        # but keep the malformed max_recording_sec config
        with mock.patch("wayvoice.daemon.engine_status", return_value={"state": "missing", "message": "Faster-Whisper is not prepared"}):
            recorder = mock.Mock(recording=False)
            d = _daemon(self, recorder)
            
            # This should fail with config error, not engine error
            reply = d.start_recording()
            
            # Should fail with config validation error (not engine error)
            self.assertFalse(reply["ok"])
            self.assertIn("max_recording_sec", str(reply.get("error") or ""))
            
            # The recorder.start should never be called
            self.assertFalse(recorder.start.called, "recorder.start was called despite config error")
            
            # Engine preparation should not be triggered
            # We check this by verifying _prepare_engine was not called
            self.assertFalse(d._prepare_engine.called, "_prepare_engine was called despite config error")


class StaleRecordingSweeperTests(DaemonCase):
    """Legacy temporary files have no proof that WayVoice owns their contents."""

    def test_daemon_does_not_sweep_unverified_legacy_wavs(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "wayvoice-user-document.wav"
            old.write_bytes(b"RIFF" + b"\x00" * 300)
            os.utime(old, (0, 0))
            with mock.patch("wayvoice.recordings.tempfile.gettempdir", return_value=directory):
                self.assertEqual(daemon_mod._sweep_stale_recordings(max_age=3600), 0)
            self.assertTrue(old.exists(), "filename and age do not establish ownership")


if __name__ == "__main__":
    unittest.main()
