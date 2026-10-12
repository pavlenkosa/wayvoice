"""Overload admission failures must never launch a second recognizer."""
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine
from wayvoice.runtime import locations, processes, worker_client
from wayvoice.runtime.errors import WorkerBusyError


class WorkerBusyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "bin").mkdir()
        (self.root / "bin/python").touch()
        patch = mock.patch.object(locations, "faster_runtime", return_value=self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.busy = {"ok": False, "busy": True, "error": "at capacity"}

    def test_ping_busy_propagates_without_spawn_or_fallback(self):
        path = self.root / "worker.sock"
        path.touch()
        with mock.patch.object(locations, "worker_socket_path", return_value=path), \
                mock.patch.object(worker_client, "_worker_call", return_value=self.busy), \
                mock.patch.object(worker_client, "_start_worker") as spawn, \
                mock.patch.object(processes, "_run_cancelable") as fallback:
            with self.assertRaisesRegex(WorkerBusyError, "at capacity"):
                engine._transcribe_faster(self.root / "audio.wav", {}, None)
            spawn.assert_not_called()
            fallback.assert_not_called()

    def request_socket(self):
        sock = mock.Mock()
        sock.getsockopt.return_value = struct.pack("3i", os.getpid(), os.getuid(), os.getgid())
        return sock

    def test_busy_recovery_preflight_never_falls_back(self):
        sock = self.request_socket()
        with mock.patch.object(worker_client, "ensure_worker", return_value=True), \
                mock.patch.object(worker_client.socket, "socket", return_value=sock), \
                mock.patch.object(worker_client, "_worker_call", return_value=self.busy), \
                mock.patch.object(processes, "_run_cancelable") as fallback:
            with self.assertRaises(WorkerBusyError):
                engine._transcribe_faster(self.root / "audio.wav", {}, None)
            sock.sendall.assert_not_called()
            fallback.assert_not_called()
        sock.close.assert_called_once()

    def test_busy_operation_reply_without_request_id_never_falls_back(self):
        sock = self.request_socket()
        sock.recv.return_value = (json.dumps(self.busy) + "\n").encode()
        def health(payload, timeout):
            return {"ok": True, "pid": os.getpid(), "request_id": payload["request_id"],
                    "state": "unknown", "request_completion": True}
        with mock.patch.object(worker_client, "ensure_worker", return_value=True), \
                mock.patch.object(worker_client.socket, "socket", return_value=sock), \
                mock.patch.object(worker_client, "_worker_call", side_effect=health), \
                mock.patch.object(processes, "_run_cancelable") as fallback:
            with self.assertRaises(WorkerBusyError):
                engine._transcribe_faster(self.root / "audio.wav", {}, None)
            fallback.assert_not_called()
        sock.close.assert_called_once()
