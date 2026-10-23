"""The optional indicator only observes; settings and indicator have separate lifetimes."""
import unittest
from types import SimpleNamespace
from unittest import mock
from tests.ui_support import GTK_AVAILABLE, gtk_display_available

if GTK_AVAILABLE:
    from gi.repository import Gtk, Adw, Gio, Gdk
    from wayvoice.ui.indicator import IndicatorWindow
    from wayvoice.ui.application import App
    from wayvoice.ui.window import WayVoiceWindow


@unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")
class IndicatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not gtk_display_available():
            raise unittest.SkipTest('GTK display unavailable')
        cls.app = Adw.Application(application_id='io.github.stepan.WayVoice.IndicatorTest',
                                   flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        self.tasks = mock.Mock()
        with mock.patch('wayvoice.ui.indicator.TaskRunner', return_value=self.tasks):
            self.window = IndicatorWindow(self.app)
        self.addCleanup(self.window.destroy)

    def paint(self, **changes):
        reply = {'ok': True, 'engine': {'state': 'ready'}, 'last_text': 'private transcript'}
        reply.update(changes)
        self.window._paint((reply, {'ui_language': 'en'}))

    def test_states_never_raise_window_or_show_transcript(self):
        with mock.patch.object(self.window, 'present') as present:
            for changes, expected in (({}, 'Ready'), ({'recording': True}, 'Recording'),
                                      ({'busy': True}, 'Transcribing'), ({'ok': False}, 'Needs attention'),
                                      ({'model': {'supported': True, 'present': False}}, 'Needs attention'),
                                      ({'_missing_deps': ['wl-clipboard']}, 'Needs attention')):
                self.paint(**changes)
                self.assertEqual(self.window.state_label.get_text(), expected)
                self.assertNotIn('private transcript', self.window.detail_label.get_text())
            self.window._paint(({'ok': False}, {}))
            self.assertEqual(self.window.language, 'en')
            self.assertEqual(self.window.settings_button.get_tooltip_text(), 'Open WayVoice')
            present.assert_not_called()

    def test_hold_recording_explains_release_instead_of_another_press(self):
        self.paint(recording=True, ptt_recording=True)
        self.assertEqual(self.window.detail_label.get_text(), 'Release the shortcut to transcribe.')
        self.paint(recording=True)
        self.assertEqual(self.window.detail_label.get_text(), 'Press again when you are done.')

    def test_compact_drag_handle_and_details_keep_long_errors_out_of_row(self):
        self.assertIsInstance(self.window.handle, Gtk.WindowHandle)
        self.assertFalse(self.window.details.get_expanded())
        self.paint(last_error='A long failure ' * 50)
        self.assertFalse(self.window.details.get_expanded())
        self.assertEqual(self.window.state_label.get_tooltip_text(),
                         self.window.detail_label.get_text())
        self.assertLessEqual(self.window.get_default_size().height, 100)
        self.assertLessEqual(self.window.measure(Gtk.Orientation.VERTICAL, -1).minimum, 100)
        self.assertFalse(self.window.get_resizable())
        self.paint(busy=True)
        self.assertTrue(self.window.spinner.get_spinning())
        self.paint(recording=True)
        self.assertFalse(self.window.spinner.get_spinning())
        self.assertEqual(self.window.state_icon.get_icon_name(), 'media-record-symbolic')

    def test_window_actions_use_current_input_event_and_explain_unavailable_menu(self):
        event = object()
        surface = mock.Mock()
        with mock.patch.object(self.window, 'get_surface', return_value=surface):
            surface.show_window_menu.return_value = True
            self.assertTrue(self.window._show_window_menu(event))
            surface.show_window_menu.assert_called_once_with(event)
            self.assertFalse(self.window.details.get_expanded())
            surface.show_window_menu.return_value = False
            self.assertFalse(self.window._show_window_menu(event))
            self.assertTrue(self.window.details.get_expanded())
        controller = mock.Mock()
        controller.get_current_event.return_value = event
        with mock.patch.object(self.window, '_show_window_menu') as show:
            self.assertTrue(self.window._menu_key_pressed(controller, Gdk.KEY_space, 0, 0))
            self.assertTrue(self.window._window_key_pressed(
                controller, Gdk.KEY_F10, 0, Gdk.ModifierType.SHIFT_MASK))
            self.assertFalse(self.window._window_key_pressed(controller, Gdk.KEY_F10, 0, 0))
            self.assertEqual(show.call_count, 2)
            show.assert_called_with(event)
            gesture = mock.Mock()
            gesture.get_current_event.return_value = None
            self.window._menu_clicked(self.window.window_actions_button, gesture)
            show.assert_called_with(None)

    def test_language_refresh_preserves_menu_fallback_and_actual_portal_binding(self):
        reply = {'ok': True, 'engine': {'state': 'ready'}, 'shortcut_portal': {'trigger': ''}}
        self.window._paint((reply, {'ui_language': 'en', 'shortcut': 'F8'}))
        self.assertEqual(self.window.detail_label.get_text(), self.window.t('setup.shortcut_disabled'))
        self.window._show_window_menu(None)
        self.window._paint((reply, {'ui_language': 'ru', 'shortcut': 'F8'}))
        self.assertEqual(self.window.window_actions_hint.get_text(),
                         self.window.t('indicator.window_actions_unavailable'))
        self.window._menu_unavailable = False
        reply['shortcut_portal']['trigger'] = 'F9'
        self.window._paint((reply, {'ui_language': 'en', 'shortcut': ''}))
        self.assertEqual(self.window.window_actions_hint.get_text(),
                         self.window.t('indicator.window_actions_hint'))
        self.assertEqual(self.window.detail_label.get_text(), self.window.t('indicator.ready'))

    def test_poll_is_single_flight_and_close_disposes_callbacks(self):
        self.window._poll()
        self.window._poll()
        self.tasks.run.assert_called_once()
        self.window._dispose_ui()
        self.tasks.close.assert_called_once()

    def test_open_routes_to_singleton_indicator_and_settings_separately(self):
        fake_app = SimpleNamespace(get_windows=lambda: [self.window])
        with mock.patch.object(self.window, 'present') as present:
            App.open_indicator(fake_app)
            App.open_indicator(fake_app)
            self.assertEqual(present.call_count, 2)
        created = []
        class Settings:
            def __init__(self, app):
                self.app = app
                self.present = mock.Mock()
                created.append(self)
        with mock.patch('wayvoice.ui.application.WayVoiceWindow', Settings), \
                mock.patch('wayvoice.ui.application.needs_onboarding', return_value=False):
            App.do_activate(fake_app)
        self.assertEqual(len(created), 1)
        self.assertIs(created[0].app, fake_app)
        created[0].present.assert_called_once()

    def test_command_line_indicator_does_not_activate_settings(self):
        fake_app = SimpleNamespace(open_indicator=mock.Mock(), activate=mock.Mock())
        for indicator in (True, False):
            options = SimpleNamespace(contains=lambda key: indicator)
            command = SimpleNamespace(get_options_dict=lambda: options)
            self.assertEqual(App.do_command_line(fake_app, command), 0)
        fake_app.open_indicator.assert_called_once()
        fake_app.activate.assert_called_once()

    def test_language_replacement_preserves_indicator_and_closing_settings_only_destroys_it(self):
        old = mock.Mock()
        with mock.patch('wayvoice.ui.application.WayVoiceWindow') as constructor:
            App.replace_settings(self.app, old)
            constructor.assert_called_once_with(self.app)
            old._dispose_ui.assert_called_once()
            old.destroy.assert_called_once()
            constructor.return_value.present.assert_called_once()
        self.assertIn(self.window, self.app.get_windows())
        fake = mock.Mock()
        fake._update_busy = False
        fake.context.preferences._mutations = []
        fake._confirm_exit.return_value = False
        WayVoiceWindow._quit(fake)
        fake.destroy.assert_called_once()
        fake.get_application.assert_not_called()
