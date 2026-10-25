"""Hold-mode choices are saved explicitly; indicator access is visible on Home."""
import unittest
from unittest import mock
from tests.ui_support import GTK_AVAILABLE, gtk_display_available

if GTK_AVAILABLE:
    from gi.repository import Gtk, Adw, Gio
    from wayvoice.ui.window import WayVoiceWindow


@unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")
class HoldUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not gtk_display_available():
            raise unittest.SkipTest('GTK display unavailable')
        cls.app = Adw.Application(application_id='io.github.stepan.WayVoice.HoldTest',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def test_saved_hold_mode_is_a_draft_and_backend_limit_is_visible(self):
        for portal in (True, False):
            with self.subTest(portal=portal), \
                 mock.patch('wayvoice.ui.window.TaskRunner'), \
                 mock.patch('wayvoice.ui.window.load_config', return_value={'ui_language': 'en', 'shortcut_mode': 'hold'}), \
                 mock.patch('wayvoice.ui.pages.settings.portal_shortcut_desktop', return_value=portal):
                window = WayVoiceWindow(self.app)
                try:
                    mode = window.context.settings.shortcut_mode
                    self.assertEqual(mode.get_selected(), 1)
                    self.assertEqual(mode.get_sensitive(), portal)
                    self.assertEqual(window.context.preferences._draft_values()['shortcut_mode'], 'hold')
                    self.assertTrue(mode.get_subtitle())
                    mode.set_selected(0)
                    self.assertEqual(bool(mode.get_subtitle()), not portal)
                    self.assertTrue(window.context.preferences.has_unsaved_changes())
                    self.assertEqual(window.context.preferences._draft_values()['shortcut_mode'], 'toggle')
                    with mock.patch.object(window, 'get_application') as application:
                        window.lookup_action('indicator').activate(None)
                        application.return_value.open_indicator.assert_called_once()
                finally:
                    window.destroy()
