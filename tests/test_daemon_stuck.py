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

from wayvoice import audio, daemon as daemon_mod
from wayvoice.audio import AudioRecorder
from wayvoice.daemon import WayVoiceDaemon


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
        while daemon._prepare_thread is not None and time.monotonic() < deadline:
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
    test.patch("wayvoice.engine.warm_worker", return_value=True)
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
        self.recorder._finished = path
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
            recorder._finished = path
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
            ("wayvoice.engine.warm_worker", True),
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
        d = self._work(PermissionError(13, "Permission denied"))
        self.assertFalse(d._transcribe_cancel.is_set(),
                         "the next dictation would start already cancelled")

    def test_the_text_was_still_delivered(self):
        # The unlink is cleanup; failing it must not cost the user their transcript.
        d = self._work(OSError(5, "Input/output error"))
        self.assertEqual(d.last_text, "текст")
        self.assertEqual(d.last_error, "")


class RecordingLimitTests(DaemonCase):
    """A limit that cannot be read is refused before the microphone opens."""

    def setUp(self):
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
        _d, recorder, reply = self._start()
        self.assertFalse(reply["ok"], reply)
        self.assertFalse(recorder.start.called, "the recorder was started anyway")
        self.assertFalse(recorder.recording)

    def test_no_timer_is_left_running(self):
        d, _recorder, _reply = self._start()
        self.assertIsNone(d._record_timer)
        self.assertEqual(d._record_started, 0.0)

    def test_the_reason_is_a_setting_problem_not_a_crash(self):
        _d, _recorder, reply = self._start()
        self.assertIn("max_recording_sec", str(reply.get("error") or ""),
                      "the user is told which setting to fix")

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


class StaleRecordingSweeperTests(DaemonCase):
    """The sweeper is what covers a file whose owner died before cleaning up."""

    def test_it_still_removes_recordings_nobody_owns(self):
        # The sweeper is what covers a file whose owner died before cleaning up: it
        # globs the system temporary directory, so the files have to be there.
        made = []
        old = Path(tempfile.gettempdir()) / "wayvoice-sweeper-old.wav"
        fresh = Path(tempfile.gettempdir()) / "wayvoice-sweeper-fresh.wav"
        made += [old, fresh]
        for path, payload in ((old, 300), (fresh, 300)):
            path.write_bytes(b"RIFF" + b"\x00" * payload)
        ancient = 60 * 60 * 24
        now = time.time()
        os.utime(old, (now - ancient, now - ancient))
        os.utime(fresh, (now, now))
        try:
            daemon_mod._sweep_stale_recordings(max_age=3600)
            self.assertFalse(old.exists(), "a stale recording survived the sweep")
            self.assertTrue(fresh.exists(), "a recording in progress was swept")
        finally:
            for path in made:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
