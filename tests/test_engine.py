import threading
import time
import unittest

from wayvoice.engine import TranscriptionCancelled, TranscriptionTimeout, _run_cancelable

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

if __name__ == "__main__":
    unittest.main()
