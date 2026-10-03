import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice.daemon import WayVoiceDaemon
from wayvoice.protocol import socket_path


class _FakeDaemonServer:
    """A minimal peer that answers ``ping`` the way the daemon does."""

    def __init__(self, path: Path, answer: bytes = b'{"ok": true, "pong": true}\n'):
        self.path = path
        self.answer = answer
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.stop = threading.Event()

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                try:
                    conn.settimeout(0.5)
                    data = b""
                    while not data.endswith(b"\n"):
                        chunk = conn.recv(256)
                        if not chunk:
                            break
                        data += chunk
                    if data:
                        conn.sendall(self.answer)
                except OSError:
                    pass

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.sock.close()


class LiveDaemonProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})
        patch.start()
        self.addCleanup(patch.stop)
        self.path = socket_path()
        self.daemon = WayVoiceDaemon()

    def test_no_socket_means_no_daemon(self):
        self.assertFalse(self.daemon._live_daemon(self.path))

    def test_answering_socket_means_a_daemon_is_live(self):
        with _FakeDaemonServer(self.path):
            self.assertTrue(self.daemon._live_daemon(self.path))

    def test_foreign_answer_counts_as_occupied(self):
        with _FakeDaemonServer(self.path, answer=b"not json at all\n"):
            self.assertTrue(self.daemon._live_daemon(self.path))

    def test_stale_socket_file_is_not_a_daemon(self):
        # Bound then closed: the file is there, nobody answers on it. This is
        # what a killed daemon leaves behind and must not block a new one.
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        self.assertTrue(self.path.exists())
        self.assertFalse(self.daemon._live_daemon(self.path))


class ServeResilienceTests(unittest.TestCase):
    """The accept loop must survive clients that do not read their answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        runtime = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)})
        patch.start()
        self.addCleanup(patch.stop)
        self.path = socket_path()
        self.daemon = WayVoiceDaemon()
        self.thread = threading.Thread(target=self.daemon.serve, daemon=True)
        self.addCleanup(self._stop)
        self.thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.path.exists():
            time.sleep(0.02)
        self.assertTrue(self.path.exists(), "the daemon did not create its socket")

    def _stop(self):
        self.daemon._shutdown.set()
        self.thread.join(timeout=5.0)

    def _request(self, payload: bytes) -> bytes:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        self.addCleanup(client.close)
        client.connect(str(self.path))
        client.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def test_connection_without_a_request_is_ignored(self):
        # Connecting and hanging up used to make the reply raise EPIPE, which
        # propagated out of the accept loop and killed the whole daemon.
        for _ in range(3):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2.0)
            client.connect(str(self.path))
            client.close()
        reply = self._request(b"ping\n")
        self.assertTrue(json.loads(reply.decode()).get("ok"))

    def test_socket_is_removed_on_exit(self):
        self._stop()
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
