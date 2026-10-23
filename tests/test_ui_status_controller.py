"""Behavioral characterization of the owned status controller units."""

from tests.ui_support import GTK_AVAILABLE, controller_context

import unittest
from unittest import mock


if GTK_AVAILABLE:
    from wayvoice import ui
    from wayvoice.ui.controllers.status import StatusController

needs_window = unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")

@needs_window
class ToggleTests(unittest.TestCase):
    """The hotkey press asks the daemon to toggle or cancel, off the main loop."""

    def _window(self):
        window = controller_context()
        window.state.t = lambda key, **kwargs: key
        window.window.toast = mock.Mock()
        return window

    def test_idle_press_toggles(self):
        window = self._window()
        window.status._ui_busy = False
        with mock.patch("wayvoice.ui.controllers.status.request") as request:
            request.return_value = {"ok": True}
            window.status._toggle()
            request.assert_called_once_with("toggle-clipboard", timeout=0.8)

    def test_busy_press_cancels_and_a_failure_is_announced(self):
        window = self._window()
        window.status._ui_busy = True
        with mock.patch("wayvoice.ui.controllers.status.request") as request:
            request.return_value = {"ok": False, "error": "daemon down"}
            window.status._toggle()
            request.assert_called_once_with("cancel", timeout=0.8)
            window.window.toast.add_toast.assert_called_once()

if __name__ == "__main__":
    unittest.main()

@needs_window
class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        from wayvoice.ui.controllers import status
        self.module = status
        self.ctx = controller_context()
        self.controller = self.ctx.status
        self.ctx.window._toast = mock.Mock()
        self.ctx.state.t = lambda key: key
        self.clock = mock.patch.object(status.time, "monotonic", return_value=0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.restart = mock.patch.object(status.service, "restart_daemon", return_value=False)
        self.work = self.restart.start()
        self.addCleanup(self.restart.stop)
        self.old = {"ok": True, "version": "old"}

    def test_failure_cooldown_retry_and_limit(self):
        c = self.controller
        self.assertTrue(c._maybe_restart(self.old))
        self.assertFalse(c._restart_requested)
        self.assertFalse(c._maybe_restart(self.old))
        for tick in (5, 15):
            self.now.return_value = tick
            self.assertTrue(c._maybe_restart(self.old))
        self.now.return_value = 100
        self.assertFalse(c._maybe_restart(self.old))
        self.assertFalse(c._maybe_restart(self.old))
        self.assertEqual(self.work.call_count, 3)
        self.assertEqual(self.ctx.window._toast.call_args.args[0], "toast.restart_exhausted")

    def test_exception_releases_owner_for_retry(self):
        self.work.side_effect = RuntimeError("unit denied")
        self.controller._maybe_restart(self.old)
        self.assertFalse(self.controller._restart_requested)
        self.assertIn("unit denied", self.ctx.window._toast.call_args.args[0])
        self.now.return_value = 5
        self.assertTrue(self.controller._maybe_restart(self.old))

    def test_initial_offline_is_ignored_but_failed_episode_can_start(self):
        with mock.patch.object(self.module.service, "start_daemon", return_value=True) as start:
            self.assertFalse(self.controller._maybe_restart({"ok": False}))
            start.assert_not_called()
            self.controller._maybe_restart(self.old)
            self.now.return_value = 5
            self.assertTrue(self.controller._maybe_restart({"ok": False}))
            start.assert_called_once()

    def test_success_keeps_budget_until_current_version_confirmed(self):
        self.work.return_value = True
        self.controller._maybe_restart(self.old)
        self.assertEqual(self.controller._restart_attempts, 1)
        self.controller._maybe_restart({"ok": True, "version": self.module.__version__})
        self.assertEqual(self.controller._restart_attempts, 0)
        self.assertFalse(self.controller._restart_needed)

    def test_busy_recording_and_inflight_do_not_launch_again(self):
        for state in ("busy", "recording"):
            self.assertFalse(self.controller._maybe_restart(dict(self.old, **{state: True})))
        self.assertEqual(self.controller._restart_attempts, 0)
        self.ctx.tasks = mock.Mock()
        self.ctx.tasks.run.return_value = True
        self.assertTrue(self.controller._maybe_restart(self.old))
        self.assertFalse(self.controller._maybe_restart(self.old))
        self.assertEqual(self.ctx.tasks.run.call_count, 1)

    def test_preparation_defers_restart_without_consuming_retry_budget(self):
        for download in ({"state": "downloading"}, {"state": "warming"},
                         {"state": "downloading", "warming": True}):
            with self.subTest(download=download):
                reply = dict(self.old, model={"download": download})
                self.assertFalse(self.controller._maybe_restart(reply))
                self.assertTrue(self.controller._restart_needed)
                self.assertFalse(self.controller._restart_requested)
                self.assertEqual(self.controller._restart_attempts, 0)
                self.work.assert_not_called()
        self.assertTrue(self.controller._maybe_restart(
            dict(self.old, model={"download": {"state": "ready"}})))
        self.work.assert_called_once()

    def test_current_version_during_job_keeps_owner_and_suppresses_stale_error(self):
        self.ctx.tasks = mock.Mock()
        self.ctx.tasks.run.return_value = True
        self.controller._maybe_restart(self.old)
        self.controller._maybe_restart({"ok": True, "version": self.module.__version__})
        self.assertTrue(self.controller._restart_requested)
        done = self.ctx.tasks.run.call_args.args[1]
        done(False)
        self.ctx.window._toast.assert_not_called()
        self.assertFalse(self.controller._restart_requested)
        self.assertEqual(self.controller._restart_attempts, 0)

    def test_close_suppresses_late_restart_error(self):
        import threading
        from tests.ui_tasks import FakeGLib, TaskRunner
        glib = FakeGLib()
        runner = TaskRunner(glib)
        self.addCleanup(runner.close)
        self.ctx.tasks = runner
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def work():
            entered.set()
            release.wait(2)
            finished.set()
            return False
        self.work.side_effect = work
        self.assertTrue(self.controller._maybe_restart(self.old))
        self.assertTrue(entered.wait(2))
        runner.close()
        release.set()
        self.assertTrue(finished.wait(2))
        glib.drain()
        self.ctx.window._toast.assert_not_called()

    def test_offline_invalidates_confirmation_before_failed_completion(self):
        self.ctx.tasks = mock.Mock()
        self.ctx.tasks.run.return_value = True
        self.controller._maybe_restart(self.old)
        done = self.ctx.tasks.run.call_args.args[1]
        self.controller._maybe_restart({"ok": True, "version": self.module.__version__})
        self.controller._maybe_restart({"ok": False})
        done(False)
        self.assertTrue(self.controller._restart_needed)
        self.assertEqual(self.controller._restart_attempts, 1)
        self.ctx.window._toast.assert_called_once_with("toast.restart_failed")
        self.now.return_value = 5
        self.assertTrue(self.controller._maybe_restart({"ok": False}))


@needs_window
class StatusSnapshotTests(unittest.TestCase):
    def test_prepared_choices_and_painted_config_share_one_snapshot(self):
        from wayvoice.ui.controllers import status
        ctx = controller_context()
        ctx.selection = mock.Mock()
        ctx.selection.snapshot.return_value = [{"model": "small"}]
        cfg = {"engine": "faster-whisper", "model": "small"}
        with mock.patch.object(status, "request", return_value={"ok": True}), \
                mock.patch.object(status, "load_config", return_value=cfg) as load, \
                mock.patch.object(status, "shortcut_support", return_value=(True, "")), \
                mock.patch.object(status, "portal_shortcut_desktop", return_value=False), \
                mock.patch.object(ctx.integration, "_missing_required", return_value=[]):
            reply, painted_cfg, missing = ctx.status._status_snapshot()
        load.assert_called_once_with()
        ctx.selection.snapshot.assert_called_once_with(cfg)
        self.assertIs(painted_cfg, cfg)
        self.assertEqual(reply["_prepared_choices"], [{"model": "small"}])
        self.assertEqual(missing, [])
