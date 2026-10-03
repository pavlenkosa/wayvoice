"""The download row in the settings window.

Fetching a model is the only thing WayVoice waits minutes for, and until this
row existed that wait was invisible: the daemon was busy, the window said
nothing, and the user had no way to tell a slow line from a stuck one.

The widgets are stubbed: what matters is which of them the window touches and
with what, not how a ``Gtk.ProgressBar`` draws.  The window itself is built
without its constructor, because that one starts threads and talks to a daemon.
"""

import unittest

from wayvoice import ui
from wayvoice.i18n import tr


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


class FakeBar:
    def __init__(self):
        self.fraction = None
        self.pulses = 0
        self.paused = False

    def set_fraction(self, value):
        self.fraction = float(value)
        self.paused = False

    def pulse(self):
        self.pulses += 1
        self.paused = True


class DownloadRowTests(unittest.TestCase):
    def setUp(self):
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.ui_lang = "en"
        self.window.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.model_download_row = FakeRow()
        self.window.model_download_bar = FakeBar()
        self.row = self.window.model_download_row
        self.bar = self.window.model_download_bar

    def _apply(self, report):
        self.window._apply_download_state(report)

    def _downloading(self, **download):
        base = {"state": "downloading", "model": "medium", "done_bytes": 0,
                "total_bytes": 0, "error": "", "warming": False}
        base.update(download)
        return {"supported": True, "present": False, "model": "medium",
                "download": base, "warming": bool(download.get("warming"))}

    def test_a_download_shows_the_row(self):
        self._apply(self._downloading(done_bytes=10, total_bytes=100))
        self.assertTrue(self.row.visible)
        self.assertIn("Medium", self.row.title)

    def test_the_fraction_follows_the_bytes(self):
        self._apply(self._downloading(done_bytes=25, total_bytes=100))
        self.assertAlmostEqual(self.bar.fraction, 0.25)

    def test_the_text_carries_bytes_and_a_percentage(self):
        self._apply(self._downloading(done_bytes=512 * 1024 * 1024,
                                      total_bytes=1024 * 1024 * 1024))
        self.assertIn("50%", self.row.subtitle)
        self.assertIn("512", self.row.subtitle)

    def test_nothing_arrived_yet_pulses_instead_of_sitting_at_zero(self):
        # A fixed fraction of zero looks like a hang; the bar is still moving.
        self._apply(self._downloading(done_bytes=0, total_bytes=100))
        self.assertIsNone(self.bar.fraction)
        self.assertTrue(self.bar.paused)

    def test_an_unknown_size_pulses_too(self):
        self._apply(self._downloading(done_bytes=0, total_bytes=0))
        self.assertTrue(self.bar.paused)

    def test_more_bytes_than_expected_stays_at_full(self):
        self._apply(self._downloading(done_bytes=120, total_bytes=100))
        self.assertEqual(self.bar.fraction, 1.0)

    def test_warming_is_its_own_message(self):
        # Nothing is being fetched any more: the weights are down and the model
        # is going into memory. Showing 100% forever would look stuck.
        self._apply(self._downloading(done_bytes=100, total_bytes=100, warming=True))
        self.assertTrue(self.row.visible)
        self.assertTrue(self.bar.paused)
        self.assertNotIn("%", self.row.subtitle)

    def test_a_failed_download_shows_the_reason(self):
        self._apply(self._downloading(state="error", error="404 Client Error"))
        self.assertTrue(self.row.visible)
        self.assertIn("404", self.row.subtitle)

    def test_a_failure_without_a_reason_is_not_shown(self):
        self._apply(self._downloading(state="error", error=""))
        self.assertFalse(self.row.visible)

    def test_a_finished_download_hides_the_row(self):
        self._apply({"supported": True, "present": True, "model": "medium",
                     "download": {"state": "ready", "done_bytes": 1,
                                  "total_bytes": 1, "error": "", "warming": False},
                     "warming": False})
        self.assertFalse(self.row.visible)

    def test_an_idle_daemon_hides_the_row(self):
        self._apply({"supported": True, "present": True, "model": "medium",
                     "download": {"state": "idle"}, "warming": False})
        self.assertFalse(self.row.visible)

    def test_a_report_from_an_older_daemon_is_not_fatal(self):
        # The window asks for a status the daemon may not know how to answer.
        for report in (None, {}, {"download": None}, "nonsense"):
            self._apply(report)
            self.assertFalse(self.row.visible)

    def test_another_models_download_is_not_reported_as_this_row(self):
        # The report is about the selected model; a download of something else
        # must not be painted next to it.
        self._apply({"supported": True, "present": False, "model": "small",
                     "download": {"state": "downloading", "model": "medium",
                                  "done_bytes": 5, "total_bytes": 10,
                                  "error": "", "warming": False},
                     "warming": False})
        self.assertFalse(self.row.visible)


if __name__ == "__main__":
    unittest.main()
