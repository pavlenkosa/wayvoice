"""Behavioral characterization of the owned models controller units."""

from tests.ui_support import controller_context

import unittest

from wayvoice import model_store
from wayvoice.i18n import tr

try:
    from wayvoice import ui
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")


class FakeButton:
    def __init__(self):
        self.sensitive = True
        self.visible = None
        self.label = ""

    def set_sensitive(self, value):
        self.sensitive = value

    def set_visible(self, value):
        self.visible = value

    def set_label(self, label):
        self.label = label


class FakeRow:
    def __init__(self):
        self.title = ""
        self.subtitle = ""
        self.visible = None

    def set_title(self, text):
        self.title = text

    def set_subtitle(self, text):
        self.subtitle = text

    def set_visible(self, value):
        self.visible = value


@needs_window
class ControllerBehaviorSpotChecks(unittest.TestCase):
    """The bound methods answer through the real class, fakes for widgets."""

    def _window(self):
        window = controller_context()
        window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        window.state.ui_lang = "en"
        window.window.toast = FakeRow()  # only .set_subtitle-ish shape unused here
        window.models._model_entry = {}
        window.models._download_report = {}
        window.settings.model_fetch_btn = FakeButton()
        return window

    def test_custom_model_text_wins_for_the_model_id(self):
        window = self._window()
        window.state.cfg = {"custom_model": "my-folder/big-v3"}
        window.settings.model = FakeCombo(selected=len(model_store_preset_names()) - 1)
        window.settings.custom_model = FakeEntry("my-folder/big-v3")
        self.assertEqual(window.models._selected_model_id(), "my-folder/big-v3")

    def test_refresh_fetch_button_hides_for_a_local_model(self):
        window = self._window()
        window.models._model_entry = {
            "id": "/home/me/notes.wav", "kind": model_store.KIND_LOCAL,
            "downloaded": True, "size_bytes": 1,
        }
        window.models._refresh_fetch_button()
        self.assertIs(window.settings.model_fetch_btn.visible, False)


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


def model_store_preset_names():
    from wayvoice.models import MODEL_PRESETS

    return [str(p["id"]) for p in MODEL_PRESETS]


if __name__ == "__main__":
    unittest.main()
