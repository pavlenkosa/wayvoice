"""Behavioral characterization of the owned shortcut controller units."""

from tests.ui_support import controller_context

import unittest
from unittest import mock

try:
    from wayvoice import ui
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")


class FakeRow:
    def __init__(self):
        self.subtitle = ""

    def set_subtitle(self, text):
        self.subtitle = text


class FakeLabel:
    def __init__(self):
        self.text = ""

    def set_text(self, text):
        self.text = text


class FakeWin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@needs_window
class OpenCaptureTests(unittest.TestCase):
    """The settings row reaches the capture dialog through the bound method."""

    def test_the_row_opens_the_capture_window(self):
        window = controller_context()
        import wayvoice.ui.dialogs.shortcut_window as shortcut_window
        with mock.patch.object(shortcut_window, "shortcut_capture") as shortcut_capture:
            window.shortcut._open_shortcut_capture()
        shortcut_capture.assert_called_once_with(window.window, window.state.shortcut_binding, window.state.t, window.shortcut._disable_shortcut, window.shortcut._capture_shortcut_key)


@needs_window
class DisableTests(unittest.TestCase):
    def test_disabling_clears_the_binding_and_closes_the_dialog(self):
        window = controller_context()
        window.state.t = lambda key, **kwargs: key
        window.state.shortcut_binding = "<Control>r"
        window.settings.shortcut_row = FakeRow()
        window.home.hotkey_label = FakeLabel()
        win = FakeWin()
        window.shortcut._disable_shortcut(None, win)
        self.assertEqual(window.state.shortcut_binding, "")
        self.assertEqual(window.settings.shortcut_row.subtitle, "shortcut.disabled")
        self.assertEqual(window.home.hotkey_label.text, "shortcut.disabled")
        self.assertTrue(win.closed)


class FakeKeyLabel(FakeLabel):
    pass


@needs_window
class CaptureKeyTests(unittest.TestCase):
    """A lone letter is not a binding; a modified key becomes one."""

    def _window(self):
        window = controller_context()
        window.state.t = lambda key, **kwargs: key
        window.state.shortcut_binding = ""
        window.settings.shortcut_row = FakeRow()
        window.home.hotkey_label = FakeLabel()
        return window

    def test_a_bare_letter_is_refused_with_an_explanation(self):
        window = self._window()
        key_label = FakeKeyLabel()
        state = 0
        with (
            mock.patch("wayvoice.ui.controllers.shortcut.Gdk.keyval_name", return_value="r"),
            mock.patch("wayvoice.ui.controllers.shortcut.Gdk.ModifierType",
                       mock.Mock(CONTROL_MASK=1, SHIFT_MASK=2, ALT_MASK=4, SUPER_MASK=8, META_MASK=16)),
        ):
            result = window.shortcut._capture_shortcut_key(None, "r", 0, state, FakeWin(), key_label)
        self.assertTrue(result)
        self.assertIn("shortcut.need_modifier", key_label.text)
        self.assertEqual(window.state.shortcut_binding, "")

    def test_escape_closes_without_changing_the_binding(self):
        window = self._window()
        win = FakeWin()
        with mock.patch("wayvoice.ui.controllers.shortcut.Gdk.keyval_name", return_value="Escape"):
            result = window.shortcut._capture_shortcut_key(None, "Escape", 0, 0, win, FakeKeyLabel())
        self.assertTrue(result)
        self.assertTrue(win.closed)
        self.assertEqual(window.state.shortcut_binding, "")


if __name__ == "__main__":
    unittest.main()
