"""Behavioral characterization of the owned settings controller units."""

from tests.ui_support import controller_context
try:
    from gi.repository import GLib
    from wayvoice.ui.controllers.status import StatusController
    from wayvoice.ui.controllers.models import ModelsController
except Exception:
    pass

import ast
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

try:
    from wayvoice import ui
    from wayvoice.ui.controllers.settings import SettingsController
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")

#: The methods phase 2E moved out of the legacy class body.
SETTINGS_METHODS = (
    "_on_engine_selected", "_selected_engine", "_selected_engine_object",
    "_selected_engine_uses_models", "_update_engine_visibility", "_save",
    "_prepare_selected_engine", "_setup_engine", "_pending_engine_config",
    "_poll_engine_settings",
)






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


if __name__ == "__main__":
    unittest.main()