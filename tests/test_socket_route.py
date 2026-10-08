"""Socket discovery never lets a stale preferred path hide a live helper."""
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import injector


class SocketRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.packaged = self.root / 'wayvoice-ydotool.sock'
        self.distro = self.root / injector.DEFAULT_SOCKET_NAME
        self.legacy = self.root / 'legacy.sock'
        for patcher in (mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.root)}),
                        mock.patch.object(injector, 'LEGACY_SOCKET', str(self.legacy)),
                        mock.patch.object(injector, '_helper_started_at', None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def bind(self, path, stale=False):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(listener.close)
        listener.bind(str(path))
        if stale:
            listener.close()
        return listener

    def test_stale_packaged_does_not_hide_live_distro(self):
        self.bind(self.packaged, stale=True)
        listener = self.bind(self.distro)
        self.assertEqual(injector.ydotool_socket(), self.distro)
        self.assertTrue(injector.helper_answering())
        self.assertEqual(injector._ydotool_env()['YDOTOOL_SOCKET'], str(self.distro))
        listener.settimeout(0.01)
        with self.assertRaises(TimeoutError):
            listener.recv(1)  # Discovery sends no input or health datagram.

    def test_stale_runtime_paths_do_not_hide_live_legacy(self):
        self.bind(self.packaged, stale=True)
        self.bind(self.distro, stale=True)
        self.bind(self.legacy)
        self.assertEqual(injector.ydotool_socket(), self.legacy)

    def test_two_live_helpers_keep_packaged_priority(self):
        self.bind(self.packaged)
        self.bind(self.distro)
        self.assertEqual(injector.ydotool_socket(), self.packaged)

    def test_all_stale_preserve_diagnostic_path_without_claiming_live(self):
        self.bind(self.packaged, stale=True)
        self.bind(self.distro, stale=True)
        self.assertEqual(injector.ydotool_socket(), self.packaged)
        self.assertFalse(injector.helper_answering())

    def test_missing_paths_return_none(self):
        self.assertIsNone(injector.ydotool_socket())
        self.assertFalse(injector.helper_answering())

    def test_regular_file_does_not_hide_live_alternate(self):
        self.packaged.touch()
        self.bind(self.distro)
        self.assertEqual(injector.ydotool_socket(), self.distro)

    def test_live_alternate_prevents_duplicate_service_start_and_routes_client(self):
        self.bind(self.packaged, stale=True)
        self.bind(self.distro)
        with mock.patch('wayvoice.service.start_user_unit') as start, \
             mock.patch.object(injector, 'ydotool_command', return_value='fake-ydotool'), \
             mock.patch.object(injector, '_run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run, \
             mock.patch.object(injector.time, 'sleep'):
            self.assertTrue(injector.ensure_helper_running())
            self.assertEqual(injector.paste_with_ydotool('standard'), (True, ''))
        start.assert_not_called()
        self.assertEqual(run.call_args.kwargs['env']['YDOTOOL_SOCKET'], str(self.distro))
