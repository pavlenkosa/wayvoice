"""Behavioral regressions for asynchronous controller jobs and saved snapshots."""
import threading
import unittest
from unittest import mock
from tests.ui_support import controller_context
from tests.test_ui_async_tasks import FakeGLib, TaskRunner

try:
    from wayvoice.ui.controllers.settings import SettingsController
except Exception:
    SettingsController = None


class ControlledTasks:
    def __init__(self):
        self.pending = []

    def run(self, work, done, failed):
        self.pending.append((work, done, failed))

    def complete(self):
        work, done, failed = self.pending.pop(0)
        try:
            result = work()
        except Exception as exc:
            failed(exc)
        else:
            done(result)


@unittest.skipIf(SettingsController is None, 'GTK bindings unavailable')
class OwnedJobTests(unittest.TestCase):
    def setUp(self):
        self.ctx = controller_context()
        self.ctx.window._toast = mock.Mock()

    def test_successful_save_captures_widgets_and_runs_external_work_off_main_loop(self):
        from wayvoice.config import load_config
        from tests.support import isolate_environment
        isolate_environment(self)
        ctx = self.ctx
        glib = FakeGLib()
        ctx.tasks = TaskRunner(glib)
        self.addCleanup(ctx.tasks.close)
        # Populate the actual save inputs without GTK widget construction.
        for name in ('custom_model','cpp_binary','cpp_model','custom_command'):
            row = mock.Mock()
            row.get_text.return_value = ''
            setattr(ctx.settings, name, row)
        for name in ('engine','device','paste','timeout','max_recording'):
            row = mock.Mock()
            row.get_selected.return_value = 0
            setattr(ctx.settings, name, row)
        ctx.settings.model = mock.Mock()
        ctx.settings.model.get_selected.return_value = 2  # small
        ctx.settings.ui_language = mock.Mock()
        ctx.settings.ui_language.get_selected.return_value = 2  # en
        ctx.settings.language = mock.Mock()
        ctx.settings.language.selected_code.return_value = 'ru'
        for name in ('vad','worker','auto_punct','spoken','append_space','notifications','notification_text','cpp_gpu'):
            row = mock.Mock()
            row.get_active.return_value = True
            setattr(ctx.settings, name, row)
        ctx.window.save_button = mock.Mock()
        ctx.home.hotkey_label = mock.Mock()
        ctx.status._update_cards = mock.Mock()
        ctx.models._refresh_model_state = mock.Mock()
        ctx.preferences._poll_engine_settings = mock.Mock()
        entered, release = threading.Event(), threading.Event()
        threads = []
        def shortcut(binding, language=None):
            threads.append(threading.current_thread())
            entered.set()
            release.wait(2)
            return True, 'ok'
        with (mock.patch('wayvoice.ui.controllers.settings.apply_shortcut', side_effect=shortcut),
              mock.patch.object(ctx.preferences,'_prepare_selected_engine') as prepare):
            ctx.state.shortcut_binding = 'F8'
            ctx.preferences._save()
            self.assertTrue(entered.wait(2))
            self.assertTrue(ctx.preferences._saving)
            self.assertIsNot(threads[0],threading.current_thread())
            # Later widget edits cannot change this accepted save snapshot.
            ctx.state.shortcut_binding = 'F9'
            ctx.settings.language.selected_code.return_value = 'en'
            release.set()
            self.assertTrue(glib.ready.wait(2))
            glib.drain()
            prepare.assert_called_once()
        saved = load_config()
        self.assertEqual(saved['model'],'small')
        self.assertEqual(saved['language'],'ru')
        self.assertEqual(saved['shortcut'],'F8')
        self.assertIs(saved['notify_transcript'], True)
        ctx.home.hotkey_label.set_text.assert_called_once_with('F8')
        self.assertFalse(ctx.preferences._saving)
        ctx.window.save_button.set_sensitive.assert_called_with(True)

    def test_configuration_mutations_preserve_invocation_order(self):
        tasks = self.ctx.tasks = ControlledTasks()
        writes = []
        prefs = self.ctx.preferences
        prefs._queue_mutation(lambda: writes.append('setup') or 1, mock.Mock(), mock.Mock())
        prefs._queue_mutation(lambda: writes.append('save') or 2, mock.Mock(), mock.Mock())
        self.assertEqual(len(tasks.pending), 1)
        tasks.complete()
        self.assertEqual(writes, ['setup'])
        self.assertEqual(len(tasks.pending), 1)
        tasks.complete()
        self.assertEqual(writes, ['setup','save'])
        self.assertEqual(prefs._mutations, [])

    def test_close_waits_for_accepted_save_without_dropping_it(self):
        ctx = self.ctx
        ctx.tasks = ControlledTasks()
        writes = []
        ctx.window.close = mock.Mock()
        ctx.preferences._queue_mutation(lambda: writes.append('setup'), mock.Mock(), mock.Mock())
        ctx.preferences._queue_mutation(lambda: writes.append('save'), mock.Mock(), mock.Mock())
        self.assertTrue(ctx.window._close_requested())
        ctx.tasks.complete()
        ctx.window.close.assert_not_called()
        ctx.tasks.complete()
        self.assertEqual(writes, ['setup','save'])
        ctx.window.close.assert_called_once()

    def test_failed_configuration_mutation_does_not_strand_next_save(self):
        tasks = self.ctx.tasks = ControlledTasks()
        failed, done = mock.Mock(), mock.Mock()
        self.ctx.preferences._queue_mutation(mock.Mock(side_effect=OSError('disk')), mock.Mock(), failed)
        self.ctx.preferences._queue_mutation(lambda: 2, done, mock.Mock())
        tasks.complete()
        failed.assert_called_once()
        tasks.complete()
        done.assert_called_once_with(2)

    def test_save_completion_labels_saved_shortcut_not_later_draft(self):
        ctx = self.ctx
        ctx.state.shortcut_binding = 'F9'
        ctx.home.hotkey_label = mock.Mock()
        ctx.status._update_cards = mock.Mock()
        ctx.models._refresh_model_state = mock.Mock()
        ctx.preferences._poll_engine_settings = mock.Mock()
        ctx.preferences._save_finished(({'shortcut':'F8','ui_language':'en'}, True, 'ok'))
        ctx.home.hotkey_label.set_text.assert_called_once_with('F8')
        self.assertEqual(ctx.state.shortcut_binding, 'F9')
        self.assertEqual(ctx.state.cfg['shortcut'], 'F8')

    def test_setup_completion_does_not_paint_different_selected_engine(self):
        ctx = self.ctx
        ctx.preferences._selected_engine = lambda: 'whisper-cpp'
        ctx.settings.engine_status_row = mock.Mock()
        ctx.preferences._setup_finished('faster-whisper', True)
        ctx.settings.engine_status_row.set_subtitle.assert_not_called()
        self.assertFalse(ctx.preferences._setup_running)

    def test_diagnostics_do_not_block_click_and_complete_on_main_loop(self):
        ctx = self.ctx
        glib = FakeGLib()
        ctx.tasks = TaskRunner(glib)
        self.addCleanup(ctx.tasks.close)
        entered, release = threading.Event(), threading.Event()
        where = []
        def report():
            where.append(threading.current_thread())
            entered.set()
            release.wait(2)
            return 'report'
        ctx.status._diagnostics_text = report
        with mock.patch.object(ctx.status, '_diagnostics_preview') as preview, mock.patch('wayvoice.ui.controllers.status.injector.copy_to_clipboard') as copy:
            ctx.status._copy_diagnostics()
            self.assertTrue(entered.wait(2))
            self.assertIsNot(where[0], threading.current_thread())
            ctx.window._toast.assert_not_called()
            ctx.status._copy_diagnostics()
            self.assertEqual(len(where), 1)
            release.set()
            self.assertTrue(glib.ready.wait(2))
            glib.drain()
            preview.assert_called_once_with('report')
            copy.assert_not_called()
        ctx.window._toast.assert_not_called()

    def test_model_refresh_thread_failure_restores_flags(self):
        ctx = self.ctx
        ctx.settings.model_state_row = mock.Mock()
        ctx.models._selected_model_id = lambda: 'small'
        glib = FakeGLib()
        ctx.tasks = TaskRunner(glib)
        with mock.patch('threading.Thread', side_effect=RuntimeError('no worker')):
            ctx.models._refresh_model_state()
        self.assertFalse(ctx.models._model_refresh_busy)
        ctx.window._toast.assert_called_once_with(ctx.state.t('health.backend_error'))
        self.assertEqual(ctx.status._action_error, 'no worker')

    def test_painting_model_state_never_walks_cache(self):
        ctx = self.ctx
        for name in ('model_state_row','model_disk_row','model_delete_btn','model_fetch_btn'):
            setattr(ctx.settings,name,mock.Mock())
        with mock.patch('wayvoice.model_store.hub_size', side_effect=AssertionError('GTK scan')):
            ctx.models._apply_model_state({'id':'small','kind':'repo','downloaded':True,'size_bytes':10},20,10,{},30)

    def test_overlapping_status_polls_start_only_one_probe(self):
        tasks = self.ctx.tasks = ControlledTasks()
        self.ctx.status._poll_status()
        self.ctx.status._poll_status()
        self.assertEqual(len(tasks.pending),1)
