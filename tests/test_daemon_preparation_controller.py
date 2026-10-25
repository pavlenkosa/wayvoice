import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from wayvoice.daemon_preparation import ModelPreparationController


class PreparationControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = ModelPreparationController(threading.RLock(), threading.Event(), lambda: False)

    def test_thread_constructor_failure_releases_preparation(self):
        with mock.patch('wayvoice.daemon_preparation.engine_from_config', return_value=SimpleNamespace(id='test')), \
             mock.patch('wayvoice.daemon_preparation.model_state', return_value={'supported': True, 'present': False, 'model': 'small'}), \
             mock.patch('wayvoice.daemon_preparation.threading.Thread', side_effect=RuntimeError('no thread')):
            self.assertEqual(self.controller.start({'model': 'small'}), '')
        self.assertFalse(self.controller.running)
        self.assertIsNone(self.controller.thread)
        self.assertEqual(self.controller.report['state'], 'error')

    def test_configuration_snapshot_is_detached_and_readonly(self):
        cfg = {'model': 'small', 'vocabulary': ['original']}
        with mock.patch('wayvoice.daemon_preparation.engine_from_config', return_value=SimpleNamespace(id='test')), \
             mock.patch('wayvoice.daemon_preparation.model_state', return_value={'supported': True, 'present': False, 'model': 'small'}), \
             mock.patch('wayvoice.daemon_preparation.threading.Thread') as thread:
            self.controller.start(cfg)
        snapshot = thread.call_args.kwargs['args'][0]
        cfg['vocabulary'].append('changed')
        self.assertEqual(snapshot['vocabulary'], ['original'])
        with self.assertRaises(TypeError):
            snapshot['model'] = 'medium'

    def test_late_thread_after_shutdown_never_prepares_runtime(self):
        self.controller._shutdown.set()
        with mock.patch('wayvoice.daemon_preparation.prepare_model') as prepare:
            self.controller.run({}, {}, True)
        prepare.assert_not_called()
        self.assertFalse(self.controller.running)
        self.assertEqual(self.controller.report['state'], 'cancelled')

    def test_old_operation_callbacks_and_completion_cannot_replace_new_report(self):
        self.controller.running = True
        self.controller.report = {'state': 'downloading', 'operation_id': 'old'}
        def prepare(engine, cfg, progress, cancel, warming, download):
            self.controller.report = {'state': 'downloading', 'operation_id': 'new', 'done_bytes': 7}
            progress(99, 100)
            warming()
            return {'state': 'ready'}
        with mock.patch('wayvoice.daemon_preparation.engine_from_config'), \
             mock.patch('wayvoice.daemon_preparation.prepare_model', side_effect=prepare):
            self.controller.run({}, {'operation_id': 'old'}, True)
        self.assertEqual(self.controller.report, {'state': 'downloading', 'operation_id': 'new', 'done_bytes': 7})
        self.assertTrue(self.controller.running)

    def test_malformed_result_counts_do_not_strand_preparation(self):
        self.controller.running = True
        with mock.patch('wayvoice.daemon_preparation.engine_from_config'), \
             mock.patch('wayvoice.daemon_preparation.prepare_model', return_value={'done': 'invalid'}):
            self.controller.run({}, {}, True)
        self.assertFalse(self.controller.running)
        self.assertIsNone(self.controller.thread)
        self.assertEqual(self.controller.report['state'], 'error')

    def test_base_exception_propagates_but_still_releases_preparation(self):
        # A `return` inside the `finally` used to swallow everything, hiding even
        # the interruption itself; the cleanup must run and the exception must
        # still surface instead of vanishing into the thread.
        self.controller.running = True
        self.controller.report['operation_id'] = 'current'
        with mock.patch('wayvoice.daemon_preparation.engine_from_config'), \
             mock.patch('wayvoice.daemon_preparation.prepare_model',
                        side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.run({}, {'operation_id': 'current'}, True)
        self.assertFalse(self.controller.running)
        self.assertIsNone(self.controller.thread)
        self.assertEqual(self.controller.report['state'], 'cancelled')

    def test_base_exception_of_superseded_operation_leaves_new_report_alone(self):
        self.controller.running = True
        self.controller.report = {'state': 'downloading', 'operation_id': 'old'}
        def prepare(engine, cfg, progress, cancel, warming, download):
            self.controller.report = {'state': 'downloading', 'operation_id': 'new'}
            raise KeyboardInterrupt
        with mock.patch('wayvoice.daemon_preparation.engine_from_config'), \
             mock.patch('wayvoice.daemon_preparation.prepare_model', side_effect=prepare):
            with self.assertRaises(KeyboardInterrupt):
                self.controller.run({}, {'operation_id': 'old'}, True)
        self.assertEqual(self.controller.report, {'state': 'downloading', 'operation_id': 'new'})
        self.assertTrue(self.controller.running)
