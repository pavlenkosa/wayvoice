"""Behavioral characterization of the owned settings controller units."""

from tests.ui_support import controller_context

import unittest
from unittest import mock

try:
    from wayvoice import ui
    from wayvoice.ui.controllers.settings import SettingsController
    from wayvoice.ui.controllers.models import ModelsController
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")


@needs_window
class SelectedEngineTests(unittest.TestCase):
    """The dropdown index maps onto the engine registry, not onto a guess."""

    def _window(self):
        window = controller_context()

        class FakeCombo:
            def __init__(self, selected):
                self._selected = selected

            def get_selected(self):
                return self._selected

        window.settings.engine = FakeCombo(1)
        return window

    def test_the_selected_index_names_the_engine(self):
        window = self._window()
        with mock.patch("wayvoice.ui.controllers.settings.engine_ids",
                        return_value=["faster-whisper", "whisper-cpp"]):
            self.assertEqual(window.preferences._selected_engine(), "whisper-cpp")


class FakeCombo:
    def __init__(self, selected):
        self._selected = selected

    def get_selected(self):
        return self._selected


class FakeEntry:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


@needs_window
class SaveGuardTests(unittest.TestCase):
    """A custom model without a path is refused before anything is saved."""

    def _window(self):
        window = controller_context()
        window.state.t = lambda key, **kwargs: key
        window.window.toast = mock.Mock()
        window.state.cfg = {}
        return window

    def test_an_empty_custom_model_path_is_refused(self):
        window = self._window()
        window.settings.custom_model = FakeEntry("   ")
        with (
            mock.patch.object(ModelsController, "_selected_model_preset",
                              return_value={"id": "__custom__"}),
            mock.patch.object(SettingsController, "_selected_engine_uses_models",
                              return_value=True),
            mock.patch("wayvoice.ui.controllers.settings.Adw") as adw,
            mock.patch("wayvoice.ui.controllers.settings.save_config") as save_config,
        ):
            window.preferences._save()
        self.assertEqual(save_config.call_count, 0)
        window.window.toast.add_toast.assert_called_once_with(adw.Toast.return_value)


@needs_window
class UnsavedNavigationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = controller_context()
        self.controller = self.ctx.preferences
        self.draft = {"model": "small", "ui_language": "en", "shortcut": "F8"}
        self.controller._draft_values = lambda: dict(self.draft)
        self.controller.remember_draft()
        self.ctx.window._toast = mock.Mock()
        self.ctx.home.hotkey_label = mock.Mock()
        self.controller._poll_engine_settings = mock.Mock()
        self.ctx.models._refresh_model_state = mock.Mock()
        self.ctx.status._update_cards = mock.Mock()

    def test_draft_change_revert_and_navigation_stay(self):
        self.assertFalse(self.controller.has_unsaved_changes())
        self.draft["model"] = "medium"
        self.assertTrue(self.controller.has_unsaved_changes())
        self.ctx.window._previous_page = "settings"
        self.ctx.window.save_button = mock.Mock()
        self.ctx.window.stack = mock.Mock()
        self.ctx.window.stack.get_visible_child_name.return_value = "home"
        with mock.patch("wayvoice.ui.dialogs.confirmations.unsaved_confirmation") as dialog:
            self.ctx.window._save_button_visibility()
            self.ctx.window.stack.set_visible_child_name.assert_called_once_with("settings")
            dialog.call_args.args[-1]("stay")
        self.assertTrue(self.controller.has_unsaved_changes())
        self.draft["model"] = "small"
        self.assertFalse(self.controller.has_unsaved_changes())

    def test_save_navigation_waits_for_success_and_preserves_later_edits(self):
        for later_edit in (False, True):
            with self.subTest(later_edit=later_edit):
                self.draft["model"] = "medium"
                proceed = mock.Mock()
                with mock.patch("wayvoice.ui.dialogs.confirmations.unsaved_confirmation") as dialog, mock.patch.object(self.controller, "_save", return_value=True):
                    self.controller.confirm_leaving(proceed)
                    dialog.call_args.args[-1]("save")
                proceed.assert_not_called()
                self.controller._saved_draft = dict(self.draft)
                if later_edit:
                    self.draft["model"] = "large-v3"
                self.controller._save_finished((dict(self.controller._saved_draft), True, ""))
                self.assertEqual(proceed.call_count, 0 if later_edit else 1)
                self.assertEqual(self.controller.has_unsaved_changes(), later_edit)

    def test_language_restart_does_not_discard_edits_during_save(self):
        self.draft["ui_language"] = "ru"
        self.controller._saved_draft = dict(self.draft)
        self.draft["model"] = "medium"
        proceed = mock.Mock()
        self.controller._leave_after_save = proceed
        self.controller._save_finished((dict(self.controller._saved_draft), True, ""))
        self.assertIsNone(self.controller._restart_command)
        self.assertTrue(self.controller.has_unsaved_changes())
        proceed.assert_not_called()
        self.controller._saved_draft = dict(self.draft)
        self.controller._save_finished((dict(self.draft), True, ""))
        self.assertIsNotNone(self.controller._restart_command)
        self.assertFalse(self.controller.has_unsaved_changes())

    def test_failed_save_stays_but_explicit_leave_keeps_draft(self):
        self.draft["model"] = "medium"
        proceed = mock.Mock()
        with mock.patch("wayvoice.ui.dialogs.confirmations.unsaved_confirmation") as dialog, mock.patch.object(self.controller, "_save", return_value=True):
            self.controller.confirm_leaving(proceed)
            dialog.call_args.args[-1]("save")
        self.controller._save_failed(OSError("disk full"))
        proceed.assert_not_called()
        self.assertTrue(self.controller.has_unsaved_changes())
        with mock.patch("wayvoice.ui.dialogs.confirmations.unsaved_confirmation") as dialog:
            self.controller.confirm_leaving(proceed)
            dialog.call_args.args[-1]("leave")
        proceed.assert_called_once()
        self.assertTrue(self.controller.has_unsaved_changes())


if __name__ == "__main__":
    unittest.main()