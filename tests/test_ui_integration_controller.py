"""Behavioral characterization of the owned integration controller units."""

from tests.ui_support import GTK_AVAILABLE, controller_context

import time
import unittest
from unittest import mock

from wayvoice import deps as deps_mod


if GTK_AVAILABLE:
    from wayvoice import ui
    from wayvoice.ui.controllers.integration import IntegrationController

needs_window = unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")

class FakeDep:
    def __init__(self, dep_id, label, required):
        self.id = dep_id
        self.label = label
        self.required = required

@needs_window
class MissingRequiredTests(unittest.TestCase):
    """``_missing_required`` filters to required-and-unusable dependencies."""

    def setUp(self):
        self.window = controller_context()

    def test_only_required_and_unusable_are_returned(self):
        deps = [
            FakeDep("a", "A", True),
            FakeDep("b", "B", False),
            FakeDep("c", "C", True),
        ]
        statuses = {
            "a": {"ok": False},
            "b": {"ok": False},   # optional: never reported
            "c": {"ok": True},    # required but fine: not missing
        }
        with mock.patch.object(deps_mod, "dependencies", return_value=deps), \
             mock.patch.object(deps_mod, "status_of", side_effect=lambda d: statuses[d.id]):
            self.assertEqual([d.id for d in self.window.integration._missing_required()], ["a"])

class FakeButton:
    def __init__(self):
        self.sensitive = True
        self.visible = None

    def set_sensitive(self, value):
        self.sensitive = value

    def set_visible(self, value):
        self.visible = value

class FakeSpinner:
    def __init__(self):
        self.visible = None
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def set_visible(self, value):
        self.visible = value

class FakeRow:
    def __init__(self):
        self.subtitle = ""
        self.visible = None

    def set_subtitle(self, text):
        self.subtitle = text

    def set_visible(self, value):
        self.visible = value

@needs_window
class RefreshThrottleTests(unittest.TestCase):
    """The dependency probe is throttled; force and installs bypass it."""

    def _window(self):
        window = controller_context()
        window.state.t = lambda key, **kwargs: key
        window.state.ui_lang = "en"
        window.integration._dep_rows = {}
        window.integration._dep_installing = set()
        window.integration._dep_last_refresh = time.monotonic()
        return window

    def test_a_recent_refresh_is_not_repeated(self):
        window = self._window()
        with mock.patch.object(deps_mod, "status_all", return_value=[]) as probe:
            window.integration._refresh_dependency_rows()
        probe.assert_not_called()

    def test_force_refreshes_even_when_recent(self):
        window = self._window()
        with mock.patch.object(deps_mod, "status_all", return_value=[]) as status_all:
            window.integration._refresh_dependency_rows(force=True)
            status_all.assert_called_once_with()

@needs_window
class DesktopIntegrationResultTests(unittest.TestCase):
    def test_setup_failure_is_returned_separately_from_daemon_failure(self):
        from wayvoice.ui.controllers import integration
        controller = IntegrationController(controller_context().integration.ctx)
        with mock.patch.object(integration.service, "start_daemon", return_value=False), \
             mock.patch.object(integration.service, "systemd_available", return_value=True), \
             mock.patch.object(integration, "_run_setup_user", return_value=(False, "unit denied")):
            result = controller._apply_desktop_integration_worker()
        self.assertIn("daemon did not come up", result)
        self.assertIn("unit denied", result)

    def test_worker_result_is_marshaled_by_window_runner(self):
        context = mock.Mock()
        controller = IntegrationController(context)
        controller._apply_desktop_integration()
        work, done, failed = context.tasks.run.call_args.args
        self.assertEqual(work, controller._apply_desktop_integration_worker)
        done("setup denied")
        context.window._toast.assert_called_once_with(context.state.t("health.backend_error"))
        self.assertEqual(context.status._action_error, "setup denied")

    def test_repeated_requests_queue_one_followup_even_after_failure(self):
        context = mock.Mock()
        context.tasks.run.return_value = True
        controller = IntegrationController(context)
        controller._apply_desktop_integration()
        for _ in range(5):
            controller._apply_desktop_integration()
        self.assertEqual(context.tasks.run.call_count, 1)
        context.tasks.run.call_args.args[2](RuntimeError("setup denied"))
        self.assertEqual(context.tasks.run.call_count, 2)
        self.assertTrue(controller._integration_running)
        context.tasks.run.call_args.args[1]("")
        self.assertFalse(controller._integration_running)
        self.assertFalse(controller._integration_pending)
        self.assertEqual(context.tasks.run.call_count, 2)

    def test_close_suppresses_pending_job_and_late_toast(self):
        import threading
        from tests.ui_tasks import FakeGLib, TaskRunner
        glib = FakeGLib()
        runner = TaskRunner(glib)
        self.addCleanup(runner.close)
        context = mock.Mock(tasks=runner)
        controller = IntegrationController(context)
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        def work():
            entered.set()
            release.wait(2)
            finished.set()
            return "setup denied"
        with mock.patch.object(controller, "_apply_desktop_integration_worker", side_effect=work) as worker:
            controller._apply_desktop_integration()
            self.assertTrue(entered.wait(2))
            controller._apply_desktop_integration()
            runner.close()
            release.set()
            self.assertTrue(finished.wait(2))
            glib.drain()
            self.assertEqual(worker.call_count, 1)
        context.window._toast.assert_not_called()

    def test_rejected_job_does_not_strand_running_state(self):
        context = mock.Mock()
        context.tasks.run.return_value = False
        controller = IntegrationController(context)
        controller._apply_desktop_integration()
        self.assertFalse(controller._integration_running)
        self.assertFalse(controller._integration_pending)

@needs_window
class HealthRefreshTests(unittest.TestCase):
    def setUp(self):
        self.ctx = controller_context()
        self.ctx.tasks = mock.Mock()
        self.ctx.tasks.run.return_value = True
        self.controller = self.ctx.integration
        self.ctx.state.cfg = {'shortcut': 'F8', 'paste_mode': 'copy'}

    def test_singleflight_throttle_and_explicit_retry(self):
        c = self.controller
        c.refresh_health()
        work, done, _failed = self.ctx.tasks.run.call_args.args
        c.refresh_health()
        self.assertEqual(self.ctx.tasks.run.call_count, 1)
        done({'delivery':'copy_only','reason':'copy_only'})
        c.refresh_health()
        self.assertEqual(self.ctx.tasks.run.call_count, 1)
        c.refresh_health(force=True)
        self.assertEqual(self.ctx.tasks.run.call_count, 2)

    def test_changed_configuration_discards_late_snapshot(self):
        c = self.controller
        c.refresh_health()
        done = self.ctx.tasks.run.call_args.args[1]
        self.ctx.state.cfg['shortcut'] = 'F9'
        done({'reason':'prerequisites'})
        self.assertIsNone(c.health)
        self.assertEqual(self.ctx.tasks.run.call_count, 2)
        self.ctx.tasks.run.call_args.args[1]({'reason':'copy_only'})
        self.assertEqual(c.health['reason'],'copy_only')

    def test_refused_job_can_be_retried(self):
        self.ctx.tasks.run.return_value = False
        self.controller.refresh_health()
        self.assertFalse(self.controller._health_running)
        self.ctx.tasks.run.return_value = True
        self.controller.refresh_health()
        self.assertTrue(self.controller._health_running)
