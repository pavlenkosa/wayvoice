"""Actual composition, navigation and signal wiring with all external work isolated."""
import os
import unittest
from unittest import mock
from tests.support import isolate_environment

try:
    from wayvoice import ui
    from gi.repository import Gtk, Adw, Gio
except Exception:
    ui = None


def has_display():
    return bool(ui and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and Gtk.init_check())


class RecordingTasks:
    def __init__(self):
        self.sources = []
        self.closed = False

    def idle(self, callback, *args):
        self.sources.append(('idle', callback, args))

    def every(self, ms, callback):
        self.sources.append((ms, callback))

    def run(self, *_args):
        raise AssertionError('a construction test must never perform external work')

    def close(self):
        self.closed = True
        self.sources.clear()


@unittest.skipUnless(has_display(), 'no GTK display available')
class CompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Adw.Application(application_id='io.github.stepan.WayVoice.CompositionTest', flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        isolate_environment(self)
        self.tasks = RecordingTasks()
        self.patchers = [
            mock.patch('wayvoice.ui.window.TaskRunner', return_value=self.tasks),
            mock.patch('wayvoice.ui.window.load_config', return_value={'ui_language':'en','engine':'faster-whisper','model':'small'}),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.window = ui.WayVoiceWindow(self.app)
        self.addCleanup(self.window.destroy)
        self.addCleanup(self.window._dispose_ui)

    def test_pages_own_widgets_and_window_only_composes(self):
        self.assertEqual(self.window.settings.model.get_selected(), 2)
        self.assertEqual(self.window.settings.custom_model.get_text(), '')
        self.assertIs(self.window.context.settings, self.window.settings)
        self.assertIs(self.window.context.home, self.window.home)
        self.assertFalse(hasattr(self.window, '_save'))
        self.assertFalse(hasattr(self.window, 'model'))
        self.assertEqual([s[0] for s in self.tasks.sources], ['idle','idle',650,900])

    def test_save_visibility_follows_navigation(self):
        self.assertFalse(self.window.save_button.get_visible())
        self.window.stack.set_visible_child_name('settings')
        self.assertTrue(self.window.save_button.get_visible())
        self.window.stack.set_visible_child_name('home')
        self.assertFalse(self.window.save_button.get_visible())

    def test_engine_change_reaches_owned_controllers(self):
        ctx = self.window.context
        with (mock.patch.object(ctx.models, '_sync_model_ui') as sync,
              mock.patch.object(ctx.models, '_refresh_model_state') as refresh,
              mock.patch.object(ctx.preferences, '_poll_engine_settings') as poll):
            self.window.settings.engine.set_selected(1)
        sync.assert_called_once()
        refresh.assert_called_once()
        poll.assert_called_once()
        self.assertFalse(self.window.settings.model.get_visible())
        self.assertTrue(self.window.settings.cpp_binary.get_visible())

    def test_disposal_removes_startup_and_poll_sources(self):
        self.window._dispose_ui()
        self.window._dispose_ui()
        self.assertTrue(self.tasks.closed)
        self.assertEqual(self.tasks.sources, [])
