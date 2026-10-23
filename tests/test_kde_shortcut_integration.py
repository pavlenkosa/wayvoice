"""Desktop activation uses the dictation queue; setup does not start recording."""
import queue
import unittest
from unittest import mock
from wayvoice.daemon import WayVoiceDaemon


class PortalDaemonTests(unittest.TestCase):
    def setUp(self):
        self.daemon = WayVoiceDaemon()
        self.daemon._shortcut_portal = mock.Mock()

    def test_explicit_configuration_requires_supported_desktop_and_valid_config(self):
        with mock.patch('wayvoice.daemon.portal_shortcut_desktop', return_value=True), \
                mock.patch('wayvoice.daemon.load_config', return_value={'shortcut': 'F9'}), \
                mock.patch('wayvoice.daemon.config_error', return_value=''):
            self.assertTrue(self.daemon.dispatch('configure-shortcut')['ok'])
            self.daemon._shortcut_portal.configure.assert_called_once_with('F9')
            self.daemon._shortcut_portal.configure.return_value = False
            self.assertFalse(self.daemon.dispatch('configure-shortcut')['ok'])
        self.daemon._shortcut_portal.reset_mock()
        with mock.patch('wayvoice.daemon.portal_shortcut_desktop', return_value=False):
            self.assertFalse(self.daemon.dispatch('configure-shortcut')['ok'])
        self.daemon._shortcut_portal.configure.assert_not_called()

    def test_activation_queues_one_toggle_and_shutdown_rejects_late_events(self):
        commands = queue.Queue()
        self.daemon._commands = commands
        with mock.patch.object(self.daemon, 'toggle') as toggle:
            self.daemon._portal_activation()
            toggle.assert_not_called()
            commands.put(None)
            self.daemon._command_loop(commands)
            toggle.assert_called_once_with()
            self.daemon._shutdown.set()
            self.daemon._portal_activation()
            self.assertTrue(commands.empty())
            self.assertFalse(self.daemon.dispatch('toggle')['ok'])

    def test_restore_is_opt_in_and_not_a_configuration_request(self):
        with mock.patch('wayvoice.daemon.portal_shortcut_desktop', return_value=True), \
                mock.patch('wayvoice.daemon.load_config', return_value={'shortcut': 'F8'}):
            self.daemon._restore_portal_shortcut()
            self.daemon._shortcut_portal.start.assert_not_called()
        with mock.patch('wayvoice.daemon.portal_shortcut_desktop', return_value=True), \
                mock.patch('wayvoice.daemon.load_config', return_value={'shortcut_backend': 'portal', 'shortcut': 'F9'}):
            self.daemon._restore_portal_shortcut()
            self.daemon._shortcut_portal.start.assert_called_once_with('F9')
            self.daemon._shortcut_portal.configure.assert_not_called()

from tests.ui_support import GTK_AVAILABLE, controller_context

if GTK_AVAILABLE:
    from wayvoice.ui.controllers.shortcut import ShortcutController


@unittest.skipUnless(GTK_AVAILABLE, 'GTK/libadwaita bindings unavailable')
class PortalControllerTests(unittest.TestCase):
    def test_configuration_serializes_with_save_and_does_not_change_draft(self):
        ctx = controller_context()
        ctx.preferences._queue_mutation = mock.Mock()
        before = ctx.state.shortcut_binding
        with mock.patch('wayvoice.ui.controllers.shortcut.portal_shortcut_desktop', return_value=True):
            ctx.shortcut._open_shortcut_capture()
            ctx.shortcut._open_shortcut_capture()
        ctx.preferences._queue_mutation.assert_called_once()
        work, done, failed = ctx.preferences._queue_mutation.call_args.args
        with mock.patch('wayvoice.ui.controllers.shortcut.service.start_daemon', return_value=True), \
                mock.patch('wayvoice.ui.controllers.shortcut.request', return_value={'ok': True}) as request, \
                mock.patch('wayvoice.ui.controllers.shortcut.update_config', return_value={'model': 'medium', 'shortcut_backend': 'portal'}) as save:
            work()
            request.assert_called_once_with('configure-shortcut', timeout=2.0)
            save.assert_called_once_with({'shortcut_backend': 'portal'}, {'shortcut_backend': None})
        done(None)
        self.assertEqual(ctx.state.shortcut_binding, before)
        ctx.settings.shortcut_button.set_sensitive.assert_called_with(True)

    def test_actual_portal_binding_overrides_saved_preference(self):
        ctx = controller_context()
        ctx.integration._missing_required = mock.Mock(return_value=[])
        for portal, expected in [({'state': 'active', 'trigger': 'Meta+F9'}, 'Meta+F9'),
                                 ({'state': 'needs_configuration', 'trigger': ''}, 'shortcut.disabled')]:
            with mock.patch('wayvoice.ui.controllers.status.request', return_value={'ok': True, 'shortcut_portal': portal}), \
                    mock.patch('wayvoice.ui.controllers.status.shortcut_support', return_value=(False, 'manual')), \
                    mock.patch('wayvoice.ui.controllers.status.load_config', return_value={'shortcut': 'F8'}):
                ctx.state.t = lambda key, **kw: key
                reply, _, _ = ctx.status._status_snapshot()
                self.assertEqual(reply['shortcut'], expected)
                self.assertEqual(reply['shortcut_support'][0], portal['state'] == 'active')
