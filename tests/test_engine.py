import threading
import time
import unittest

from wayvoice.engine import (
    TranscriptionCancelled,
    TranscriptionTimeout,
    _language,
    _postprocess,
    _run_cancelable,
    worker_info,
)


class WorkerInfoTests(unittest.TestCase):
    """What the settings window asks before offering to delete a model."""

    def test_no_worker_is_reported_as_not_running(self):
        from unittest import mock

        with mock.patch("wayvoice.engine._worker_ping", return_value=None):
            self.assertEqual(worker_info(), {"running": False, "model": ""})

    def test_running_worker_reports_the_model_it_holds(self):
        from unittest import mock

        with mock.patch(
            "wayvoice.engine._worker_ping",
            return_value={"ok": True, "config": {"model": "medium", "device": "cpu"}},
        ):
            info = worker_info()
        self.assertTrue(info["running"])
        self.assertEqual(info["model"], "medium")

    def test_worker_without_config_is_running_but_unknown(self):
        from unittest import mock

        with mock.patch("wayvoice.engine._worker_ping", return_value={"ok": True}):
            info = worker_info()
        self.assertTrue(info["running"])
        self.assertEqual(info["model"], "")

class EngineProcessTests(unittest.TestCase):
    def test_timeout_kills_process(self):
        with self.assertRaises(TranscriptionTimeout):
            _run_cancelable(["/bin/sh", "-c", "sleep 2"], timeout=0.2, cancel_event=None)

    def test_cancel_kills_process(self):
        event = threading.Event()
        timer = threading.Timer(0.15, event.set)
        timer.start()
        try:
            with self.assertRaises(TranscriptionCancelled):
                _run_cancelable(["/bin/sh", "-c", "sleep 2"], timeout=3, cancel_event=event)
        finally:
            timer.cancel()


class LanguageResolutionTests(unittest.TestCase):
    def test_default_is_detection(self):
        self.assertEqual(_language({}), "auto")

    def test_shipped_default_is_detection(self):
        from wayvoice.config import DEFAULTS

        self.assertEqual(DEFAULTS["language"], "auto")
        self.assertEqual(_language(dict(DEFAULTS)), "auto")

    def test_configured_language_is_normalized(self):
        self.assertEqual(_language({"language": "de-DE"}), "de")
        self.assertEqual(_language({"language": "RU_ru"}), "ru")
        self.assertEqual(_language({"language": ""}), "auto")
        self.assertEqual(_language({"language": "klingon"}), "auto")
        self.assertEqual(_language({"language": None}), "auto")

    def test_single_language_model_wins(self):
        self.assertEqual(_language({"model": "small.en", "language": "ru"}), "en")

    def test_spoken_punctuation_follows_the_recognition_language(self):
        cfg = {"language": "en", "append_space": False}
        self.assertEqual(_postprocess("hello comma world", cfg), "Hello, world.")
        self.assertEqual(_postprocess("привет запятая мир", cfg), "Привет запятая мир.")

    def test_auto_language_honours_both_command_sets(self):
        cfg = {"append_space": False}
        self.assertEqual(_postprocess("hello comma мир", cfg), "Hello, мир.")
        self.assertEqual(_postprocess("привет comma мир", cfg), "Привет, мир.")


if __name__ == "__main__":
    unittest.main()
