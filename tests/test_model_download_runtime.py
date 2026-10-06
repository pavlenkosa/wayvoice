"""Tests for the model download when the runtime is missing.

This reproduces the bug where pressing Download in Settings for a hub model
silently does nothing when the Faster-Whisper runtime is NOT prepared.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine, model_store

from support import isolate_environment


class RuntimeMissingTests(unittest.TestCase):
    """Test cases for when the Faster-Whisper runtime is missing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        
        # Set up isolated environment
        self.root = isolate_environment(self)
        
        # Patch HF_HUB_CACHE to use our test directory
        patch = mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(self.cache)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_download_model_with_missing_runtime_returns_error_state(self):
        """Test that download_model returns 'error' state when runtime is missing."""
        # Mock the _model_download_args to return None when runtime python is missing
        with mock.patch.object(engine, "model_is_present", return_value=False):
            with mock.patch.object(engine, "_model_download_args", return_value=None):
                # This should return error state, not unsupported
                result = engine.download_model("small", language="en")
                
        # Should be error state, not unsupported
        self.assertEqual(result["state"], "error")
        # Should mention runtime/setup
        self.assertIn("runtime", result["error"].lower())
        # Should NOT be "unsupported"
        self.assertNotEqual(result["state"], "unsupported")

    def test_download_model_with_non_hub_id_still_returns_unsupported(self):
        """Test that download_model still returns 'unsupported' for non-hub models."""
        # Mock the _model_download_args to return None for a local path
        with mock.patch.object(engine, "model_is_present", return_value=False):
            with mock.patch.object(engine, "_model_download_args", return_value=None):
                # This should still return unsupported state
                result = engine.download_model("/some/local/path", language="en")
                
        # Should still be unsupported for non-hub models
        self.assertEqual(result["state"], "unsupported")

    def test_model_download_args_returns_none_when_runtime_python_missing(self):
        """Test that _model_download_args returns None when runtime python is missing."""
        # Make sure the runtime directory doesn't exist or python doesn't exist
        with mock.patch.object(engine, "faster_runtime", return_value=Path("/nonexistent/runtime")):
            args = engine._model_download_args("small")
            self.assertIsNone(args)

    def test_model_download_args_returns_args_when_runtime_python_exists(self):
        """Test that _model_download_args returns args when runtime python exists."""
        # Create a fake runtime directory with python executable
        fake_runtime = Path(self.tmp.name) / "fake_runtime"
        fake_runtime.mkdir()
        bin_dir = fake_runtime / "bin"
        bin_dir.mkdir()
        python_exe = bin_dir / "python"
        python_exe.touch()
        python_exe.chmod(0o755)
        
        # Create a fake model_fetch.py
        fetch_script = fake_runtime / "model_fetch.py"
        fetch_script.write_text("#!/usr/bin/env python\nprint('fake fetch')\n")
        fetch_script.chmod(0o755)
        
        with mock.patch.object(engine, "faster_runtime", return_value=fake_runtime):
            with mock.patch.object(engine, "script_path", return_value=fetch_script):
                args = engine._model_download_args("small")
                self.assertIsInstance(args, list)
                self.assertIn(str(python_exe), args)
                self.assertIn(str(fetch_script), args)

    def test_daemon_prepare_model_with_missing_runtime_sets_error_state(self):
        """The daemon's download of a Hub model without a runtime ends in "error".

        This is the user-facing half of the bug: the daemon runs the real
        ``prepare_model`` here (the names it imports come from ``wayvoice.engine``
        directly, so patching the module attributes would not reach them), the
        isolated environment has no runtime, and the report the window polls must
        name the missing setup instead of pretending all is well.
        """
        from wayvoice import daemon

        daemon_instance = daemon.WayVoiceDaemon()
        self.addCleanup(daemon_instance._shutdown.set)
        # The language is pinned because replies are translated: a test that
        # asserts on a message must not depend on the machine's locale.
        phase = daemon_instance._start_model_prepare(
            cfg={"model": "small", "ui_language": "en"}, download=True
        )
        self.assertEqual(phase, "downloading")
        if daemon_instance._prepare_thread is not None:
            daemon_instance._prepare_thread.join(timeout=10.0)
        download_state = daemon_instance._download
        self.assertEqual(download_state["state"], "error")
        self.assertIn("runtime", download_state["error"].lower())
        self.assertIn("setup", download_state["error"].lower())
        # The thread bookkeeping is unwound, so the next press is not refused.
        self.assertFalse(daemon_instance._prepare_running)
        self.assertIsNone(daemon_instance._prepare_thread)


if __name__ == "__main__":
    unittest.main()