"""Behavioral characterization of the owned integration controller units."""

from tests.ui_support import controller_context

import ast
import importlib.util
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import deps as deps_mod

try:
    from wayvoice import ui
    from wayvoice.ui.controllers.integration import IntegrationController
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")

#: The methods phase 2C moved out of the legacy class body.
INTEGRATION_METHODS = (
    "_apply_desktop_integration", "_apply_desktop_integration_worker",
    "_background_start", "_build_dependencies_group",
    "_refresh_dependency_rows", "_install_dependency",
    "_install_dependency_worker", "_install_dependency_done",
    "_missing_required",
)






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
        with mock.patch.object(deps_mod, "status_all", side_effect=AssertionError("probed anyway")):
            window.integration._refresh_dependency_rows()
            self.assertEqual(window.integration._dep_rows, {})

    def test_force_refreshes_even_when_recent(self):
        window = self._window()
        with mock.patch.object(deps_mod, "status_all", return_value=[]) as status_all:
            window.integration._refresh_dependency_rows(force=True)
            status_all.assert_called_once_with()
