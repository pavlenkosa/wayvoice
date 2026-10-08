"""Recording startup is atomic, even when its timer cannot start."""
import unittest
from unittest import mock

from tests import support
from tests.test_audio import FakeProc
from wayvoice import audio, daemon


class StartRollbackTests(unittest.TestCase):
    def setUp(self):
        support.isolate_environment(self)
        support.isolate_engine(self)
        self.d = daemon.WayVoiceDaemon()
        self.addCleanup(self.d.recorder.cancel)
        self.addCleanup(self.d._cancel_record_timer)
        for target, value in (("load_config", {"max_recording_sec": 120, "notify": False}),
                              ("engine_status", {"state": "ready"}),
                              ("notify", None)):
            patcher = mock.patch.object(daemon, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(self.d, "_model_report", return_value={"supported": False})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = []

    def start(self, timer):
        proc = FakeProc(alive=True, hang_first=True)
        original = self.d.recorder.start
        def capture():
            original()
            self.paths.append(self.d.recorder._path)
        with mock.patch.object(audio.shutil, "which", return_value="pw-record"), \
             mock.patch.object(audio.subprocess, "Popen", return_value=proc), \
             mock.patch.object(self.d.recorder, "start", side_effect=capture), \
             mock.patch.object(daemon.threading, "Timer", timer):
            reply = self.d.start_recording()
        return reply, proc

    def assert_rolled_back(self, reply, proc):
        self.assertFalse(reply["ok"])
        self.assertIn("timer unavailable", reply["error"])
        self.assertFalse(self.d.recorder.recording)
        self.assertTrue(proc.terminated)
        self.assertFalse(self.paths[-1].exists())
        self.assertIsNone(self.d._record_timer)
        self.assertEqual(self.d._record_started, 0.0)

    def test_timer_start_failure_cleans_take_and_allows_retry(self):
        timer = mock.Mock()
        timer.start.side_effect = RuntimeError("timer unavailable")
        reply, proc = self.start(mock.Mock(return_value=timer))
        self.assert_rolled_back(reply, proc)
        timer.cancel.assert_called_once()
        reply, proc = self.start(mock.Mock(return_value=mock.Mock()))
        self.assertTrue(reply["ok"])
        self.assertTrue(self.d.recorder.recording)
        self.assertFalse(proc.terminated)

    def test_timer_constructor_failure_cleans_take(self):
        reply, proc = self.start(mock.Mock(side_effect=RuntimeError("timer unavailable")))
        self.assert_rolled_back(reply, proc)

    def test_cleanup_error_preserves_startup_reason_and_resets_state(self):
        timer = mock.Mock()
        timer.start.side_effect = RuntimeError("timer unavailable")
        original = self.d.recorder.cancel
        def cancel():
            original()
            raise OSError("cleanup failed")
        with mock.patch.object(self.d.recorder, "cancel", side_effect=cancel):
            reply, proc = self.start(mock.Mock(return_value=timer))
        self.assert_rolled_back(reply, proc)
        self.assertIn("cleanup failed", reply["error"])
        self.assertEqual(self.d.last_error, reply["error"])

    def test_cancel_denied_reports_failure_keeps_owner_and_allows_retry(self):
        reply, proc = self.start(mock.Mock(return_value=mock.Mock()))
        self.assertTrue(reply["ok"])
        path = self.paths[-1]
        with mock.patch.object(proc, "terminate", side_effect=PermissionError("denied")):
            reply = self.d.cancel()
        self.assertFalse(reply["ok"])
        self.assertIn("denied", self.d.last_error)
        self.assertTrue(self.d.recorder.recording)
        self.assertIs(self.d.recorder._proc, proc)
        self.assertTrue(path.exists())
        self.assertEqual(self.d.cancel(), {"ok": True, "state": "idle"})
        self.assertFalse(path.exists())

    def test_startup_rollback_denied_keeps_process_for_explicit_cancel(self):
        proc = FakeProc(alive=True, hang_first=True)
        timer = mock.Mock()
        timer.start.side_effect = RuntimeError("timer unavailable")
        with mock.patch.object(audio.shutil, "which", return_value="pw-record"), \
             mock.patch.object(audio.subprocess, "Popen", return_value=proc), \
             mock.patch.object(proc, "terminate", side_effect=PermissionError("denied")), \
             mock.patch.object(daemon.threading, "Timer", return_value=timer):
            reply = self.d.start_recording()
        self.assertFalse(reply["ok"])
        self.assertIn("timer unavailable", reply["error"])
        self.assertIn("denied", reply["error"])
        self.assertIs(self.d.recorder._proc, proc)
        path = self.d.recorder._path
        self.assertTrue(path.exists())
        self.assertTrue(self.d.cancel()["ok"])
        self.assertFalse(path.exists())
