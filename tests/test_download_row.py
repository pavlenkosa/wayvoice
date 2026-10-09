"""The download row in the settings window.

Fetching a model is the only thing WayVoice waits minutes for, and until this
row existed that wait was invisible: the daemon was busy, the window said
nothing, and the user had no way to tell a slow line from a stuck one.

The widgets are stubbed: what matters is which of them the window touches and
with what, not how a ``Gtk.ProgressBar`` draws.  The window itself is built
without its constructor, because that one starts threads and talks to a daemon.
"""

from tests.ui_support import controller_context
try:
    from wayvoice.ui.model_presentation import model_state_text, hero_preparation_caption, show_hero_preparation
except Exception:
    pass

import unittest

from wayvoice import model_store
from wayvoice.i18n import tr

try:
    from wayvoice import ui
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

#: Applied to every class below: they all need ``ui.WayVoiceWindow``.
needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")


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


class FakeButton:
    def __init__(self):
        self.sensitive = None
        self.visible = None

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)


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


@needs_window
class DownloadRowTests(unittest.TestCase):
    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.settings.model_download_row = FakeRow()
        self.window.settings.model_download_bar = FakeBar()
        self.window.settings.model_download_cancel_btn = FakeButton()
        self.window.models._selected_model_id = lambda: "medium"
        self.row = self.window.settings.model_download_row
        self.bar = self.window.settings.model_download_bar

    def _apply(self, report):
        self.window.models._apply_download_state(report)

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

    def test_cancelling_is_offered_while_bytes_are_moving(self):
        self._apply(self._downloading(done_bytes=1, total_bytes=100))
        self.assertTrue(self.window.settings.model_download_cancel_btn.sensitive)

    def test_cancelling_is_not_offered_while_the_model_is_loading(self):
        # Nothing can be interrupted at that point; a button that reports
        # success and changes nothing is worse than no button.
        self._apply(self._downloading(done_bytes=100, total_bytes=100, warming=True))
        self.assertFalse(self.window.settings.model_download_cancel_btn.sensitive)

    def test_warming_is_its_own_message(self):
        # Nothing is being fetched any more: the weights are down and the model is going
        # into memory. Showing 100% forever would look stuck.
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
        self.window.models._selected_model_id = lambda: "small"
        # The report is about the selected model; a download of something else must not
        # be painted next to it.
        self._apply({"supported": True, "present": False, "model": "small",
                     "download": {"state": "downloading", "model": "medium",
                                  "done_bytes": 5, "total_bytes": 10,
                                  "error": "", "warming": False},
                     "warming": False})
        self.assertFalse(self.row.visible)

    def test_another_models_warm_up_is_not_reported_as_this_row_either(self):
        self.window.models._selected_model_id = lambda: "small"
        # The same lie in the other direction: the user picked another model while the
        # old one was still being read into the worker. The warm-up is a branch of its
        # own, so it needed the same guard, in the same place: before it.
        self._apply({"supported": True, "present": False, "model": "small",
                     "download": {"state": "warming", "model": "medium",
                                  "done_bytes": 0, "total_bytes": 0,
                                  "error": "", "warming": True},
                     "warming": True})
        self.assertFalse(self.row.visible)
        self.assertEqual(self.row.title, "")

    def test_this_models_own_warm_up_is_still_shown(self):
        # The guard must not swallow the case it was written for.
        self._apply({"supported": True, "present": True, "model": "medium",
                     "download": {"state": "warming", "model": "medium",
                                  "done_bytes": 0, "total_bytes": 0,
                                  "error": "", "warming": True},
                     "warming": True})
        self.assertTrue(self.row.visible)
        self.assertIn("Medium", self.row.title)


@needs_window
class FetchButtonTests(unittest.TestCase):
    """The row's own way to fetch a model it does not have."""

    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.settings.model_download_row = FakeRow()
        self.window.settings.model_download_bar = FakeBar()
        self.window.settings.model_download_cancel_btn = FakeButton()
        self.window.settings.model_fetch_btn = FakeButton()
        self.window.models._model_entry = {}
        self.window.models._download_report = {}
        self.asked = []
        self.window.models._ask_about_download = lambda model_id, size: self.asked.append(
            (model_id, size)
        )

    def _download(self, **download):
        base = {"state": "idle", "model": "", "done_bytes": 0, "total_bytes": 0,
                "error": "", "warming": False}
        base.update(download)
        return {"supported": True, "present": False, "model": "medium",
                "download": base}

    def _entry(self, **entry):
        base = {"id": "medium", "kind": model_store.KIND_REPO,
                "downloaded": False, "size_bytes": 1500}
        base.update(entry)
        self.window.models._model_entry = base

    def test_a_missing_hub_model_offers_the_download(self):
        # Without this button the only way to fetch a model was to pick a different
        # one - a place a user goes to only by accident.
        self._entry()
        self.window.models._apply_download_state(self._download())
        self.assertTrue(self.window.settings.model_fetch_btn.visible)

    def test_a_model_that_is_on_disk_does_not(self):
        self._entry(downloaded=True)
        self.window.models._apply_download_state(self._download())
        self.assertFalse(self.window.settings.model_fetch_btn.visible)

    def test_a_download_that_is_running_does_not_offer_a_second_one(self):
        self._entry()
        self.window.models._apply_download_state(
            self._download(state="downloading", model="medium", total_bytes=10)
        )
        self.assertFalse(self.window.settings.model_fetch_btn.visible)

    def test_a_failed_download_offers_it_again(self):
        # The reason it failed may be gone - the network came back - and a user who
        # cannot retry has to restart the program.
        self._entry()
        self.window.models._apply_download_state(
            self._download(state="error", model="medium", error="404 Client Error")
        )
        self.assertTrue(self.window.settings.model_fetch_btn.visible)

    def test_a_local_path_is_never_offered_a_download(self):
        # Nothing can fetch it: a button here would report success and change
        # nothing.
        self._entry(id="/home/u/models/foo", kind="local")
        self.window.models._apply_download_state(self._download())
        self.assertFalse(self.window.settings.model_fetch_btn.visible)

    def test_pressing_it_asks_the_same_question_a_choice_asks(self):
        self._entry()
        self.window.models._apply_download_state(self._download())
        self.window.models._ask_to_fetch_the_model()
        self.assertEqual(self.asked, [("medium", 1500)])


@needs_window
class ChoosingAModelTests(unittest.TestCase):
    """What happens between picking a model and the daemon fetching it.

    The widgets are the same stubs as above; the confirmation is a GTK window,
    so what is checked here is the decision: which of the three answers - warm
    it, ask about it, or leave it alone - a selection leads to.
    """

    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.models._download_confirmation_for = None
        self.prepared = []
        self.asked = []
        self.window.models._ask_daemon_to_prepare_model = lambda: self.prepared.append(True)
        self.window.models._ask_about_download = lambda model_id, size: self.asked.append(
            (model_id, size)
        )

    def _decide(self, **entry):
        base = {"id": "medium", "kind": model_store.KIND_REPO,
                "downloaded": False, "size_bytes": 1500}
        base.update(entry)
        self.window.models._decide_what_to_do_about_the_selected_model(base)

    def test_a_model_on_disk_is_warmed_without_a_question(self):
        # Loading it is free, and the first dictation would otherwise pay for it with
        # nothing having said so.
        self.window.models._download_confirmation_for = "medium"
        self._decide(downloaded=True)
        self.assertEqual(self.prepared, [True])
        self.assertEqual(self.asked, [])

    def test_a_model_that_is_not_there_is_asked_about_first(self):
        self.window.models._download_confirmation_for = "medium"
        self._decide()
        self.assertEqual(self.asked, [("medium", 1500)])
        self.assertEqual(self.prepared, [], "the download started without an answer")

    def test_a_local_path_is_neither_asked_about_nor_fetched(self):
        # Nothing can fetch it, so there is nothing to ask; the daemon reports
        # that for itself.
        self.window.models._download_confirmation_for = "/home/u/models/foo"
        self._decide(id="/home/u/models/foo", kind="local", downloaded=False)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.prepared, [True])

    def test_the_question_is_asked_once(self):
        self.window.models._download_confirmation_for = "medium"
        self._decide()
        self._decide()
        self.assertEqual(len(self.asked), 1, "the same download was asked about twice")

    def test_a_report_about_another_model_answers_nothing(self):
        # The state report is refreshed for the model that is selected now; one
        # that arrives late, from before the change, must not trigger anything.
        self.window.models._download_confirmation_for = "large-v3"
        self._decide(id="small")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.prepared, [])

    def test_an_answer_only_counts_for_the_model_it_was_asked_about(self):
        self.window.models._download_confirmation_for = "medium"
        self._decide(id="large-v3")
        self.assertEqual(self.asked, [])
        self.assertIsNotNone(
            self.window.models._download_confirmation_for,
            "the pending answer was dropped by an unrelated report",
        )


class FakeLabel:
    def __init__(self):
        self.text = ""

    def set_text(self, value):
        self.text = str(value)


class FakeIcon:
    def __init__(self):
        self.icon_name = ""

    def set_from_icon_name(self, name):
        self.icon_name = name


@needs_window
class HeroPreparationTests(unittest.TestCase):
    """The main window says what the daemon is doing to the model.

    While the settings window painted its download row, the hero kept saying
    "press and speak" for the whole wait - an invitation the hot key could not
    honour yet, since starting a dictation now answers "model missing".
    """

    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.models._selected_model_id = lambda: "medium"
        self.window.home.status_pill = FakeLabel()
        self.window.home.hero_state = FakeLabel()
        self.window.home.hero_caption = FakeLabel()
        self.window.home.mic_icon = FakeIcon()

    def _report(self, **download):
        base = {"state": "idle", "model": "", "done_bytes": 0,
                "total_bytes": 0, "error": "", "warming": False}
        base.update(download)
        return {"supported": True, "present": False, "model": "medium",
                "download": base}

    def _caption_for(self, report):
        return hero_preparation_caption(self.window, report)

    def test_a_download_is_painted_with_progress(self):
        caption = self._caption_for(self._report(
            state="downloading", model="medium", done_bytes=500, total_bytes=1500))
        self.assertIsNotNone(caption)
        self.assertIn("Downloading Medium", caption[0])
        self.assertIn("33%", caption[1])
        show_hero_preparation(self.window, *caption)
        self.assertEqual(self.window.home.status_pill.text, "Preparing")
        self.assertEqual(self.window.home.hero_state.text, "Downloading Medium")
        self.assertEqual(self.window.home.mic_icon.icon_name, "folder-download-symbolic")

    def test_a_download_without_bytes_yet_says_connecting(self):
        caption = self._caption_for(self._report(
            state="downloading", model="medium", total_bytes=0))
        self.assertIn("connecting", caption[1])

    def test_a_warm_up_names_the_phase(self):
        caption = self._caption_for(self._report(state="warming", model="medium"))
        self.assertIn("Preparing Medium", caption[0])
        self.assertIn("loaded into memory", caption[1])

    def test_a_warm_up_inside_downloading_does_not_say_100_percent(self):
        # The daemon reports the load into memory as "downloading" with
        # ``warming`` set, and the bytes stand at 100%. The settings row words
        # this phase as "Preparing"; the hero must not keep quoting a transfer
        # that has already finished.
        caption = self._caption_for(self._report(
            state="downloading", model="medium",
            done_bytes=1500, total_bytes=1500, warming=True))
        self.assertIn("Preparing Medium", caption[0])
        self.assertIn("loaded into memory", caption[1])
        self.assertNotIn("100%", caption[1])

    def test_an_error_is_not_quoted_every_poll(self):
        # The error's place is the settings row and the toast; the hero would
        # otherwise repeat it every 650 ms for the rest of the session.
        caption = self._caption_for(self._report(
            state="error", model="medium", error="404"))
        self.assertIsNone(caption)

    def test_work_on_another_model_is_not_shown(self):
        # The user has already moved the selector away; painting the old
        # download would describe a model this window does not name.
        caption = self._caption_for(self._report(
            state="downloading", model="small", done_bytes=1, total_bytes=2))
        self.assertIsNone(caption)

    def test_idle_reports_nothing(self):
        self.assertIsNone(self._caption_for(self._report()))
        self.assertIsNone(self._caption_for({}))


if __name__ == "__main__":
    unittest.main()
