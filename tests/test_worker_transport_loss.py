"""Lost sockets cannot release ownership of a still-running native request."""
import threading
import socket
import time
import unittest
from unittest import mock

from tests import test_worker_recovery as helpers
from wayvoice import engine


class TransportLossTests(unittest.TestCase):
    setUp = helpers.WorkerRecoveryTests.setUp
    worker = helpers.WorkerRecoveryTests.worker

    def request(self, command, event=None, timeout=2):
        if command == 'warm':
            with mock.patch.object(engine, 'ensure_worker', return_value=True):
                return engine.warm_worker({}, timeout=timeout, cancel_event=event)
        return engine._worker_request({'cmd': command, 'request_id': 'take'}, 'take', timeout, event)

    def root_runtime(self):
        root = self.path.parent / 'runtime'
        (root / 'bin').mkdir(parents=True, exist_ok=True)
        (root / 'bin' / 'python').touch()
        return root

    def test_cancel_socket_loss_retires_decode_and_warm_before_owner_unwinds(self):
        for command in ('transcribe', 'warm'):
            with self.subTest(command=command):
                proc = self.worker('drop-on-cancel')
                event = threading.Event()
                timer = threading.Timer(0.06, event.set)
                timer.start()
                self.addCleanup(timer.cancel)
                with self.assertRaises(engine.TranscriptionCancelled):
                    self.request(command, event)
                self.assertFalse(engine._pid_alive(proc.pid), 'native job outlived its owner')
                proc.wait(timeout=2)
                self.assertNotIn(event, engine._worker_jobs)
                self.worker('instant')
                self.assertTrue(self.request(command))

    def test_timeout_socket_loss_retires_decode_and_warm(self):
        for command in ('transcribe', 'warm'):
            with self.subTest(command=command):
                proc = self.worker('drop-on-cancel')
                began = time.monotonic()
                if command == 'warm':
                    self.assertFalse(self.request(command, timeout=0.05))
                else:
                    with self.assertRaises(engine.TranscriptionTimeout):
                        self.request(command, timeout=0.05)
                self.assertLess(time.monotonic() - began, 2)
                self.assertFalse(engine._pid_alive(proc.pid))
                proc.wait(timeout=2)

    def test_uncancelled_socket_loss_retires_before_fallback(self):
        for command in ('transcribe', 'warm'):
            with self.subTest(command=command):
                proc = self.worker('drop-running')
                if command == 'warm':
                    self.assertFalse(self.request(command))
                else:
                    with self.assertRaises(engine.WorkerUnavailable):
                        self.request(command)
                self.assertFalse(engine._pid_alive(proc.pid))
                proc.wait(timeout=2)

    def test_finished_worker_survives_lost_response(self):
        for command in ('transcribe', 'warm'):
            with self.subTest(command=command):
                proc = self.worker('drop-finished')
                if command == 'warm':
                    self.assertFalse(self.request(command))
                else:
                    with self.assertRaises(engine.WorkerUnavailable):
                        self.request(command)
                self.assertIsNone(proc.poll())
                proc.kill()
                proc.wait(timeout=2)

    def test_loss_before_registration_does_not_claim_request_finished(self):
        proc = self.worker('drop-delayed')
        with self.assertRaises(engine.WorkerUnavailable):
            self.request('transcribe')
        self.assertFalse(engine._pid_alive(proc.pid))
        proc.wait(timeout=2)

    def test_sendall_error_after_acceptance_retires_the_accepted_request(self):
        real_socket = socket.socket
        def factory(*args, **kwargs):
            actual = real_socket(*args, **kwargs)
            wrapper = mock.Mock(wraps=actual)
            def send(data):
                actual.sendall(data)
                if b'"cmd": "transcribe"' in data or b'"cmd": "warm"' in data:
                    raise OSError('send failed after acceptance')
            wrapper.sendall.side_effect = send
            return wrapper
        for command in ('transcribe', 'warm'):
            with self.subTest(command=command):
                proc = self.worker('hang')
                with mock.patch.object(engine.socket, 'socket', side_effect=factory):
                    if command == 'warm':
                        self.assertFalse(self.request(command))
                    else:
                        with self.assertRaises(engine.WorkerUnavailable):
                            self.request(command)
                self.assertFalse(engine._pid_alive(proc.pid))
                proc.wait(timeout=2)

    def test_completion_history_distinguishes_unseen_and_finished_requests(self):
        from wayvoice import fw_worker
        state = fw_worker._WorkerState()
        self.assertEqual(state.request_status('new'), 'unknown')
        state.begin('new')
        self.assertEqual(state.request_status('new'), 'queued')
        state.finish('new')
        self.assertEqual(state.request_status('new'), 'finished')
        for number in range(300):
            state.finish(str(number))
        self.assertLessEqual(len(state._finished), 256)

    def test_failed_retirement_blocks_fallback_and_retains_owner_for_shutdown(self):
        proc = self.worker('drop-running')
        event = threading.Event()
        with mock.patch.object(engine, 'stop_worker', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'fallback was blocked') as raised:
                self.request('transcribe', event)
            self.assertNotIsInstance(raised.exception, engine.WorkerUnavailable)
            self.assertTrue(engine._pid_alive(proc.pid))
            self.assertNotIn(event, engine._worker_jobs)
            self.assertIn(event, engine._unresolved_worker_jobs)
            active = engine.cancel_jobs(event)
            self.assertTrue(active)
            event.clear()
            with self.assertRaisesRegex(RuntimeError, 'automatic retry was blocked'):
                engine.ensure_worker({}, event)
            runtime = self.root_runtime()
            with mock.patch.object(engine, 'faster_runtime', return_value=runtime), \
                 mock.patch.object(engine, '_run_cancelable') as oneshot:
                with self.assertRaisesRegex(RuntimeError, 'automatic retry was blocked'):
                    engine._transcribe_faster(runtime / 'take.wav', {'engine_worker': False}, event)
            oneshot.assert_not_called()
        engine.stop_jobs(event, deadline=time.monotonic() + 2, active_worker=active)
        proc.wait(timeout=2)
        self.assertNotIn(event, engine._unresolved_worker_jobs)

    def test_failed_retirement_preserves_cancel_and_pending_warm_owner(self):
        proc = self.worker('drop-on-cancel')
        event = threading.Event()
        timer = threading.Timer(0.06, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(engine, 'stop_worker', return_value=False):
            with self.assertRaises(engine.TranscriptionCancelled):
                self.request('warm', event)
        self.assertTrue(engine._pid_alive(proc.pid))
        self.assertIn(event, engine._unresolved_worker_jobs)
        engine.stop_jobs(event, deadline=time.monotonic() + 2)
        proc.wait(timeout=2)
        self.assertNotIn(event, engine._unresolved_worker_jobs)
