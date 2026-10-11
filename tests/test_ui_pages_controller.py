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
        self.assertEqual([s[0] for s in self.tasks.sources], ['idle','idle','idle',60_000,650,900,'idle'])
        # The new startup source and timer are inert without saved consent.
        _, configure, args = self.tasks.sources[2]
        configure(*args)
        self.tasks.sources[3][1]()
        self.assertFalse(self.window.context.updates.pending)

    def test_settings_uses_available_width_within_clamp(self):
        clamp = self.window.settings.root.get_child().get_child()
        page = clamp.get_child()
        for width in (420, 1600):
            with self.subTest(width=width):
                clamp.allocate(width, 900, -1, None)
                self.assertGreaterEqual(page.get_width(), min(width, 780) - 10)
                self.assertLessEqual(page.get_width(), 780)

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

    def test_language_draft_follows_active_engine(self):
        ctx = self.window.context
        with (mock.patch.object(ctx.models, '_refresh_model_state'),
              mock.patch.object(ctx.preferences, '_poll_engine_settings')):
            self.window.settings.model.set_selected(6)  # tiny.en
            self.assertEqual(ctx.preferences._draft_values()['language'], 'en')
            for index in (1, 2):  # whisper.cpp, external command
                self.window.settings.engine.set_selected(index)
                self.window.settings.language.select_code('ru')
                self.assertEqual(ctx.preferences._draft_values()['language'], 'ru')

    def test_dictionary_draft_and_invalid_save_do_not_start_work(self):
        ctx = self.window.context
        ctx.settings.vocabulary.enabled.set_active(True)
        ctx.settings.vocabulary.buffer.set_text('way voice => WayVoice')
        self.assertTrue(ctx.preferences.has_unsaved_changes())
        draft = ctx.preferences._draft_values()
        self.assertTrue(draft['vocabulary_enabled'])
        self.assertEqual(draft['vocabulary_rules'], 'way voice => WayVoice')
        ctx.settings.vocabulary.buffer.set_text('invalid')
        self.assertFalse(ctx.preferences._save())
        self.assertFalse(ctx.preferences._saving)
        self.assertEqual(ctx.preferences._mutations, [])

    def test_disposal_removes_startup_and_poll_sources(self):
        self.window._dispose_ui()
        self.window._dispose_ui()
        self.assertTrue(self.tasks.closed)
        self.assertEqual(self.tasks.sources, [])
