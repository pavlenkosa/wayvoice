import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import audio, daemon as dm
from tests.test_audio import FakeProc
from tests.support import isolate_environment, isolate_engine


class RecorderDeathTests(unittest.TestCase):
    def setUp(self):
        isolate_environment(self)
        isolate_engine(self)
        self.d = dm.WayVoiceDaemon()
        self.addCleanup(self.d._stop_work)
        self.proc = FakeProc(alive=True, hang_first=True, stderr="PipeWire lost microphone")
        with mock.patch.object(audio.shutil, "which", return_value="pw-record"), mock.patch.object(audio.subprocess, "Popen", return_value=self.proc):
            self.d.recorder.start()
        self.path = self.d.recorder._path
        self.path.write_bytes(b"partial audio" * 100)
        self.d._record_started = time.monotonic()
        self.timer = mock.Mock()
        self.d._record_timer = self.timer
        self.proc.alive = False

    def test_status_consumes_failure_cancels_timer_and_reports_reason(self):
        self.assertFalse(self.d.status()["recording"])
        self.assertIn("lost microphone", self.d.last_error)
        self.assertFalse(self.path.exists())
        self.timer.cancel.assert_called_once()
        self.assertIsNone(self.d._record_timer)
        self.assertEqual(self.d._record_started, 0)
        self.d.status()
        self.timer.cancel.assert_called_once()

    def test_stop_and_toggle_refuse_lost_take_without_starting_asr(self):
        for action in (self.d.stop_recording, self.d.toggle):
            # Each subcase supplies a fresh unexpected exit.
            self.d.recorder._proc = self.proc
            self.d.recorder._path = self.path
            self.path.write_bytes(b"partial" * 100)
            with mock.patch.object(dm, "transcribe") as transcribe:
                reply = action()
            self.assertFalse(reply["ok"])
            self.assertIn("lost microphone", reply["error"])
            transcribe.assert_not_called()
            self.assertFalse(self.path.exists())
            self.assertFalse(self.d.busy)

    def test_cancel_and_auto_stop_clean_lost_take(self):
        self.d.cancel()
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.d._record_timer)
        self.d.recorder._proc = self.proc
        self.d.recorder._path = self.path
        self.path.write_bytes(b"partial" * 100)
        self.d._auto_stop_recording()
        self.assertFalse(self.path.exists())

    def test_retry_after_failure_starts_fresh_recording(self):
        reply = self.d.start_recording()
        self.assertFalse(reply["ok"])
        healthy = FakeProc(alive=True, hang_first=True)
        with mock.patch.object(dm, "engine_status", return_value={"state": "ready"}), mock.patch.object(self.d, "_model_report", return_value={"supported": False}), mock.patch.object(dm, "notify"), mock.patch.object(audio.shutil, "which", return_value="pw-record"), mock.patch.object(audio.subprocess, "Popen", return_value=healthy):
            reply = self.d.start_recording()
        self.assertTrue(reply["ok"])
        self.assertTrue(self.d.recorder.recording)
        self.assertFalse(self.path.exists())
        self.assertNotEqual(self.path, self.d.recorder._path)

    def test_status_does_not_wait_for_lifecycle_lock(self):
        entered = threading.Event()
        release = threading.Event()
        def hold():
            with self.d._lock:
                entered.set()
                release.wait(2)
        owner = threading.Thread(target=hold)
        owner.start()
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            self.d.status()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(self.path.exists())
        finally:
            release.set()
            owner.join()
        self.d.status()
        self.assertFalse(self.path.exists())
