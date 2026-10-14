"""GUI update ownership and restart are tested without installing any package."""
import io
import time
import unittest
from unittest import mock
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, Gio, GLib
    from wayvoice.ui.dialogs.update import UpdateDialog, show_update
    from wayvoice.ui.window import WayVoiceWindow
    from wayvoice.ui.state import UiState
except (ImportError, ValueError):
    UpdateDialog = None


@unittest.skipIf(UpdateDialog is None, 'GTK unavailable')
class UpdateUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest('GTK display unavailable')
        cls.app = Adw.Application(application_id='io.github.stepan.WayVoice.UpdateTest',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        with mock.patch('wayvoice.ui.window.TaskRunner'), \
             mock.patch('wayvoice.ui.window.load_config', return_value={'ui_language': 'en'}):
            self.window = WayVoiceWindow(self.app)
        self.addCleanup(self.window.destroy)
        self.window.tasks.run.reset_mock()
        self.update = UpdateDialog(self.window)
        self.window._update_dialog = self.update
        self.addCleanup(self.update.dialog.destroy)

    def test_update_geometry_remains_stable_across_normal_stages(self):
        def settled_size():
            deadline = time.monotonic() + .25
            while time.monotonic() < deadline:
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                time.sleep(.005)
            size = (self.update.dialog.get_width(), self.update.dialog.get_height())
            self.assertGreaterEqual(size[0], 420)
            self.assertGreaterEqual(size[1], 320)
            return size

        self.window.present()
        for language in ('en', 'ru'):
            with self.subTest(language=language):
                self.update.dialog.destroy()
                self.window.state.ui_lang = language
                self.update = UpdateDialog(self.window)
                self.window._update_dialog = self.update
                self.addCleanup(self.update.dialog.destroy)
                initial = settled_size()
                self.update._checked({'available': True, 'version': '99.0.0'})
                self.update._supported(('deb', 'amd64'))
                self.assertEqual(settled_size(), initial)
                self.update._start()
                for stage in ('download', 'verify', 'authorize', 'install', 'restart'):
                    self.update._progress({'stage': stage})
                    self.assertEqual(settled_size(), initial)
                self.update._failed(RuntimeError('Authorization declined'))
                settled_size()
                self.update.details.set_text('Diagnostic detail\n' * 100)
                self.update.details_expander.set_expanded(True)
                settled_size()
                self.assertEqual(self.update.details.get_text().count('Diagnostic detail'), 100)

    def test_one_dialog_and_no_install_for_source_or_flatpak(self):
        show_update(self.window)
        self.assertIs(self.window._update_dialog, self.update)
        self.update._checked({'available': True, 'version': '99.0.0'})
        self.assertFalse(self.update.install_button.get_visible())
        self.update._unsupported(ValueError('source checkout'))
        self.assertIn('Flatpak', self.update.dialog.get_body())

    def test_busy_blocks_close_and_failure_restores_controls(self):
        self.update._start()
        self.assertTrue(self.window._close_requested())
        self.update.dialog.close()
        self.assertTrue(self.update.dialog.get_visible())
        self.assertFalse(self.update.dialog.get_response_enabled('close'))
        self.update._failed(RuntimeError('authorization declined'))
        self.assertFalse(self.window._update_busy)
        self.assertTrue(self.update.dialog.get_response_enabled('close'))
        self.assertIn('declined', self.update.details.get_text())

    def test_process_protocol_and_confirmed_success_restart(self):
        process = mock.Mock()
        process.stdout = io.StringIO('{"stage":"download"}\n{"stage":"done","version":"99.0.0"}\n')
        process.wait.return_value = 0
        process.__enter__ = mock.Mock(return_value=process)
        process.__exit__ = mock.Mock(return_value=False)
        with mock.patch('wayvoice.ui.dialogs.update.subprocess.Popen', return_value=process) as popen, \
             mock.patch('wayvoice.ui.dialogs.update.shutil.which', return_value='/prefix/bin/wayvoice'):
            result = self.update._transaction()
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.args[0][-1], '--gui-install')
        with mock.patch('wayvoice.ui.application.restart_installed_ui') as restart:
            self.update._finished(result)
            restart.assert_called_once_with(self.window)

    def test_restart_failure_preserves_window_and_status_does_not_compete(self):
        self.update._start()
        with mock.patch('wayvoice.ui.controllers.status.service.restart_daemon') as restart:
            self.assertFalse(self.window.context.status._maybe_restart({'ok': True, 'version': '99.0.0'}))
            restart.assert_not_called()
        with mock.patch('wayvoice.ui.application.restart_installed_ui', side_effect=OSError('launcher failed')):
            self.update._finished({'stage': 'done'})
        self.assertFalse(self.window._update_busy)
        self.assertIn('launcher failed', self.update.details.get_text())

    def test_save_then_update_waits_until_save_leaves_mutation_queue(self):
        prefs = self.window.context.preferences
        with mock.patch.object(prefs, 'has_unsaved_changes', return_value=True), \
             mock.patch.object(prefs, 'confirm_leaving') as confirm:
            self.update._install()
        prefs._mutations.append(('save', None, None))
        confirm.call_args.args[0]()
        self.assertFalse(self.update.busy)
        deferred = self.window.tasks.idle.call_args.args[0]
        prefs._mutations.clear()
        deferred()
        self.assertTrue(self.update.busy)

    def test_unsaved_confirmation_is_parented_to_active_update_dialog(self):
        prefs = self.window.context.preferences
        with mock.patch.object(prefs, 'has_unsaved_changes', return_value=True), \
             mock.patch('wayvoice.ui.dialogs.confirmations.unsaved_confirmation') as confirm:
            self.update._install()
        self.assertIs(confirm.call_args.args[0], self.update.dialog)
        self.assertFalse(self.update.busy)
        confirm.call_args.args[2]('stay')
        self.assertFalse(prefs._leave_dialog_open)
        self.assertFalse(self.update.busy)

    def test_normal_navigation_confirmation_keeps_main_window_parent(self):
        prefs = self.window.context.preferences
        proceed = mock.Mock()
        with mock.patch('wayvoice.ui.dialogs.confirmations.unsaved_confirmation') as confirm:
            prefs.confirm_leaving(proceed)
        self.assertIs(confirm.call_args.args[0], self.window)
        confirm.call_args.args[2]('leave')
        proceed.assert_called_once_with()
        self.assertFalse(prefs._leave_dialog_open)

    def test_home_notice_tracks_manual_check_and_reuses_update_action(self):
        self.update._checked({'available': True, 'version': '0.7.0'})
        banner = self.window.context.home.update_banner
        self.assertTrue(banner.get_revealed())
        self.assertTrue(banner.get_visible())
        self.assertIn('0.7.0', banner.get_title())
        with mock.patch('wayvoice.ui.window.show_update') as show:
            banner.emit('button-clicked')
            show.assert_called_once_with(self.window)
        self.update._checked({'available': False})
        self.assertFalse(banner.get_revealed())
        self.assertFalse(banner.get_visible())

    def test_restart_retry_keeps_actual_installed_version_and_skips_install(self):
        from wayvoice.ui.dialogs.update import UpdateFailure
        self.update.can_install = True
        self.update.target_version = '1.0.0'
        self.update._failed(UpdateFailure('restart failed', installed=True, version='1.0.1'))
        self.assertTrue(self.update.install_button.get_visible())
        self.assertTrue(self.update.restart_only)
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.StringIO('{"stage":"done","version":"1.0.1"}\n')
        process.wait.return_value = 0
        with mock.patch('wayvoice.ui.dialogs.update.subprocess.Popen', return_value=process) as popen, \
             mock.patch('wayvoice.ui.dialogs.update.shutil.which', return_value='/prefix/bin/wayvoice'):
            self.update._transaction()
        self.assertEqual(popen.call_args.args[0], ['/prefix/bin/wayvoice', 'update', '--gui-restart', '1.0.1'])
