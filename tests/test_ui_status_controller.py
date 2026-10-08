"""Behavioral characterization of the owned status controller units."""

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
    from wayvoice.ui.controllers.status import StatusController
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")

#: The methods and class attributes phase 2D moved out of the legacy class body.
STATUS_METHODS = (
    "JOURNAL_LINES", "JOURNAL_TIMEOUT",
    "_toggle", "_update_cards", "_set_state_style", "_poll_status",
    "_language_label", "_diagnostics_text", "_copy_diagnostics",
    "_copy_logs", "_copy_logs_finished", "_journal_tail",
)






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
            request.assert_called_once_with("toggle", timeout=0.8)

    def test_busy_press_cancels_and_a_failure_is_announced(self):
        window = self._window()
        window.status._ui_busy = True
        with mock.patch("wayvoice.ui.controllers.status.request") as request:
            request.return_value = {"ok": False, "error": "daemon down"}
            window.status._toggle()
            request.assert_called_once_with("cancel", timeout=0.8)
            window.window.toast.add_toast.assert_called_once()


class FakeProcess:
    def __init__(self, returncode, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


@needs_window
class JournalTailTests(unittest.TestCase):
    def test_a_failed_journalctl_raises_subprocess_error(self):
        window = controller_context()
        window.status.JOURNAL_LINES = StatusController.JOURNAL_LINES
        window.status.JOURNAL_TIMEOUT = StatusController.JOURNAL_TIMEOUT
        with mock.patch("wayvoice.ui.controllers.status.subprocess.run",
                        return_value=FakeProcess(1, stderr="boom")):
            from subprocess import SubprocessError

            with self.assertRaises(SubprocessError) as caught:
                window.status._journal_tail()
            self.assertIn("boom", str(caught.exception))


if __name__ == "__main__":
    unittest.main()