import threading
import time
import unittest

from wayvoice.engine import (
    TranscriptionCancelled,
    TranscriptionTimeout,
    _language,
    _postprocess,
    _run_cancelable,
)

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
