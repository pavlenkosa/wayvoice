"""GLib ownership tests run even when GI is absent."""
import importlib.util
import threading
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('wayvoice_ui_async_tasks', Path(__file__).resolve().parents[1] / 'app/src/wayvoice/ui/async_tasks.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TaskRunner = module.TaskRunner


class FakeGLib:
    def __init__(self):
        self.sources = {}
        self.removed = []
        self.counter = 0
        self.ready = threading.Event()

    def idle_add(self, callback):
        self.counter += 1
        self.sources[self.counter] = callback
        self.ready.set()
        return self.counter

    def timeout_add(self, _ms, callback):
        return self.idle_add(callback)

    def source_remove(self, source):
        self.removed.append(source)
        self.sources.pop(source, None)

    def fire(self, source):
        if not self.sources[source]():
            self.sources.pop(source, None)

    def drain(self):
        for source in list(self.sources):
            self.fire(source)


class TaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.glib = FakeGLib()
        self.runner = TaskRunner(self.glib)
        self.addCleanup(self.runner.close)

    def test_background_work_and_main_loop_completion_have_distinct_owners(self):
        caller = threading.current_thread()
        where = []
        done = []
        self.runner.run(lambda: where.append(threading.current_thread()) or 42,
                        lambda result: done.append((threading.current_thread(), result)))
        self.assertTrue(self.glib.ready.wait(2))
        self.assertIsNot(where[0], caller)
        self.assertEqual(done, [])
        self.glib.drain()
        self.assertEqual(done, [(caller,42)])
        self.assertEqual(self.runner._sources, set())

    def test_close_cancels_sources_and_late_completion(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        done = mock.Mock()
        def work():
            entered.set()
            release.wait(2)
            finished.set()
            return 1
        self.runner.every(650, lambda: True)
        self.runner.run(work, done)
        self.assertTrue(entered.wait(2))
        self.runner.close()
        release.set()
        self.assertTrue(finished.wait(2))
        self.runner.idle(done, 2)
        self.assertEqual(self.glib.sources, {})
        done.assert_not_called()
        self.assertEqual(self.runner._sources, set())

    def test_queued_completion_is_not_called_after_close(self):
        done = mock.Mock()
        source = self.runner.idle(done)
        queued_callback = self.glib.sources[source]
        self.runner.close()
        self.assertFalse(queued_callback())
        done.assert_not_called()

    def test_failing_repeating_callback_does_not_leave_stale_source(self):
        source = self.runner.every(650, mock.Mock(side_effect=RuntimeError('failed poll')))
        with self.assertLogs(level='ERROR'):
            self.glib.fire(source)
        self.assertEqual(self.runner._sources, set())
        self.runner.close()
        self.assertNotIn(source, self.glib.removed)

    def test_thread_start_failure_is_reported_synchronously(self):
        failure = mock.Mock()
        with mock.patch.object(module.threading, 'Thread', side_effect=RuntimeError('no worker')):
            self.assertFalse(self.runner.run(lambda: None, mock.Mock(), failure))
        self.assertEqual(str(failure.call_args.args[0]), 'no worker')

    def test_background_failure_returns_to_main_loop(self):
        failure = mock.Mock()
        def work():
            raise ValueError('probe failed')
        self.runner.run(work, mock.Mock(), failure)
        self.assertTrue(self.glib.ready.wait(2))
        failure.assert_not_called()
        self.glib.drain()
        self.assertEqual(str(failure.call_args.args[0]), 'probe failed')
