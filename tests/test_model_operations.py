"""Maintenance and captured cancellation are daemon-owned, without live audio."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wayvoice import daemon as mod, daemon_models


class ModelOperationsTests(unittest.TestCase):
    def setUp(self):
        self.d = mod.WayVoiceDaemon()
        patch = mock.patch.object(daemon_models.model_store, '_check_deletable')
        patch.start()
        self.addCleanup(patch.stop)

    def delete(self):
        return self.d.dispatch('delete-model ' + json.dumps({'model': 'small'}))

    def test_captured_cancel_never_cancels_a_newer_operation(self):
        self.d._prepare_running = True
        self.d._download = {'state': 'warming', 'model': 'small', 'operation_id': 'new'}
        stale = self.d.dispatch('cancel-download {"operation_id":"old"}')
        self.assertFalse(stale['ok'])
        self.assertFalse(self.d._prepare_cancel.is_set())
        reply = self.d.dispatch('cancel-download {"operation_id":"new"}')
        self.assertTrue(reply['cancelling'])
        self.assertFalse(reply['stopped'])
        self.assertTrue(self.d._prepare_cancel.is_set())

    def test_busy_and_recording_and_preparing_refuse_deletion(self):
        for state in ('busy', '_prepare_running', 'recording'):
            with self.subTest(state=state), mock.patch.object(daemon_models.model_store, 'delete') as delete:
                if state == 'recording':
                    with mock.patch.object(self.d.recorder.__class__, 'recording', new_callable=mock.PropertyMock, return_value=True):
                        self.assertFalse(self.delete()['ok'])
                else:
                    setattr(self.d, state, True)
                    self.assertFalse(self.delete()['ok'])
                    setattr(self.d, state, False)
                delete.assert_not_called()

    def test_maintenance_blocks_new_recording_and_prepare_then_unwinds(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def delete(_model):
            entered.set()
            release.wait(2)
            raise OSError('filesystem refused')
        with mock.patch.object(daemon_models.engine, '_retry_unresolved_workers'), mock.patch.object(daemon_models.engine, '_worker_ping', return_value=None), mock.patch.object(daemon_models.engine, 'worker_pid_path', return_value=Path('/nonexistent-wayvoice-test.pid')), mock.patch.object(daemon_models.model_store, 'delete', side_effect=delete):
            thread = threading.Thread(target=lambda: result.append(self.delete()))
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                self.assertFalse(self.d.start_recording()['ok'])
                self.assertEqual(self.d._start_model_prepare({}), '')
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result[0]['ok'])
        self.assertIsNone(self.d._model_maintenance)

    def test_surviving_pinned_worker_prevents_delete(self):
        reply = {'pid': 123, 'config': {'model': 'small'}}
        with mock.patch.object(daemon_models.engine, '_retry_unresolved_workers'), mock.patch.object(daemon_models.engine, '_worker_ping', return_value=reply), mock.patch.object(daemon_models.engine, '_worker_identity', return_value=(123, 'start')), mock.patch.object(daemon_models.engine, '_pid_alive', return_value=True), mock.patch.object(daemon_models.engine, 'stop_worker') as stop, mock.patch.object(daemon_models.model_store, 'delete') as delete:
            self.assertFalse(self.delete()['ok'])
        stop.assert_called_once_with(timeout=0.5, expected_identity=(123, 'start'))
        delete.assert_not_called()
        self.assertIsNone(self.d._model_maintenance)

    def test_button_delivery_is_copy_only_without_mutating_config(self):
        cfg = {'paste_mode': 'standard', 'notify': False}
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / 'take.wav'
            wav.write_bytes(b'audio')
            with mock.patch.object(mod, 'load_config', return_value=cfg), mock.patch.object(mod, 'transcribe', return_value='hello'), mock.patch.object(mod, 'inject', return_value=SimpleNamespace(warning='')) as inject, mock.patch.object(mod, 'notify'), mock.patch.object(mod, 'reset_notification_id'):
                self.d._transcribe_worker(wav, delivery_mode='copy')
        self.assertEqual(inject.call_args.args[1]['paste_mode'], 'copy')
        self.assertEqual(cfg['paste_mode'], 'standard')

    def test_clear_text_only_clears_current_text(self):
        self.d.last_text = 'private text'
        self.d.busy = True
        self.assertTrue(self.d.dispatch('clear-text')['ok'])
        self.assertEqual(self.d.last_text, '')
        self.assertTrue(self.d.busy)


    def test_dead_pinned_worker_is_stopped_before_deletion(self):
        reply = {'pid': 123, 'config': {'model': 'small'}}
        with mock.patch.object(daemon_models.engine, '_retry_unresolved_workers'), mock.patch.object(daemon_models.engine, '_worker_ping', return_value=reply), mock.patch.object(daemon_models.engine, '_worker_identity', side_effect=[(123, 'start'), None]), mock.patch.object(daemon_models.engine, 'stop_worker') as stop, mock.patch.object(daemon_models.model_store, 'delete', return_value={'ok': True}) as delete:
            self.assertTrue(self.delete()['ok'])
        stop.assert_called_once_with(timeout=0.5, expected_identity=(123, 'start'))
        delete.assert_called_once_with('small')

    def test_unresponsive_live_worker_refuses_deletion(self):
        with mock.patch.object(daemon_models.engine, '_retry_unresolved_workers'), mock.patch.object(daemon_models.engine, '_worker_ping', return_value=None), mock.patch.object(daemon_models.engine, 'worker_pid_path') as path, mock.patch.object(daemon_models.engine, '_pid_alive', return_value=True), mock.patch.object(daemon_models.model_store, 'delete') as delete:
            path.return_value.read_text.return_value = '123'
            self.assertFalse(self.delete()['ok'])
        delete.assert_not_called()

    def test_toggle_clipboard_routes_explicit_override_without_saving(self):
        with mock.patch.object(self.d, 'start_recording', return_value={'ok': True}) as start:
            self.d.dispatch('toggle-clipboard')
        start.assert_called_once_with(delivery_mode='copy')
        with mock.patch.object(self.d.recorder.__class__, 'recording', new_callable=mock.PropertyMock, return_value=True), mock.patch.object(self.d, 'stop_recording', return_value={'ok': True}) as stop:
            self.d.dispatch('toggle-clipboard')
        stop.assert_called_once_with(delivery_mode='copy')
