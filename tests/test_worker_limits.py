"""Resource bounds are independent of the optional recognition runtime."""
import json
import socket
import threading
import time
import unittest
from unittest import mock

from wayvoice import fw_worker as worker


class WorkerLimitsTests(unittest.TestCase):
    def setUp(self):
        self.state = worker._WorkerState()
        self.cache = worker.ModelCache(lambda *args: object())
        self.config = worker.WorkerConfig(device="cpu")

    def test_operation_limit_keeps_control_requests_available(self):
        self.assertTrue(self.state.operations.acquire(False))
        self.assertTrue(self.state.operations.acquire(False))
        try:
            reply = worker._dispatch({"cmd": "warm", "request_id": "rejected"}, self.cache, self.config, self.state)
            self.assertTrue(reply["busy"])
            self.assertEqual(reply["request_id"], "rejected")
            self.assertEqual(self.state.request_status("rejected"), "unknown")
            self.assertTrue(worker._dispatch({"cmd": "ping"}, self.cache, self.config, self.state)["pong"])
            self.assertEqual(worker._dispatch({"cmd": "request-status", "request_id": "x"}, self.cache, self.config, self.state)["state"], "unknown")
        finally:
            self.state.operations.release()
            self.state.operations.release()

    def test_queue_timeout_releases_operation(self):
        self.state.serial.acquire()
        try:
            reply = worker._dispatch({"cmd": "warm", "request_id": "queued", "queue_timeout_sec": .01}, self.cache, self.config, self.state)
            self.assertTrue(reply["busy"])
            self.assertFalse(self.state.busy())
            self.assertTrue(self.state.operations.acquire(False))
            self.assertTrue(self.state.operations.acquire(False))
        finally:
            self.state.serial.release()

    def test_queued_cancel_releases_operation(self):
        self.state.serial.acquire()
        replies = []
        thread = threading.Thread(target=lambda: replies.append(worker._dispatch({"cmd": "warm", "request_id": "queued"}, self.cache, self.config, self.state)))
        thread.start()
        try:
            deadline = time.monotonic() + 1
            while self.state.request_status("queued") != "queued" and time.monotonic() < deadline:
                time.sleep(.001)
            self.state.cancel("queued")
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(replies[0]["cancelled"])
            self.assertFalse(self.state.busy())
        finally:
            self.state.serial.release()
            thread.join(1)

    def test_histories_bounded_and_expire_without_evicting_active(self):
        now = [0.0]
        state = worker._WorkerState(clock=lambda: now[0])
        event = state.begin("active")
        for n in range(worker._MAX_HISTORY + 10):
            state.cancel(str(n))
            state.finish(str(n))
        self.assertEqual(len(state._early), worker._MAX_HISTORY)
        self.assertEqual(len(state._finished), worker._MAX_HISTORY)
        self.assertIs(state._active["active"], event)
        now[0] = 6
        self.assertEqual(state.request_status("0"), "unknown")
        self.assertFalse(state._finished)
        self.assertFalse(state._early)

    def test_oversized_line_with_newline_rejected_before_json(self):
        server, client = socket.socketpair()
        try:
            client.sendall(b"x" * worker._MAX_REQUEST_BYTES + b"\n")
            with self.assertRaisesRegex(ValueError, "too large"):
                worker._read_request(server)
        finally:
            server.close()
            client.close()

    def test_read_uses_total_deadline(self):
        conn = mock.Mock()
        conn.recv.side_effect = [b"x", b"y"]
        with mock.patch.object(worker.time, "monotonic", side_effect=[0, 0, 4, 5.1]):
            with self.assertRaises(TimeoutError):
                worker._read_request(conn)
        self.assertEqual(conn.settimeout.call_args_list, [mock.call(5), mock.call(1)])

    def test_long_id_rejected_and_connection_slot_released(self):
        server, client = socket.socketpair()
        self.state.connections.acquire()
        client.sendall((json.dumps({"cmd": "cancel", "request_id": "x" * 257}) + "\n").encode())
        worker._serve_connection(server, self.cache, self.config, self.state, True)
        try:
            reply = json.loads(client.recv(4096))
            self.assertFalse(reply["ok"])
            self.assertFalse(self.state._early)
            for _ in range(worker._MAX_CONNECTIONS):
                self.assertTrue(self.state.connections.acquire(False))
            self.assertFalse(self.state.connections.acquire(False))
        finally:
            client.close()

    def test_response_timeouts(self):
        conn = mock.Mock()
        worker._send(conn, {"ok": False, "busy": True})
        self.assertEqual(conn.settimeout.call_args, mock.call(.1))
        worker._send(conn, {"ok": True})
        self.assertEqual(conn.settimeout.call_args, mock.call(1))

    def test_thread_start_failure_releases_connection(self):
        listener = mock.Mock()
        conn = mock.Mock()
        listener.accept.side_effect = [(conn, None), OSError("stop")]
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("cannot start")
        path = mock.Mock()
        with mock.patch.object(worker, "_WorkerState", return_value=self.state), \
                mock.patch.object(worker, "_bind", return_value=listener), \
                mock.patch.object(worker.threading, "Thread", return_value=thread), \
                mock.patch.object(worker, "Path", return_value=path):
            worker.run_server(path, self.config, idle_timeout=0)
        conn.close.assert_called_once()
        for _ in range(worker._MAX_CONNECTIONS):
            self.assertTrue(self.state.connections.acquire(False))

    def test_connection_overflow_is_bounded_busy_reply(self):
        listener = mock.Mock()
        conn = mock.Mock()
        listener.accept.side_effect = [(conn, None), OSError("stop")]
        for _ in range(worker._MAX_CONNECTIONS):
            self.state.connections.acquire()
        path = mock.Mock()
        with mock.patch.object(worker, "_WorkerState", return_value=self.state), \
                mock.patch.object(worker, "_bind", return_value=listener), \
                mock.patch.object(worker.threading, "Thread") as thread, \
                mock.patch.object(worker, "Path", return_value=path):
            worker.run_server(path, self.config, idle_timeout=0)
        thread.assert_not_called()
        conn.settimeout.assert_called_with(.1)
        self.assertTrue(json.loads(conn.sendall.call_args.args[0])["busy"])
        conn.close.assert_called_once()

    def test_duplicate_id_cannot_replace_active_owner(self):
        event = self.state.begin("same")
        reply = worker._dispatch({"cmd": "warm", "request_id": "same"}, self.cache, self.config, self.state)
        self.assertFalse(reply["ok"])
        self.assertIs(self.state._active["same"], event)
        self.assertTrue(self.state.operations.acquire(False))
        self.assertTrue(self.state.operations.acquire(False))

    def test_queue_timeout_validation_and_command_specific_maximum(self):
        cases = [("transcribe", None, 90), ("warm", None, 900),
                 ("transcribe", float("nan"), 90), ("warm", float("inf"), 900),
                 ("transcribe", -1, 90), ("warm", "bad", 900),
                 ("transcribe", 1000, 90), ("warm", 1000, 900),
                 ("transcribe", .05, .05)]
        for command, timeout, expected in cases:
            with self.subTest(command=command, timeout=timeout):
                state = worker._WorkerState()
                state.serial = mock.Mock()
                state.serial.acquire.return_value = True
                payload = {"cmd": command, "request_id": "test"}
                if timeout is not None:
                    payload["queue_timeout_sec"] = timeout
                with mock.patch.object(worker, "_POLL_INTERVAL", 1000), \
                        mock.patch.object(worker.time, "monotonic", return_value=0), \
                        mock.patch.object(worker, "handle_request", return_value={"ok": True}):
                    worker._dispatch(payload, self.cache, self.config, state)
                state.serial.acquire.assert_called_once_with(timeout=expected)
