"""Portable processes and shortcuts must survive the UI's temporary mount."""
import os
import shlex
import unittest
from unittest import mock
from wayvoice import service, shortcut, updater


class AppImageRuntimeTests(unittest.TestCase):
    def test_portable_mode_never_uses_host_systemd(self):
        with mock.patch.dict(os.environ, {'WAYVOICE_APPIMAGE': '/tmp/Way Voice.AppImage'}), mock.patch.object(service.shutil, 'which', return_value='/usr/bin/systemctl'), mock.patch.object(service.os.path, 'isdir', return_value=True):
            self.assertFalse(service.systemd_available())

    def test_detached_daemon_owns_a_new_image_mount(self):
        with mock.patch.dict(os.environ, {'WAYVOICE_APPIMAGE': '/tmp/Way Voice.AppImage'}), mock.patch.object(service, 'service_log_path', side_effect=OSError), mock.patch.object(service.subprocess, 'Popen') as spawn:
            service._spawn(service.DAEMON_MODULE)
            self.assertEqual(spawn.call_args.args[0], ['/tmp/Way Voice.AppImage', '--daemon'])
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            service._spawn(service.ENGINE_SETUP_MODULE)
            self.assertEqual(spawn.call_args.args[0], ['/tmp/Way Voice.AppImage', '--engine-setup'])

    def test_shortcut_uses_persistent_image_and_quotes_spaces(self):
        with mock.patch.dict(os.environ, {'WAYVOICE_APPIMAGE': '/home/user/Way Voice.AppImage'}):
            self.assertEqual(shlex.split(shortcut.manual_command()), ['/home/user/Way Voice.AppImage', 'toggle'])

    def test_native_updater_refuses_image_before_package_probes(self):
        with mock.patch.dict(os.environ, {'WAYVOICE_APPIMAGE': '/tmp/WayVoice.AppImage'}), mock.patch.object(updater, '_output') as probe:
            with self.assertRaisesRegex(ValueError, 'AppImage'):
                updater.package_system()
            probe.assert_not_called()

    def test_ui_restart_uses_stable_image_entry_point(self):
        try:
            from wayvoice.ui import application
        except (ImportError, ValueError):
            self.skipTest('GTK unavailable')
        window = mock.Mock()
        window.get_application.return_value.get_windows.return_value = []
        with mock.patch.dict(os.environ, {'WAYVOICE_APPIMAGE': '/tmp/Way Voice.AppImage'}), mock.patch.object(application.os, 'execv') as execute:
            application.restart_installed_ui(window)
        execute.assert_called_once_with('/tmp/Way Voice.AppImage', ['/tmp/Way Voice.AppImage', '--settings'])
