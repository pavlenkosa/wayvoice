import threading
import time
import unittest

from wayvoice.engine import (
    TranscriptionCancelled,
    TranscriptionTimeout,
    _language,
    _postprocess,
    _run_cancelable,
    _worker_settings_match,
    worker_info,
)


class WorkerInfoTests(unittest.TestCase):
    """What the settings window asks before offering to delete a model."""

    def test_no_worker_is_reported_as_not_running(self):
        from unittest import mock

        with mock.patch("wayvoice.engine._worker_ping", return_value=None):
            self.assertEqual(
                worker_info(), {"running": False, "model": "", "version": ""}
            )

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


class WorkerVersionTests(unittest.TestCase):
    """A worker outliving an update must not answer for the new code.

    The warm worker deliberately survives daemon restarts - that is what makes the second
    dictation fast - so after an update the version has to be part of the match or the
    previous release keeps decoding speech.
    """

    def test_a_worker_from_another_version_does_not_match(self):
        from wayvoice import __version__

        reply = {
            "ok": True,
            "version": "0.0.1-old",
            "config": {"model": "small", "device": "auto", "beam_size": 5, "vad": True},
        }
        self.assertNotEqual(__version__, "0.0.1-old")
        self.assertFalse(_worker_settings_match(reply, {"model": "small"}))

    def test_a_worker_of_this_version_matches(self):
        from wayvoice import __version__

        reply = {
            "ok": True,
            "version": __version__,
            "config": {"model": "small", "device": "auto", "beam_size": 5, "vad": True},
        }
        self.assertTrue(_worker_settings_match(reply, {"model": "small"}))

    def test_a_worker_started_with_another_model_is_not_used(self):
        # The version is only half of the match: a worker left over from a changed
        # setting decodes with the wrong model, which sounds like recognition being
        # broken rather than like a stale worker.
        reply = {
            "ok": True,
            "version": __import__("wayvoice").__version__,
            "config": {"model": "large-v3", "device": "auto", "beam_size": 5, "vad": True},
        }
        self.assertFalse(_worker_settings_match(reply, {"model": "small"}))

    def test_a_worker_that_reports_no_version_is_still_trusted(self):
        # An older worker has no version field at all. Refusing it would mean never
        # using a warm worker again after one restart.
        reply = {"ok": True, "config": {"model": "small", "device": "auto", "beam_size": 5, "vad": True}}
        self.assertTrue(_worker_settings_match(reply, {"model": "small"}))


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
