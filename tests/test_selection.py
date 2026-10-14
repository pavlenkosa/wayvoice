"""Quick selection is an idle-only transaction and never authorizes downloads."""
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from tests.support import isolate_environment
from wayvoice import selection
from wayvoice.config import load_config, save_config


class SelectionTests(unittest.TestCase):
    def setUp(self):
        isolate_environment(self)
        self.cfg = load_config()
        save_config(self.cfg)
        self.daemon = SimpleNamespace(_lock=threading.RLock(), _shutdown=threading.Event(),
                                     recorder=SimpleNamespace(recording=False), busy=False,
                                     preparation=mock.Mock(running=False), _model_maintenance=None)
        self.daemon.preparation.start.return_value = 'warming'
        self.daemon.preparation.snapshot.return_value = {'state': 'warming'}
        patch = mock.patch.object(selection, 'prepared_choices', side_effect=lambda cfg: [dict(engine=cfg['engine'], model=cfg['model'], forced_language=None)])
        patch.start()
        self.addCleanup(patch.stop)
        self.payload = {'changes': {'model': 'medium'}, 'expected': {key:self.cfg[key] for key in selection.FIELDS}, 'expires': time.monotonic() + 10}

    def test_success_merges_only_changed_fields_and_warms_without_download(self):
        cfg = dict(self.cfg, notify=False)
        save_config(cfg)
        reply = selection.select_prepared(self.daemon, self.payload)
        self.assertTrue(reply['ok'])
        self.assertFalse(load_config()['notify'])
        self.assertEqual(load_config()['model'], 'medium')
        self.daemon.preparation.start.assert_called_once_with(reply['cfg'], download=False)

    def test_busy_recording_preparation_and_update_refuse_without_writing(self):
        for owner, field in [(self.daemon, 'busy'), (self.daemon.recorder, 'recording'), (self.daemon.preparation, 'running'), (self.daemon, '_model_maintenance')]:
            with self.subTest(field=field):
                setattr(owner, field, True)
                reply = selection.select_prepared(self.daemon, self.payload)
                self.assertTrue(reply['busy'])
                setattr(owner, field, False)
        self.assertEqual(load_config()['model'], self.cfg['model'])
        self.daemon.preparation.start.assert_not_called()
        with mock.patch('wayvoice.updater.updating', return_value=True):
            self.assertTrue(selection.select_prepared(self.daemon, self.payload)['busy'])

    def test_conflict_never_warms(self):
        save_config(dict(self.cfg, model='tiny'))
        reply = selection.select_prepared(self.daemon, self.payload)
        self.assertFalse(reply['ok'])
        self.assertIn('model', reply['error'])
        self.assertEqual(load_config()['model'], 'tiny')
        self.daemon.preparation.start.assert_not_called()

    def test_missing_model_never_saves_or_downloads(self):
        with mock.patch.object(selection, 'prepared_choices', return_value=[]):
            self.assertFalse(selection.select_prepared(self.daemon, self.payload)['ok'])
        self.assertEqual(load_config()['model'], self.cfg['model'])
        self.daemon.preparation.start.assert_not_called()

    def test_expired_or_unrelated_changes_are_rejected(self):
        for payload in (dict(self.payload, expires=time.monotonic()-1), dict(self.payload, changes={'device':'cuda'}, expected={'device':'cpu'})):
            self.assertFalse(selection.select_prepared(self.daemon, payload)['ok'])
        self.daemon.preparation.start.assert_not_called()

    def test_corrupt_config_preserves_raw_file(self):
        from wayvoice.config import config_path
        config_path().write_text('{broken')
        self.assertFalse(selection.select_prepared(self.daemon, self.payload)['ok'])
        self.assertEqual(config_path().read_text(), '{broken')
        self.daemon.preparation.start.assert_not_called()

    def test_external_dependency_change_during_transaction_is_rejected(self):
        actual_update = selection.update_config
        def race(changes, expected, **kwargs):
            save_config(dict(load_config(), language='ru'))
            return actual_update(changes, expected, **kwargs)
        with mock.patch.object(selection, 'update_config', side_effect=race):
            reply = selection.select_prepared(self.daemon, self.payload)
        self.assertFalse(reply['ok'])
        self.assertIn('language', reply['error'])
        self.assertEqual(load_config()['model'], self.cfg['model'])
        self.daemon.preparation.start.assert_not_called()


    def test_selection_expiring_during_local_probes_is_not_committed(self):
        payload = dict(self.payload, expires=5010)
        with mock.patch.object(selection.time, 'monotonic', side_effect=[5000,5000,5011]):
            reply = selection.select_prepared(self.daemon, payload)
        self.assertFalse(reply['ok'])
        self.assertIn('expired', reply['error'])
        self.daemon.preparation.start.assert_not_called()
        self.assertEqual(load_config()['model'], self.cfg['model'])

    def test_expired_transaction_does_not_write_after_lock_acquisition(self):
        from wayvoice.config import config_path, update_config
        raw = config_path().read_bytes()
        with self.assertRaises(TimeoutError):
            update_config({'model':'tiny'}, {'model':self.cfg['model']}, deadline=time.monotonic()-1)
        self.assertEqual(config_path().read_bytes(), raw)


class PreparedChoicesTests(unittest.TestCase):
    def test_missing_incomplete_and_probe_failed_models_are_not_choices(self):
        for error in (RuntimeError('missing tokenizer'), OSError('inaccessible cache')):
            with self.subTest(error=error), mock.patch.object(selection, 'engine_status', side_effect=lambda cfg: {'state':'ready' if cfg['engine']=='faster-whisper' else 'missing'}), mock.patch.object(selection.model_store, 'inference_dir', side_effect=error):
                self.assertEqual(selection.prepared_choices({'engine':'faster-whisper','model':'/missing/local/model'}), [])

    def test_only_locally_inference_ready_models_are_listed(self):
        def local(model):
            if model != 'tiny':
                raise RuntimeError('incomplete')
            return '/prepared/tiny'
        with mock.patch.object(selection, 'engine_status', side_effect=lambda cfg: {'state':'ready' if cfg['engine']=='faster-whisper' else 'missing'}), mock.patch.object(selection.model_store, 'inference_dir', side_effect=local):
            choices = selection.prepared_choices({'model':'/missing/local/model'})
        self.assertEqual([c['model'] for c in choices], ['tiny'])

    def test_warm_failure_is_error_and_next_attempt_can_succeed(self):
        from wayvoice.engine import get_engine
        from wayvoice.runtime.model_preparation import prepare_model
        with mock.patch('wayvoice.runtime.worker_client.warm_worker', side_effect=[False,True]):
            first = prepare_model(get_engine('faster-whisper'), {'engine_worker':True}, download=False)
            second = prepare_model(get_engine('faster-whisper'), {'engine_worker':True}, download=False)
        self.assertEqual(first['state'], 'error')
        self.assertEqual(second['state'], 'ready')
