import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine
from wayvoice.paths import app_dir


class FasterRuntimeLocationTests(unittest.TestCase):
    """The engine runtime may be shipped with the application or be per-user."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Mirrors the packaged layout: <prefix>/lib/wayvoice/{app,runtime}.
        self.app = Path(self.tmp.name) / "lib" / "wayvoice" / "app"
        self.bundled = Path(self.tmp.name) / "lib" / "wayvoice" / "runtime"

    def _make_bundled(self) -> Path:
        (self.bundled / "bin").mkdir(parents=True)
        python = self.bundled / "bin" / "python"
        python.touch()
        python.chmod(0o755)
        return self.bundled

    def test_environment_override_wins(self):
        with mock.patch.dict(os.environ, {"WAYVOICE_RUNTIME": str(self.bundled)}):
            self.assertEqual(engine.faster_runtime(), self.bundled)

    def test_bundled_runtime_is_used_when_present(self):
        self._make_bundled()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAYVOICE_RUNTIME", None)
            with mock.patch.object(engine, "app_dir", return_value=self.app):
                self.assertEqual(engine.faster_runtime(), self.bundled)

    def test_a_directory_that_holds_no_runtime_is_ignored(self):
        # Not merely an empty one: a leftover directory from a previous installation,
        # holding something that is not an interpreter, must not win over the per-user
        # runtime. An empty directory made the two indistinguishable.
        (self.bundled / "bin").mkdir(parents=True)
        (self.bundled / "bin" / "not-python").write_text("#!/bin/sh\n")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAYVOICE_RUNTIME", None)
            with mock.patch.object(engine, "app_dir", return_value=self.app):
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name}):
                    resolved = engine.faster_runtime()
        self.assertEqual(resolved, Path(self.tmp.name) / "wayvoice" / "runtime")

    def test_per_user_runtime_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WAYVOICE_RUNTIME", None)
            with mock.patch.object(engine, "app_dir", return_value=self.app):
                with mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name}):
                    resolved = engine.faster_runtime()
        self.assertEqual(resolved, Path(self.tmp.name) / "wayvoice" / "runtime")

    def test_stamp_follows_the_runtime(self):
        self._make_bundled()
        with mock.patch.dict(os.environ, {"WAYVOICE_RUNTIME": str(self.bundled)}):
            self.assertEqual(engine.faster_stamp(), self.bundled / engine.RUNTIME_STAMP)


class AppDirTests(unittest.TestCase):
    def test_app_dir_is_next_to_the_sources(self):
        self.assertTrue(app_dir().name in {"app"} or app_dir().name.startswith("wayvoice"))


if __name__ == "__main__":
    unittest.main()
