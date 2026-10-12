"""First-run choices are inert until explicit preparation; failures remain recoverable."""
import unittest
from types import SimpleNamespace
from unittest import mock
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, Gio, GLib
    from wayvoice.ui.onboarding import OnboardingWindow
    from wayvoice.ui.application import App
    from wayvoice.engine import TranscriptionCancelled
except (ImportError, ValueError):
    OnboardingWindow = None


@unittest.skipIf(OnboardingWindow is None, 'GTK UI package unavailable')
class OnboardingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check():
            raise unittest.SkipTest('GTK display unavailable')
        cls.app = Adw.Application(application_id='io.github.stepan.WayVoice.OnboardingTest',
                                  flags=Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)

    def setUp(self):
        self.tasks = mock.Mock()
        self.finished = mock.Mock()
        with mock.patch('wayvoice.ui.onboarding.TaskRunner', return_value=self.tasks), \
                mock.patch('wayvoice.ui.onboarding.load_config', return_value={}):
            self.win = OnboardingWindow(self.app, self.finished)
        self.addCleanup(self.win.destroy)

    def test_choices_do_not_schedule_download_and_language_changes_immediately(self):
        self.win.language = 'ru'
        self.win._render()
        self.assertEqual(self.win.primary.get_label(), 'Продолжить')
        self.win.primary.emit('clicked')
        button = Gtk.CheckButton(active=True)
        self.win._model_selected(button, 'medium')
        self.win.primary.emit('clicked')
        self.assertEqual(self.win.model, 'medium')
        self.assertEqual(self.win.step, 2)
        self.tasks.run.assert_not_called()

    def test_all_ui_languages_render_and_picker_changes_direction(self):
        from wayvoice.i18n import SUPPORTED_UI_LANGUAGES, tr
        self.addCleanup(Gtk.Widget.set_default_direction, Gtk.TextDirection.LTR)
        def find_combo(widget):
            if isinstance(widget, Adw.ComboRow):
                return widget
            child = widget.get_first_child()
            while child:
                found = find_combo(child)
                if found:
                    return found
                child = child.get_next_sibling()
        for code in SUPPORTED_UI_LANGUAGES:
            self.win.language = code
            self.win._render()
            row = find_combo(self.win.body)
            self.assertEqual(row.get_model().get_n_items(), len(SUPPORTED_UI_LANGUAGES))
            self.assertEqual(self.win.primary.get_label(), tr('onboard.continue', code))
        self.win.language = 'en'
        self.win._render()
        row = find_combo(self.win.body)
        row.set_selected(SUPPORTED_UI_LANGUAGES.index('ar'))
        self.assertEqual(self.win.language, 'ar')
        self.assertEqual(self.win.get_direction(), Gtk.TextDirection.RTL)
        row = find_combo(self.win.body)
        row.set_selected(SUPPORTED_UI_LANGUAGES.index('de'))
        self.assertEqual(self.win.language, 'de')
        self.assertEqual(self.win.get_direction(), Gtk.TextDirection.LTR)

    def test_explicit_download_single_flight_success_then_explicit_save(self):
        self.win._go(2)
        self.win._start()
        self.win._start()
        self.tasks.run.assert_called_once()
        work, done, failed = self.tasks.run.call_args.args
        with mock.patch('wayvoice.ui.onboarding.onboarding.save_selection', return_value={'model': 'small'}) as save, \
                mock.patch('wayvoice.ui.onboarding.onboarding.prepare_selection', return_value={'ready': True}) as prepare:
            result = work()
            save.assert_called_once_with('auto', 'small', completed=False, expected={})
            self.assertEqual(self.win._expected_config, {})
            publish, accepted = self.tasks.idle.call_args.args
            publish(accepted)
            self.assertIs(prepare.call_args.args[1], self.win.cancel_event)
        done(result)
        self.assertEqual(self.win.step, 3)
        self.assertTrue(self.win.prepared)
        self.finished.assert_not_called()
        self.win._finish()
        work, done, failed = self.tasks.run.call_args.args
        with mock.patch('wayvoice.ui.onboarding.onboarding.save_selection') as save:
            work()
            save.assert_called_once_with('auto', 'small', completed=True, deferred=False,
                                         expected=self.win._expected_config)
        done({})
        self.finished.assert_called_once_with(self.win)

    def test_persisted_baseline_published_on_main_loop_before_prepare_failure(self):
        self.win._expected_config = {"model": "small", "notify": True}
        self.win.model = "tiny"
        self.win._go(2)
        self.win._start()
        work, done, failed = self.tasks.run.call_args.args
        with (mock.patch('wayvoice.ui.onboarding.onboarding.save_selection', return_value={"model": "tiny", "notify": False}),
              mock.patch('wayvoice.ui.onboarding.onboarding.prepare_selection', side_effect=RuntimeError("failed"))):
            with self.assertRaises(RuntimeError):
                work()
        self.assertEqual(self.win._expected_config["model"], "small")
        publish, accepted = self.tasks.idle.call_args.args
        publish(accepted)
        failed(RuntimeError("failed"))
        self.assertEqual(self.win._expected_config["model"], "tiny")
        self.assertTrue(self.win._expected_config["notify"])
        self.assertFalse(self.win.busy)

    def test_defer_is_saved_only_on_finish_and_save_error_allows_retry(self):
        self.win._go(3)
        self.tasks.run.assert_not_called()
        self.win._finish()
        work, done, failed = self.tasks.run.call_args.args
        with mock.patch('wayvoice.ui.onboarding.onboarding.save_selection') as save:
            work()
            save.assert_called_once_with('auto', 'small', completed=True, deferred=True, expected={})
        failed(OSError('disk full'))
        self.assertFalse(self.win.busy)
        self.assertTrue(self.win.primary.get_sensitive())
        self.assertIn('disk full', self.win.error.get_text())
        self.finished.assert_not_called()

    def test_open_failure_keeps_retry_available(self):
        self.win._go(3)
        self.win.busy = True
        self.win.primary.set_sensitive(False)
        self.finished.side_effect = RuntimeError('could not open')
        self.win._finished({})
        self.assertFalse(self.win.busy)
        self.assertTrue(self.win.primary.get_sensitive())
        self.assertIn('could not open', self.win.error.get_text())

    def test_cancel_and_error_restore_actions_close_waits_for_cleanup(self):
        self.win._go(2)
        self.win._start()
        self.win._cancel()
        self.assertTrue(self.win.cancel_event.is_set())
        self.win._failed(TranscriptionCancelled())
        self.assertFalse(self.win.busy)
        self.assertTrue(self.win.primary.get_sensitive())
        self.assertTrue(self.win.error.get_text())
        self.win._start()
        self.assertFalse(self.win.cancel_event.is_set())
        self.assertTrue(self.win._close_requested())
        self.tasks.close.assert_not_called()
        with mock.patch.object(self.win, 'close') as close:
            self.win._failed(TranscriptionCancelled())
            close.assert_called_once()
        self.win._close_requested()
        self.tasks.close.assert_called_once()

    def test_first_run_routing_and_existing_wizard_singleton(self):
        fake = SimpleNamespace(get_windows=lambda: [self.win], _onboarding_finished=mock.Mock())
        with mock.patch.object(self.win, 'present') as present, \
                mock.patch('wayvoice.ui.application.needs_onboarding') as needs:
            App.do_activate(fake)
            present.assert_called_once()
            needs.assert_not_called()
        fake.get_windows = lambda: []
        created = []
        class Wizard:
            def __init__(self, app, done):
                self.present = mock.Mock()
                created.append(self)
        with mock.patch('wayvoice.ui.application.OnboardingWindow', Wizard), \
                mock.patch('wayvoice.ui.application.needs_onboarding', return_value=True):
            App.do_activate(fake)
        self.assertEqual(len(created), 1)
        created[0].present.assert_called_once()

    def test_readable_width_when_maximized_and_narrow_layout_scrolls(self):
        import time
        self.win.language = 'ru'
        self.win._go(1)
        for width in (1000, 360):
            self.win.set_default_size(width, 700)
            self.win.present()
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline:
                while GLib.MainContext.default().pending():
                    GLib.MainContext.default().iteration(False)
                time.sleep(0.01)
            actual = self.win.body.get_width()
            if width == 1000:
                self.assertGreaterEqual(actual, 480)
                self.assertLessEqual(actual, 560)
            else:
                self.assertGreaterEqual(actual, 280)
            self.assertTrue(self.win.primary.get_mapped())
