"""A dictation owns the settings selected when its microphone opened."""

import unittest
from unittest import mock

from tests.support import isolate_environment
from wayvoice import daemon as daemon_mod
from wayvoice.injector import InjectionResult


class TakeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.root = isolate_environment(self)
        self.cfg = {"model": "small", "language": "en", "paste_mode": "copy",
                    "notify": False, "vocabulary": ["original"]}
        for name, kwargs in (
            ("load_config", {"side_effect": lambda: self.cfg}),
            ("engine_status", {"return_value": {"state": "ready"}}),
            ("notify", {}), ("play_cue", {}),
            ("reset_notification_id", {}),
        ):
            patch = mock.patch.object(daemon_mod, name, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)
        timer = mock.patch.object(daemon_mod.threading, "Timer")
        timer.start()
        self.addCleanup(timer.stop)
        self.daemon = daemon_mod.WayVoiceDaemon()
        self.daemon._model_report = mock.Mock(return_value={"supported": False})
        self.recorder = mock.Mock(recording=False)
        self.recorder.take_failure.return_value = None
        self.recorder.start.side_effect = lambda **kwargs: setattr(self.recorder, "recording", True)
        self.recorder.cancel.side_effect = lambda: setattr(self.recorder, "recording", False)
        self.wav = self.root / "take.wav"
        self.wav.write_bytes(b"test")

        def stop():
            self.recorder.recording = False
            return self.wav

        self.recorder.stop_to_wav.side_effect = stop
        self.daemon.recorder = self.recorder

    def test_changes_during_capture_apply_only_to_the_next_take(self):
        self.assertTrue(self.daemon.start_recording()["ok"])
        self.cfg.update(model="medium", language="ru", paste_mode="standard")
        self.cfg["vocabulary"].append("changed")
        with mock.patch.object(daemon_mod.threading, "Thread") as thread:
            self.assertTrue(self.daemon.stop_recording()["ok"])
        kwargs = thread.call_args.kwargs["kwargs"]
        self.assertIsNone(self.daemon._record_config)
        with mock.patch.object(daemon_mod, "transcribe", return_value="hello") as recognize, \
             mock.patch.object(daemon_mod, "inject", return_value=InjectionResult(False)) as deliver:
            self.daemon._transcribe_worker(self.wav, **kwargs)
        captured = recognize.call_args.args[1]
        self.assertEqual(captured["model"], "small")
        self.assertEqual(captured["language"], "en")
        self.assertEqual(captured["vocabulary"], ["original"])
        self.assertEqual(deliver.call_args.args[1]["paste_mode"], "copy")
        self.assertFalse(self.daemon.busy)
        self.assertFalse(self.wav.exists())
        self.assertTrue(self.daemon.start_recording()["ok"])
        self.assertEqual(self.daemon._record_config["model"], "medium")
        self.daemon.cancel()

    def test_cancel_discards_the_capture_snapshot(self):
        self.daemon.start_recording()
        self.assertTrue(self.daemon.cancel()["ok"])
        self.assertIsNone(self.daemon._record_config)

    def test_cancel_cleanup_error_discards_snapshot_when_microphone_is_stopped(self):
        self.daemon.start_recording()

        def failed_cleanup():
            self.recorder.recording = False
            raise OSError("unlink denied")

        self.recorder.cancel.side_effect = failed_cleanup
        self.assertFalse(self.daemon.cancel()["ok"])
        self.assertIsNone(self.daemon._record_config)

    def test_failed_start_discards_the_capture_snapshot(self):
        self.recorder.start.side_effect = RuntimeError("device missing")
        self.assertFalse(self.daemon.start_recording()["ok"])
        self.assertIsNone(self.daemon._record_config)

    def test_lost_recorder_discards_the_capture_snapshot(self):
        self.daemon.start_recording()
        self.recorder.recording = False
        self.recorder.take_failure.return_value = "device disconnected"
        self.daemon._reconcile_recorder()
        self.assertIsNone(self.daemon._record_config)

    def test_stop_failure_discards_snapshot_after_recorder_stops(self):
        self.daemon.start_recording()

        def failed_stop():
            self.recorder.recording = False
            raise RuntimeError("broken WAV")

        self.recorder.stop_to_wav.side_effect = failed_stop
        self.assertFalse(self.daemon.stop_recording()["ok"])
        self.assertIsNone(self.daemon._record_config)

    def test_stop_failure_retains_snapshot_while_recorder_still_owns_take(self):
        self.daemon.start_recording()
        self.recorder.stop_to_wav.side_effect = RuntimeError("cannot signal")
        self.assertFalse(self.daemon.stop_recording()["ok"])
        self.assertEqual(self.daemon._record_config["model"], "small")
        self.daemon.cancel()

    def test_shutdown_discards_the_capture_snapshot(self):
        self.daemon.start_recording()
        with mock.patch.object(daemon_mod, "cancel_jobs", return_value=False), \
             mock.patch.object(daemon_mod, "stop_jobs"):
            self.daemon._stop_work()
        self.assertIsNone(self.daemon._record_config)
