"""Dialogs use a minimal transient window, never the real application startup."""
import os
import unittest
from unittest import mock
from wayvoice.i18n import tr
from tests.support import isolate_environment

try:
    import gi
    gi.require_version('Gtk','4.0')
    gi.require_version('Adw','1')
    from gi.repository import Gtk, Adw
    from wayvoice.ui.dialogs.about import show_about
    from wayvoice.ui.dialogs.confirmations import download_confirmation
    from wayvoice.ui.dialogs.model_delete import delete_confirmation
    from wayvoice.ui.dialogs.shortcut_window import shortcut_capture
except Exception:
    Gtk = None


def has_display():
    return bool(Gtk and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and Gtk.init_check())


@unittest.skipUnless(has_display(), 'no GTK display available')
class TestDialogs(unittest.TestCase):
    def setUp(self):
        isolate_environment(self)
        Adw.init()
        self.existing = set(Gtk.Window.list_toplevels())
        self.window = Gtk.Window()
        self.window.t = lambda key, **kw: tr(key, 'en', **kw)
        self.addCleanup(self.cleanup_windows)

    def cleanup_windows(self):
        for window in Gtk.Window.list_toplevels():
            if window not in self.existing:
                window.destroy()

    def find(self, title):
        return next(w for w in Gtk.Window.list_toplevels() if w.get_title() == title)

    def test_download_confirmation(self):
        confirmed = mock.Mock()
        download_confirmation(self.window,'small',1500,self.window.t,'en',confirmed)
        dialog = self.find(self.window.t('store.download_title'))
        self.assertTrue(dialog.get_modal())
        self.assertIs(dialog.get_transient_for(), self.window)
        buttons = dialog.get_child().get_last_child()
        buttons.get_last_child().emit('clicked')
        confirmed.assert_called_once()
        self.assertIs(confirmed.call_args.args[1], dialog)

    def test_unknown_size_download_still_opens(self):
        download_confirmation(self.window,'small',0,self.window.t,'en',mock.Mock())
        self.assertTrue(self.find(self.window.t('store.download_title')).get_modal())

    def test_delete_keeps_explicit_target(self):
        confirmed = mock.Mock()
        delete_confirmation(self.window,'MyOrg/MyModel','Model',self.window.t,confirmed)
        dialog = self.find(self.window.t('store.delete_title'))
        dialog.get_child().get_last_child().get_last_child().emit('clicked')
        self.assertEqual(confirmed.call_args.args[2], 'MyOrg/MyModel')

    def test_show_about(self):
        show_about(self.window)
        about = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Adw.AboutWindow))
        self.assertEqual(about.get_application_name(), 'WayVoice')

    def test_shortcut_capture(self):
        disable = mock.Mock()
        shortcut_capture(self.window,'F8',self.window.t,disable,mock.Mock())
        dialog = self.find(self.window.t('shortcut.title'))
        self.assertTrue(dialog.get_modal())
        dialog.get_child().get_last_child().get_first_child().emit('clicked')
        disable.assert_called_once()
