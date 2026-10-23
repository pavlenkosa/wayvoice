"""Background checks require saved consent and cannot revive stale notices."""
import unittest
from types import SimpleNamespace
from unittest import mock
from tests.ui_support import GTK_AVAILABLE

if GTK_AVAILABLE:
    from wayvoice.ui.controllers.updates import UpdatesController


@unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")
class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace(tasks=mock.Mock(closed=False), window=SimpleNamespace(_update_busy=False),
            home=SimpleNamespace(update_banner=mock.Mock()),
            state=SimpleNamespace(t=lambda key, **kw: f"{key}:{kw.get('version', '')}"))
        self.controller = UpdatesController(self.ctx)
        self.clock = mock.patch('wayvoice.ui.controllers.updates.time.monotonic', return_value=100)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def enable(self):
        self.controller.configure({'auto_check_updates': True})
        return self.ctx.tasks.run.call_args.args

    def test_saved_opt_in_one_job_and_six_hour_interval(self):
        for value in (False, None, 'true', 1):
            self.controller.configure({'auto_check_updates': value})
        self.ctx.tasks.run.assert_not_called()
        work, done, failed = self.enable()
        for _ in range(5):
            self.controller.tick()
        self.ctx.tasks.run.assert_called_once()
        done({'available': True, 'version': '0.7.0'})
        self.ctx.home.update_banner.set_revealed.assert_called_with(True)
        self.controller.tick()
        self.ctx.tasks.run.assert_called_once()
        with mock.patch('wayvoice.ui.controllers.updates.time.monotonic', return_value=100 + 6 * 3600):
            self.controller.tick()
        self.assertEqual(self.ctx.tasks.run.call_count, 2)

    def test_disable_and_reenable_drops_old_result_without_overlapping_requests(self):
        _, done, _ = self.enable()
        self.controller.configure({'auto_check_updates': False})
        self.controller.configure({'auto_check_updates': True})
        self.ctx.tasks.run.assert_called_once()
        done({'available': True, 'version': '0.7.0'})
        self.ctx.home.update_banner.set_revealed.assert_called_with(False)
        self.assertEqual(self.ctx.tasks.run.call_count, 2)

    def test_manual_check_supersedes_pending_automatic_result(self):
        _, done, _ = self.enable()
        self.controller.checked({'available': True, 'version': '0.8.0'})
        done({'available': True, 'version': '0.7.0'})
        self.ctx.home.update_banner.set_title.assert_called_once_with('update.notice:0.8.0')
        self.ctx.tasks.run.assert_called_once()
        self.controller.checked({'available': False})
        self.ctx.home.update_banner.set_revealed.assert_called_with(False)

    def test_failure_is_quiet_and_close_suppresses_notice(self):
        _, _, failed = self.enable()
        failed(RuntimeError('offline'))
        self.ctx.home.update_banner.set_revealed.assert_not_called()
        self.assertEqual(self.controller.next_check, 100 + 3600)
        with mock.patch('wayvoice.ui.controllers.updates.time.monotonic', return_value=3700):
            self.controller.tick()
        self.ctx.tasks.closed = True
        self.ctx.tasks.run.call_args.args[1]({'available': True, 'version': '0.7.0'})
        self.ctx.home.update_banner.set_revealed.assert_not_called()
