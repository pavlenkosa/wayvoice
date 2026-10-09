import json
import unittest
from unittest import mock

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except (ImportError, ValueError):
    GI_AVAILABLE = False
else:
    GI_AVAILABLE = True
    from tests.ui_support import controller_context
    from wayvoice.ui.controllers import models


@unittest.skipUnless(GI_AVAILABLE, "GTK unavailable")
class ModelTargetTests(unittest.TestCase):
    def setUp(self):
        self.ctx = controller_context()
        self.controller = self.ctx.models
        self.ctx.preferences._selected_engine_object = lambda: mock.Mock(id="faster-whisper")
        self.controller._selected_model_id = lambda: "medium"
        self.ctx.window._toast = mock.Mock()
        self.controller._apply_download_state = mock.Mock()
        self.controller._refresh_model_state = mock.Mock()

    def test_confirmation_captures_target_before_later_selection_changes(self):
        with mock.patch("wayvoice.ui.dialogs.confirmations.download_confirmation") as dialog:
            self.controller._ask_about_download("MyOrg/MyModel", 20)
        confirmed = dialog.call_args.args[-1]
        self.controller._selected_model_id = lambda: "large-v3"
        self.ctx.preferences._selected_engine_object = lambda: mock.Mock(id="whisper-cpp")
        with mock.patch.object(self.controller, "_ask_daemon_to_prepare_model") as ask:
            win = mock.Mock()
            confirmed(None, win)
        ask.assert_called_once_with("MyOrg/MyModel", "faster-whisper")
        win.close.assert_called_once()

    def test_request_transmits_draft_target_not_saved_config(self):
        self.ctx.state.cfg["model"] = "small"
        with mock.patch.object(models, "request", return_value={"ok": True, "state": "downloading"}) as ask:
            self.controller._ask_daemon_to_prepare_model("MyOrg/MyModel", "faster-whisper")
        command = ask.call_args.args[0]
        name, payload = command.split(" ", 1)
        self.assertEqual(name, "prepare-model")
        self.assertEqual(json.loads(payload), {"engine": "faster-whisper", "model": "MyOrg/MyModel"})
        self.assertEqual(self.ctx.state.cfg["model"], "small")

    def test_reverse_replies_cannot_overwrite_newer_operation(self):
        jobs = []
        self.ctx.tasks.run = lambda work, done, failed: jobs.append((work, done))
        self.controller._ask_daemon_to_prepare_model("small", "faster-whisper")
        self.controller._ask_daemon_to_prepare_model("medium", "faster-whisper")
        jobs[1][1]({"ok": True, "state": "downloading"})
        jobs[0][1]({"ok": True, "state": "downloading"})
        self.assertEqual(self.controller._download_report["download"]["model"], "medium")
        self.controller._apply_download_state.assert_called_once()

    def test_ready_files_reply_does_not_invent_download_or_warming(self):
        self.controller._handle_prepare_model_reply({"ok": True, "state": "ready"}, "medium", "faster-whisper")
        self.controller._apply_download_state.assert_not_called()
        self.controller._refresh_model_state.assert_called_once()

    def test_selection_change_keeps_global_progress_named_for_captured_target(self):
        jobs = []
        self.ctx.tasks.run = lambda work, done, failed: jobs.append(done)
        self.controller._selected_model_id = lambda: "small"
        self.controller._ask_daemon_to_prepare_model("small", "faster-whisper")
        self.controller._selected_model_id = lambda: "medium"
        jobs[0]({"ok": True, "state": "downloading"})
        self.controller._apply_download_state.assert_called_once()
        self.assertEqual(self.controller._apply_download_state.call_args.args[0]["download"]["model"], "small")
        self.assertEqual(self.controller._selected_model_id(), "medium")

    def test_incomplete_cached_weights_require_confirmation_before_request(self):
        self.controller._download_confirmation_for = "medium"
        with mock.patch.object(self.controller, "_ask_about_download") as confirm, mock.patch.object(self.controller, "_ask_daemon_to_prepare_model") as ask:
            self.controller._decide_what_to_do_about_the_selected_model({
                "id": "medium", "kind": "repo", "downloaded": True,
                "inference_ready": False, "size_bytes": 20})
        confirm.assert_called_once_with("medium", 20)
        ask.assert_not_called()

    def test_worker_adds_inference_readiness_without_changing_disk_predicate(self):
        entry = {"id": "medium", "kind": "repo", "downloaded": True}
        with mock.patch.object(models.model_store, "describe", return_value=entry), mock.patch.object(models.model_store, "inference_dir", side_effect=RuntimeError("missing tokenizer")), mock.patch.object(models.model_store, "disk_free", return_value=0), mock.patch.object(models.model_store, "total_size", return_value=0), mock.patch.object(models.model_store, "hub_size", return_value=0), mock.patch.object(models, "worker_info", return_value={}), mock.patch.object(self.controller, "_model_state_ready") as ready:
            self.controller._model_state_worker("medium")
        self.assertTrue(ready.call_args.args[0]["downloaded"])
        self.assertFalse(ready.call_args.args[0]["inference_ready"])

    def test_draft_progress_and_completion_refresh_without_restart(self):
        controller = self.controller
        self.ctx.settings.model_download_row = mock.Mock()
        self.ctx.settings.model_download_bar = mock.Mock()
        self.ctx.settings.model_download_cancel_btn = mock.Mock()
        apply = lambda report: models.ModelsController._apply_download_state(controller, report)
        apply({"model": "small", "download": {"model": "medium", "state": "downloading", "done_bytes": 5, "total_bytes": 10}})
        self.ctx.settings.model_download_row.set_visible.assert_called_with(True)
        self.ctx.settings.model_download_bar.set_fraction.assert_called_once_with(0.5)
        ready = {"model": "small", "download": {"model": "medium", "state": "ready"}}
        apply(ready)
        apply(ready)
        controller._refresh_model_state.assert_called_once()

    def test_old_cache_scan_cannot_replace_selected_model(self):
        with mock.patch.object(self.controller, "_apply_model_state") as paint:
            self.controller._model_state_ready({"id": "small"}, 0, 0, {})
        paint.assert_not_called()
        self.controller._refresh_model_state.assert_called_once()
