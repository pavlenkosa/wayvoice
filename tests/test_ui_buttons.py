"""The two buttons that hand something to the user.

Both of them were broken in a way no test noticed, because the code reported
success either way:

* "Copy diagnostics" wrote to ``Gdk.Display.get_default().get_clipboard()``, which
  is the GTK3 clipboard. On Wayland it accepts the text and never offers it, so
  the toast said "copied" and the clipboard kept whatever it had;
* "Open logs" only showed a toast containing the journalctl command. Nothing was
  opened.

The clipboard goes through the same wl-copy path the dictation itself uses, which
is the one already known to work, and the logs button reads the journal and puts
the text into the clipboard.
"""

from tests.ui_support import controller_context
try:
    from gi.repository import GLib
    from wayvoice.ui.controllers.status import StatusController
except Exception:
    pass

import subprocess
import threading
import time
import unittest
from unittest import mock

from wayvoice import injector, model_store
from wayvoice.i18n import tr

try:
    from wayvoice import ui
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_window = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")


class FakeToast:
    def __init__(self):
        self.titles = []

    def add_toast(self, toast):
        # Adw.Toast exposes its text through get_title(), not as an attribute.
        self.titles.append(toast.get_title())


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

    def set_fraction(self, value):
        self.fraction = float(value)

    def pulse(self):
        self.pulses += 1


class FakeButton:
    def __init__(self):
        self.sensitive = None
        self.visible = None

    def set_sensitive(self, value):
        self.sensitive = bool(value)

    def set_visible(self, value):
        self.visible = bool(value)


class FakeClipboard:
    def __init__(self):
        self.texts = []

    def set_text(self, text):
        self.texts.append(text)


@needs_window
class DiagnosticsButtonTests(unittest.TestCase):
    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.window.toast = FakeToast()
        self.window.status._logs_copying = False
        self.window.status._logs_button = None
        self.copied = []
        patcher = mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=lambda text, language=None: self.copied.append(text),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The journal is read on a thread now, so the answer comes back through
        # GLib.idle_add. There is no main loop here, so it is run where it lands.
        patcher = mock.patch.object(
            GLib, "idle_add",
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            StatusController, "_diagnostics_text", return_value="WayVoice 0.6.2\nOS: Linux"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait(self):
        """Join the worker, so the assertions see a finished run."""
        for thread in threading.enumerate():
            if thread is not threading.current_thread():
                thread.join(timeout=10)

    def test_the_report_goes_to_a_clipboard_that_works(self):
        # The regression: the GTK3 clipboard accepts text on Wayland and never
        # offers it, so the button reported success and copied nothing.
        self.window.status._copy_diagnostics()
        self.assertEqual(self.copied, ["WayVoice 0.6.2\nOS: Linux"])
        self.assertEqual(self.window.window.toast.titles, [tr("toast.diagnostics_copied", "en")])


    def test_a_clipboard_that_refuses_is_reported(self):
        with mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=injector.InjectionError("no Wayland display"),
        ):
            self.window.status._copy_diagnostics()
        self.assertEqual(len(self.window.window.toast.titles), 1)
        self.assertIn("no Wayland display", self.window.window.toast.titles[0])
        self.assertNotIn(tr("toast.diagnostics_copied", "en"), self.window.window.toast.titles)


@needs_window
class LogsButtonTests(unittest.TestCase):
    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.window.toast = FakeToast()
        self.window.status._logs_copying = False
        self.window.status._logs_button = None
        self.copied = []
        patcher = mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=lambda text, language=None: self.copied.append(text),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # The journal is read on a thread now, so the answer comes back through
        # GLib.idle_add. There is no main loop here, so it is run where it lands.
        patcher = mock.patch.object(
            GLib, "idle_add",
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait(self):
        """Join the worker, so the assertions see a finished run."""
        for thread in threading.enumerate():
            if thread is not threading.current_thread():
                thread.join(timeout=10)

    def _journal(self, stdout="2026-10-05 01:00:00 line\n", returncode=0, stderr=""):
        cp = subprocess.CompletedProcess(["journalctl"], returncode, stdout, stderr)
        return mock.patch.object(subprocess, "run", return_value=cp)

    def test_the_journal_lands_in_the_clipboard(self):
        with self._journal():
            self.window.status._copy_logs()
        self._wait()
        self.assertEqual(self.copied, ["2026-10-05 01:00:00 line\n"])
        self.assertEqual(self.window.window.toast.titles, [tr("toast.logs_copied", "en")])

    def test_the_command_asks_for_the_daemon_unit_without_a_pager(self):
        # --no-pager matters: without it the output goes through a pager that the
        # subprocess has no terminal to draw on, and the clipboard gets nothing.
        seen = {}

        def record(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "line\n", "")

        with mock.patch.object(subprocess, "run", side_effect=record):
            self.window.status._copy_logs()
        self._wait()
        self.assertIn("--no-pager", seen["cmd"])
        self.assertIn("wayvoice.service", seen["cmd"])
        self.assertIs(seen["kwargs"].get("check"), False)
        self.assertIn("timeout", seen["kwargs"])

    def test_an_empty_journal_is_said_rather_than_copied(self):
        with self._journal(stdout="   \n"):
            self.window.status._copy_logs()
        self._wait()
        self.assertEqual(self.copied, [])
        self.assertEqual(self.window.window.toast.titles, [tr("toast.logs_empty", "en")])

    def test_a_failure_says_why(self):
        # journalctl exits non-zero when the unit has never run, and puts the
        # reason on stderr; a silent empty clipboard would be a dead button again.
        with self._journal(returncode=1, stderr="Unit wayvoice.service could not be found."):
            self.window.status._copy_logs()
        self._wait()
        self.assertEqual(self.copied, [])
        self.assertEqual(len(self.window.window.toast.titles), 1)
        self.assertIn("could not be found", self.window.window.toast.titles[0])

    def test_a_missing_journalctl_is_reported_not_raised(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("journalctl")):
            self.window.status._copy_logs()
        self._wait()
        self.assertEqual(self.copied, [])
        self.assertEqual(len(self.window.window.toast.titles), 1)

    def test_a_hanging_journal_is_not_left_to_hang_the_window(self):
        with mock.patch.object(
            subprocess, "run",
            side_effect=subprocess.TimeoutExpired("journalctl", 5),
        ):
            self.window.status._copy_logs()
        self._wait()
        self.assertEqual(len(self.window.window.toast.titles), 1)


@needs_window
class ToastClippingTests(unittest.TestCase):
    """An ``Adw.Toast`` does not wrap, so a long title stretches across the window.

    The clipping used to live in a *second* ``_toast`` defined earlier in the class,
    which the later definition silently replaced - so nothing was ever clipped, and
    the default timeout was 3 s instead of 4. The two definitions coexisted for a
    while without a symptom, because a short title looks identical either way.
    """

    def setUp(self):
        self.window = controller_context()
        self.window.window.toast = FakeToast()
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)

    def test_a_long_title_is_clipped(self):
        self.window.window._toast("refused: " + "E: broken package " * 60)
        (title,) = self.window.window.toast.titles
        self.assertLessEqual(len(title), 200, f"{len(title)} characters in one toast")

    def test_a_short_title_is_left_alone(self):
        self.window.window._toast("Copied.")
        self.assertEqual(self.window.window.toast.titles, ["Copied."])


@needs_window
class MainLoopTests(unittest.TestCase):
    """Nothing slow may run inside a GTK signal handler."""

    def setUp(self):
        self.window = controller_context()
        self.window.state.ui_lang = "en"
        self.window.state.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.window.toast = FakeToast()
        self.window.status._logs_copying = False
        self.window.status._logs_button = None
        patcher = mock.patch.object(
            GLib, "idle_add",
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(injector, "copy_to_clipboard")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_journal_is_not_read_on_the_calling_thread(self):
        # journalctl took the full five-second timeout when it stalled, inside the
        # click handler: the window stopped redrawing and stopped answering the
        # keyboard for all of it.
        import threading

        where = []
        clicked_on = threading.current_thread()

        def slow(*_args, **_kwargs):
            where.append(threading.current_thread())
            time.sleep(0.05)
            return subprocess.CompletedProcess(["journalctl"], 0, "line\n", "")

        with mock.patch.object(subprocess, "run", side_effect=slow):
            self.window.status._copy_logs()
            for thread in threading.enumerate():
                if thread is not threading.current_thread():
                    thread.join(timeout=10)
        self.assertTrue(where, "journalctl never ran at all")
        for thread in where:
            self.assertIsNot(thread, clicked_on,
                             "journalctl ran on the thread that got the click")
        self.assertFalse(self.window.status._logs_copying, "the button stayed disabled")

    def test_a_second_click_does_not_start_a_second_read(self):
        reads = []

        def record(*_args, **_kwargs):
            reads.append(1)
            return subprocess.CompletedProcess(["journalctl"], 0, "line\n", "")

        with mock.patch.object(subprocess, "run", side_effect=record):
            # Hold the flag the way a running read does.
            self.window.status._logs_copying = True
            self.window.status._copy_logs()
        self.assertEqual(reads, [], "a second read started while one was in flight")

    def test_a_thread_that_cannot_start_leaves_the_button_usable(self):
        button = mock.Mock()
        with mock.patch.object(threading, "Thread", side_effect=RuntimeError("no threads")):
            self.window.status._copy_logs(button)
        self.assertFalse(self.window.status._logs_copying)
        button.set_sensitive.assert_called_with(True)
        self.assertEqual(len(self.window.window.toast.titles), 1)


class LogRowWordingTests(unittest.TestCase):
    """The row has to say what it does.

    It used to be "Open logs" with the subtitle "Show the command used to inspect
    logs", which described the only thing it really did: print a command.
    """

    def test_the_row_says_it_copies(self):
        self.assertIn("Скопировать", tr("settings.copy_logs", "ru"))
        self.assertIn("Copy", tr("settings.copy_logs", "en"))

    def test_the_subtitle_says_where_the_text_goes(self):
        for language in ("ru", "en"):
            subtitle = tr("settings.copy_logs_sub", language)
            self.assertIn("200", subtitle)
            self.assertTrue(
                "clipboard" in subtitle.lower() or "буфер" in subtitle.lower(),
                f"{language}: {subtitle!r}",
            )


@needs_window
class DownloadErrorTests(unittest.TestCase):
    """A download the daemon refused to start must not look like a dead button.

    The bug: selecting a Hub model without the Faster-Whisper runtime and pressing
    Download made the daemon answer with an error the window swallowed - no row,
    no toast, the fetch button hidden until the next poll - so the press did
    nothing visible at all.
    """

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
        self.window.models._selected_model_id = lambda: "small"
        self.window.window.toast = FakeToast()
        # GLib.idle_add runs its callback at once, so the reply is handled on
        # this thread the way the main loop would - without a running loop.
        patcher = mock.patch.object(
            GLib, "idle_add",
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entry(self, **entry):
        base = {"id": "small", "kind": model_store.KIND_REPO,
                "downloaded": False, "size_bytes": 1500}
        base.update(entry)
        self.window.models._model_entry = base

    def test_completed_other_model_does_not_hide_download_offer(self):
        self._entry()
        self.window.models._download_report = {
            "download": {"state": "ready", "model": "medium"}}
        self.window.models._refresh_fetch_button()
        self.assertTrue(self.window.settings.model_fetch_btn.visible)

    def test_error_state_leaves_row_visible_with_error_subtitle(self):
        # The row is the only place a failed download explains itself; hiding it
        # turned an error into silence.
        self.window.models._apply_download_state({
            "supported": True, "present": False, "model": "small",
            "download": {"state": "error", "model": "small",
                         "error": "The Faster-Whisper runtime is not prepared"},
        })
        self.assertTrue(self.window.settings.model_download_row.visible)
        self.assertIn("runtime is not prepared", self.window.settings.model_download_row.subtitle)

    def test_refresh_fetch_button_shows_button_after_error_for_same_model(self):
        # A download that failed may succeed on the second press: the network
        # came back, the disk filled up - the button has to return.
        self.window.models._download_report = {
            "download": {"state": "error", "model": "small", "error": "404"}
        }
        self._entry()
        self.window.models._refresh_fetch_button()
        self.assertTrue(self.window.settings.model_fetch_btn.visible)

    def test_a_daemon_refusal_becomes_a_toast(self):
        # The press went to the daemon, the daemon refused, and nothing said so:
        # exactly the "the button does nothing" report. The refusal names its
        # reason in the same field every daemon reply uses. Called directly:
        # the real path crosses a worker thread, and asserting on it from here
        # would race the thread instead of testing the handler.
        self.window.models._handle_prepare_model_reply({"ok": False, "error": "refused: busy"})
        self.assertEqual(self.window.window.toast.titles, ["refused: busy"])

    def test_the_reply_is_marshalled_to_the_main_loop(self):
        from tests.test_ui_async_tasks import FakeGLib, TaskRunner
        glib = FakeGLib()
        runner = TaskRunner(glib)
        self.addCleanup(runner.close)
        self.window.tasks = runner
        caller = threading.current_thread()
        threads = []
        def request(*args, **kwargs):
            threads.append(threading.current_thread())
            return {"ok": False, "error": "refused"}
        with mock.patch("wayvoice.ui.controllers.models.request", side_effect=request), \
             mock.patch.object(self.window.models, "_handle_prepare_model_reply") as reply:
            self.window.models._ask_daemon_to_prepare_model("small", "faster-whisper")
            self.assertTrue(glib.ready.wait(2))
            self.assertIsNot(threads[0], caller)
            reply.assert_not_called()
            glib.drain()
            reply.assert_called_once()

    def test_a_daemon_that_does_not_answer_says_so(self):
        # request() already answers with a dict when the daemon is gone; it is
        # handled like any other refusal instead of vanishing with the thread.
        self.window.models._handle_prepare_model_reply({"ok": False, "error": "no answer"})
        self.assertEqual(self.window.window.toast.titles, ["no answer"])

    def test_a_local_folder_is_not_toasted_on_every_selection(self):
        # A local path is a perfectly good model: its row already says "not
        # downloadable", and the daemon's not_applicable refusal repeats it.
        # Toasting that on every selection would be noise, not information.
        self.window.models._handle_prepare_model_reply(
            {"ok": False, "error": "no download", "state": "not_applicable"})
        self.assertEqual(self.window.window.toast.titles, [])

    def test_a_successful_reply_confirms_the_start_at_once(self):
        # The daemon accepted the preparation. The next status poll can be a
        # full interval away and a fetch reports its first bytes later still,
        # so silence read as "the button did nothing". The window now confirms
        # the start immediately: a toast naming the model that was asked for
        # and an optimistic row in the accepted state, which the first real
        # state report then corrects.
        self._entry()
        self.window.models._handle_prepare_model_reply({"ok": True, "state": "downloading"}, "small")
        self.assertEqual(
            self.window.window.toast.titles, [self.window.state.t("toast.prepare_started", model="Small")])
        download = self.window.models._download_report["download"]
        self.assertEqual(download["state"], "downloading")
        self.assertEqual(download["model"], "small")

    def test_a_successful_warm_reply_paints_warming_not_downloading(self):
        # Weights already on disk: the accepted work is a load into memory, and
        # an optimistic "downloading" would announce a fetch that never happens.
        self._entry()
        self.window.models._model_entry["downloaded"] = True
        self.window.models._handle_prepare_model_reply({"ok": True, "state": "warming"}, "small")
        self.assertEqual(self.window.models._download_report["download"]["state"], "warming")

    def test_the_optimistic_row_is_not_painted_for_a_foreign_model(self):
        # The selection changed between the press and the answer: painting the
        # accepted work next to the newly selected model would be a lie the
        # first poll would have to correct, so the confirmation names and
        # paints the model the press was about, not the current selection.
        self._entry()
        self.window.models._model_entry["id"] = "medium"
        self.window.models._handle_prepare_model_reply({"ok": True, "state": "downloading"}, "small")
        self.assertEqual(
            self.window.window.toast.titles, [self.window.state.t("toast.prepare_started", model="Small")])
        self.assertEqual(self.window.models._download_report["download"]["model"], "small")

    def test_a_reply_for_a_press_this_window_never_made_is_ignored(self):
        # A window that never asked has no right to paint an optimistic state:
        # the attribute is only set by the ask path, and a handler invoked
        # without it would draw a download from thin air.
        self._entry()
        self.window.models._handle_prepare_model_reply({"ok": True, "state": "downloading"})
        self.assertEqual(self.window.window.toast.titles, [])
        self.assertNotIn("download", self.window.models._download_report)

    def test_a_download_offer_still_appears_for_a_missing_hub_model(self):
        # The normal case, kept honest after the rework: a Hub model that is not
        # on disk, with nothing running, offers the download.
        self._entry()
        self.window.models._apply_download_state({"supported": True, "present": False,
                                           "model": "small",
                                           "download": {"state": "idle"}})
        self.assertTrue(self.window.settings.model_fetch_btn.visible)


if __name__ == "__main__":
    unittest.main()
