"""The optional indicator only observes; settings and indicator have separate lifetimes."""
import unittest
from types import SimpleNamespace
from unittest import mock
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, Gio
    from wayvoice.ui.indicator import IndicatorWindow
    from wayvoice.ui.application import App
    from wayvoice.ui.window import WayVoiceWindow
except (ImportError, ValueError):
    IndicatorWindow = None


@unittest.skipIf(IndicatorWindow is None, 'GTK UI package unavailable')
class IndicatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
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
            present.assert_not_called()

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
        with mock.patch('wayvoice.ui.application.WayVoiceWindow', Settings):
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
        fake.context.preferences._mutations = []
        fake._confirm_exit.return_value = False
        WayVoiceWindow._quit(fake)
        fake.destroy.assert_called_once()
        fake.get_application.assert_not_called()
