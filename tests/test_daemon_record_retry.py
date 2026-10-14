"""Failed recorder shutdown retains ownership and schedules one bounded retry."""

import unittest
from unittest import mock

from tests import test_daemon_take_config as take_tests
from wayvoice import daemon as daemon_mod


class RecorderRetryTests(unittest.TestCase):
    setUp = take_tests.TakeConfigurationTests.setUp

    def test_failed_stop_preserves_elapsed_and_retries_same_take(self):
        self.daemon.start_recording()
        started = self.daemon._record_started
        generation = self.daemon._record_generation
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.recorder.stop_to_wav.side_effect = RuntimeError("signal failed")
            self.assertFalse(self.daemon.stop_recording()["ok"])
        self.assertEqual(self.daemon._record_started, started)
        self.assertEqual(self.daemon._record_generation, generation)
        self.assertIsNotNone(self.daemon._record_timer)
        self.assertGreater(timer.call_args.args[0], 0)
        self.assertLessEqual(timer.call_args.args[0], 1.0)
        callback = timer.call_args.args[1]
        with mock.patch.object(self.daemon, "stop_recording", return_value={"ok": True}) as stop:
            callback(*timer.call_args.kwargs["args"])
        stop.assert_called_once_with(expected_generation=generation)

    def test_stop_retry_preserves_explicit_copy_delivery(self):
        self.daemon.start_recording()
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.recorder.stop_to_wav.side_effect = RuntimeError("signal failed")
            self.daemon.stop_recording(delivery_mode="copy")
        callback, args = timer.call_args.args[1], timer.call_args.kwargs["args"]
        self.recorder.stop_to_wav.side_effect = lambda: self.wav
        with mock.patch.object(daemon_mod.threading, "Thread") as thread:
            callback(*args)
        self.assertEqual(thread.call_args.kwargs["kwargs"]["delivery_mode"], "copy")

    def test_failed_cancel_retry_never_transcribes(self):
        self.daemon.start_recording()
        started = self.daemon._record_started
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.recorder.cancel.side_effect = RuntimeError("signal failed")
            self.assertFalse(self.daemon.cancel()["ok"])
        self.assertEqual(self.daemon._record_started, started)
        callback = timer.call_args.args[1]
        self.recorder.cancel.side_effect = lambda: setattr(self.recorder, "recording", False)
        with mock.patch.object(self.daemon, "stop_recording") as stop:
            callback(*timer.call_args.kwargs["args"])
        stop.assert_not_called()
        self.assertFalse(self.recorder.recording)
        self.assertIsNone(self.daemon._record_timer)
        self.assertIsNone(self.daemon._record_config)

    def test_old_stop_retry_honors_subsequent_cancel_intent(self):
        self.daemon.start_recording()
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.recorder.stop_to_wav.side_effect = RuntimeError("signal failed")
            self.daemon.stop_recording()
            callback, args = timer.call_args.args[1], timer.call_args.kwargs["args"]
            self.recorder.cancel.side_effect = RuntimeError("signal failed")
            self.daemon.cancel()
            self.recorder.cancel.reset_mock()
            callback(*args)
        self.recorder.cancel.assert_called_once()
        self.recorder.stop_to_wav.assert_called_once()

    def test_stale_retry_cannot_stop_next_recording(self):
        self.daemon.start_recording()
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.recorder.cancel.side_effect = RuntimeError("signal failed")
            self.daemon.cancel()
        callback, args = timer.call_args.args[1], timer.call_args.kwargs["args"]
        self.recorder.cancel.side_effect = lambda: setattr(self.recorder, "recording", False)
        self.daemon.cancel()
        self.daemon.start_recording()
        self.recorder.cancel.reset_mock()
        callback(*args)
        self.recorder.cancel.assert_not_called()
        self.assertTrue(self.recorder.recording)
        self.daemon.cancel()

    def test_shutdown_prevents_retry_work(self):
        self.daemon.start_recording()
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.recorder.cancel.side_effect = RuntimeError("signal failed")
            self.daemon.cancel()
        callback, args = timer.call_args.args[1], timer.call_args.kwargs["args"]
        self.daemon.request_shutdown()
        self.recorder.cancel.reset_mock()
        callback(*args)
        self.recorder.cancel.assert_not_called()

    def test_repeated_failure_replaces_retry_instead_of_accumulating_timers(self):
        self.daemon.start_recording()
        self.recorder.cancel.side_effect = RuntimeError("signal failed")
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            first = mock.Mock()
            second = mock.Mock()
            timer.side_effect = [first, second]
            self.daemon.cancel()
            callback, args = timer.call_args.args[1], timer.call_args.kwargs["args"]
            callback(*args)
        first.cancel.assert_called_once()
        second.start.assert_called_once()
        self.assertIs(self.daemon._record_timer, second)

    def test_startup_rollback_retains_live_take_until_cancel_retry_succeeds(self):
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            initial, retry = mock.Mock(), mock.Mock()
            initial.start.side_effect = RuntimeError("cannot start timer")
            timer.side_effect = [initial, retry]
            self.recorder.cancel.side_effect = RuntimeError("signal failed")
            reply = self.daemon.start_recording()
        self.assertFalse(reply["ok"])
        self.assertIn("cannot start timer", reply["error"])
        self.assertIn("signal failed", reply["error"])
        self.assertEqual(self.daemon._record_config["model"], "small")
        self.assertGreater(self.daemon._record_started, 0)
        self.assertIs(self.daemon._record_timer, retry)
        self.assertTrue(self.daemon._record_cancel_requested)
        self.recorder.cancel.side_effect = lambda: setattr(self.recorder, "recording", False)
        with mock.patch.object(daemon_mod, "transcribe") as transcribe:
            self.daemon.stop_recording()
        transcribe.assert_not_called()
        self.recorder.stop_to_wav.assert_not_called()
        self.assertIsNone(self.daemon._record_config)
        retry.cancel.assert_called_once()

    def test_partial_recorder_start_failure_is_retried_as_cancellation(self):
        def opened_then_failed(**kwargs):
            self.recorder.recording = True
            raise RuntimeError("device opened but setup failed")

        self.recorder.start.side_effect = opened_then_failed
        self.recorder.cancel.side_effect = RuntimeError("signal failed")
        with mock.patch.object(daemon_mod.threading, "Timer") as timer:
            self.assertFalse(self.daemon.start_recording()["ok"])
        self.assertEqual(self.daemon._record_config["model"], "small")
        self.assertGreater(self.daemon._record_started, 0)
        self.assertTrue(self.daemon._record_cancel_requested)
        self.assertEqual(timer.call_args.kwargs["args"],
                         (self.daemon._record_generation, True))
