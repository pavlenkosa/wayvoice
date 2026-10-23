"""Worker response bounds must also retain ownership before allowing fallback."""
import os
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from wayvoice.runtime import worker_client
from wayvoice.runtime.errors import WorkerUnavailable, TranscriptionCancelled
from tests import test_worker_recovery as helpers


class ControlResponseTests(unittest.TestCase):
    def test_control_request_has_one_deadline_for_connect_send_and_receive(self):
        now = [0.0]
        sock = mock.Mock()

        def consume(_value):
            now[0] += .06

        sock.connect.side_effect = consume
        sock.sendall.side_effect = consume
        with mock.patch.object(worker_client.socket, 'socket', return_value=sock), \
             mock.patch.object(worker_client.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError):
                worker_client._worker_call({'cmd': 'ping'}, .1)
        sock.recv.assert_not_called()
        sock.close.assert_called_once()

    def test_continuous_control_response_cannot_renew_the_deadline(self):
        server, client = socket.socketpair()
        stop = threading.Event()

        def trickle():
            try:
                server.recv(4096)
                while not stop.wait(.01):
                    server.sendall(b' ')
            except OSError:
                pass

        sender = threading.Thread(target=trickle)
        sender.start()
        # The socket is already connected; keep the real timeout/recv behaviour.
        proxy = mock.Mock(wraps=client)
        proxy.connect.return_value = None
        began = time.monotonic()
        try:
            with mock.patch.object(worker_client.socket, 'socket', return_value=proxy):
                with self.assertRaises(TimeoutError):
                    worker_client._worker_call({'cmd': 'ping'}, .1)
            self.assertLess(time.monotonic() - began, 1)
        finally:
            stop.set()
            client.close()
            server.close()
            sender.join(timeout=1)
        self.assertFalse(sender.is_alive())

    def test_oversized_control_response_is_rejected_even_with_newline(self):
        sock = mock.Mock()
        sock.recv.return_value = b' ' * 128 + b'{}\n'
        with mock.patch.object(worker_client.socket, 'socket', return_value=sock), \
             mock.patch.object(worker_client, 'MAX_WORKER_RESPONSE_BYTES', 128):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                worker_client._worker_call({'cmd': 'ping'}, 1)
        sock.close.assert_called_once()


class AddressedResponseTests(unittest.TestCase):
    setUp = helpers.WorkerRecoveryTests.setUp
    worker = helpers.WorkerRecoveryTests.worker

    def test_oversized_response_retires_active_decode_before_fallback(self):
        proc = self.worker('flood-running')
        with mock.patch.object(worker_client, 'MAX_WORKER_RESPONSE_BYTES', 512):
            with self.assertRaisesRegex(WorkerUnavailable, 'size limit'):
                worker_client._worker_request({'cmd': 'transcribe', 'request_id': 'take'}, 'take', 2, None)
        proc.wait(timeout=2)
        self.assertFalse(worker_client._pid_alive(proc.pid))

    def test_failed_retirement_blocks_fallback_and_retains_owner(self):
        proc = self.worker('flood-running')
        event = threading.Event()
        with mock.patch.object(worker_client, 'MAX_WORKER_RESPONSE_BYTES', 512), \
             mock.patch.object(worker_client, 'stop_worker', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'fallback was blocked') as raised:
                worker_client._worker_request({'cmd': 'transcribe', 'request_id': 'take'}, 'take', 2, event)
        self.assertNotIsInstance(raised.exception, WorkerUnavailable)
        self.assertIsNone(proc.poll())
        self.assertIn(event, worker_client._unresolved_worker_jobs)

    def test_cancellation_during_oversized_response_recovery_remains_cancellation(self):
        sock = mock.Mock()
        sock.getsockopt.return_value = struct.pack('3i', os.getpid(), os.getuid(), os.getgid())
        sock.recv.return_value = b'x' * 513
        event = threading.Event()

        def recovered(*args, **kwargs):
            event.set()
            return True

        health = {'ok': True, 'pid': os.getpid(), 'request_id': 'take',
                  'state': 'unknown', 'request_completion': True}
        with mock.patch.object(worker_client.socket, 'socket', return_value=sock), \
             mock.patch.object(worker_client, '_worker_call', return_value=health), \
             mock.patch.object(worker_client, 'MAX_WORKER_RESPONSE_BYTES', 512), \
             mock.patch.object(worker_client, '_recover_lost_worker_request', side_effect=recovered):
            with self.assertRaises(TranscriptionCancelled):
                worker_client._worker_request({'cmd': 'transcribe', 'request_id': 'take'}, 'take', 2, event)
        self.assertNotIn(event, worker_client._worker_jobs)
        sock.close.assert_called_once()
