"""Addressed warm-up owns cancellation through queue, load and recovery."""
import threading
import time
import unittest
from unittest import mock

from tests import test_worker_recovery as helpers
from wayvoice import engine, fw_worker, __version__


class WarmRecoveryTests(unittest.TestCase):
    setUp = helpers.WorkerRecoveryTests.setUp
    worker = helpers.WorkerRecoveryTests.worker

    def warm(self, event=None, timeout=2):
        with mock.patch.object(engine, 'ensure_worker', return_value=True):
            return engine.warm_worker({}, timeout=timeout, cancel_event=event)

    def test_hung_load_cancel_retires_worker_and_next_warm_succeeds(self):
        proc = self.worker('hang')
        event = threading.Event()
        timer = threading.Timer(0.06, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(engine.TranscriptionCancelled):
            self.warm(event)
        proc.wait(timeout=2)
        self.assertNotIn(event, engine._worker_jobs)
        self.worker('instant')
        self.assertTrue(self.warm())

    def test_hung_load_timeout_retires_worker_and_returns_false(self):
        proc = self.worker('hang')
        began = time.monotonic()
        self.assertFalse(self.warm(timeout=0.05))
        self.assertLess(time.monotonic() - began, 2)
        proc.wait(timeout=2)

    def test_cooperative_timeout_preserves_worker(self):
        proc = self.worker('cooperate')
        self.assertFalse(self.warm(timeout=0.05))
        self.assertIsNone(proc.poll())

    def test_successful_load_preserves_worker(self):
        proc = self.worker('instant')
        self.assertTrue(self.warm())
        self.assertIsNone(proc.poll())

    def test_shutdown_during_load_retains_owned_request(self):
        proc = self.worker('hang')
        event = threading.Event()
        errors = []
        def warm():
            try:
                self.warm(event)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=warm)
        thread.start()
        deadline = time.monotonic() + 1
        while event not in engine._worker_jobs and time.monotonic() < deadline:
            time.sleep(0.005)
        active = engine.cancel_jobs(event)
        self.assertTrue(active)
        engine.stop_jobs(event, deadline=time.monotonic() + 2, active_worker=active)
        thread.join(timeout=2)
        proc.wait(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], engine.TranscriptionCancelled)
        self.assertNotIn(event, engine._worker_jobs)

    def test_cancel_during_transport_failure_remains_explicit(self):
        for error in (engine.WorkerUnavailable('worker stopped'), OSError('gone')):
            with self.subTest(error=error):
                event = threading.Event()
                def unavailable(*args):
                    event.set()
                    raise error
                with mock.patch.object(engine, 'ensure_worker', return_value=True), \
                     mock.patch.object(engine, '_worker_request', side_effect=unavailable):
                    with self.assertRaises(engine.TranscriptionCancelled):
                        engine.warm_worker({}, cancel_event=event)
                self.assertNotIn(event, engine._worker_jobs)

    def test_previous_same_version_worker_without_addressed_warm_is_replaced(self):
        reply = {'version': __version__, 'request_status': True}
        self.assertFalse(engine._worker_settings_match(reply, {}))


class AddressedWarmDispatchTests(unittest.TestCase):
    def test_cancelled_queued_warm_never_loads_foreign_busy_worker(self):
        state = fw_worker._WorkerState()
        state.serial.acquire()
        self.addCleanup(state.serial.release)
        replies = []
        with mock.patch.object(fw_worker, 'handle_request') as load:
            thread = threading.Thread(target=lambda: replies.append(fw_worker._dispatch(
                {'cmd': 'warm', 'request_id': 'queued-warm'}, None, None, state)))
            thread.start()
            deadline = time.monotonic() + 1
            while state.request_status('queued-warm') == 'finished' and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(state.request_status('queued-warm'), 'queued')
            state.cancel('queued-warm')
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(replies[0]['cancelled'])
            load.assert_not_called()
        self.assertEqual(state.request_status('queued-warm'), 'finished')

    def test_cancel_during_native_load_discards_success_and_releases_owner(self):
        state = fw_worker._WorkerState()
        def native_load(*args):
            self.assertEqual(state.request_status('load'), 'running')
            state.cancel('load')
            return {'ok': True, 'warm': True}
        with mock.patch.object(fw_worker, 'handle_request', side_effect=native_load):
            reply = fw_worker._dispatch({'cmd': 'warm', 'request_id': 'load'}, None, None, state)
        self.assertTrue(reply['cancelled'])
        self.assertEqual(reply['request_id'], 'load')
        self.assertEqual(state.request_status('load'), 'finished')
        self.assertFalse(state.busy())
