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

import importlib.util
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
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.ui_lang = "en"
        self.window.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.toast = FakeToast()
        self.window._logs_copying = False
        self.window._logs_button = None
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
            ui.GLib, "idle_add",
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            ui.WayVoiceWindow, "_diagnostics_text", return_value="WayVoice 0.6.2\nOS: Linux"
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
        self.window._copy_diagnostics()
        self.assertEqual(self.copied, ["WayVoice 0.6.2\nOS: Linux"])
        self.assertEqual(self.window.toast.titles, [tr("toast.diagnostics_copied", "en")])

    def test_no_other_clipboard_api_is_reached_for(self):
        # A guard rather than a test of one line: the deprecated API is still
        # importable, and reaching for it again would break the button silently.
        #
        # The pattern is a clipboard call on anything but the widget itself:
        # ``Gdk.Display.get_default().get_clipboard()`` accepts text on Wayland and
        # never offers it. Comments are skipped on purpose - the one explaining this
        # bug quotes the very call it is about, and _install_css legitimately uses
        # Gdk.Display for CSS.
        offenders = []
        with open(ui.__file__, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if ".get_clipboard()" in line and "self.get_clipboard()" not in line:
                    offenders.append(f"{number}: {stripped}")
        self.assertEqual(offenders, [],
                         "a clipboard that does not work on Wayland:\n"
                         + "\n".join(offenders))

    def test_a_clipboard_that_refuses_is_reported(self):
        with mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=injector.InjectionError("no Wayland display"),
        ):
            self.window._copy_diagnostics()
        self.assertEqual(len(self.window.toast.titles), 1)
        self.assertIn("no Wayland display", self.window.toast.titles[0])

    def test_a_clipboard_failure_does_not_claim_success(self):
        with mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=injector.InjectionError("busy"),
        ):
            self.window._copy_diagnostics()
        self.assertNotIn(tr("toast.diagnostics_copied", "en"), self.window.toast.titles)


@needs_window
class LogsButtonTests(unittest.TestCase):
    def setUp(self):
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.ui_lang = "en"
        self.window.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.toast = FakeToast()
        self.window._logs_copying = False
        self.window._logs_button = None
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
            ui.GLib, "idle_add",
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
        return mock.patch.object(ui.subprocess, "run", return_value=cp)

    def test_the_journal_lands_in_the_clipboard(self):
        with self._journal():
            self.window._copy_logs()
        self._wait()
        self.assertEqual(self.copied, ["2026-10-05 01:00:00 line\n"])
        self.assertEqual(self.window.toast.titles, [tr("toast.logs_copied", "en")])

    def test_the_command_asks_for_the_daemon_unit_without_a_pager(self):
        # --no-pager matters: without it the output goes through a pager that the
        # subprocess has no terminal to draw on, and the clipboard gets nothing.
        seen = {}

        def record(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, "line\n", "")

        with mock.patch.object(ui.subprocess, "run", side_effect=record):
            self.window._copy_logs()
        self._wait()
        self.assertIn("--no-pager", seen["cmd"])
        self.assertIn("wayvoice.service", seen["cmd"])
        self.assertIs(seen["kwargs"].get("check"), False)
        self.assertIn("timeout", seen["kwargs"])

    def test_an_empty_journal_is_said_rather_than_copied(self):
        with self._journal(stdout="   \n"):
            self.window._copy_logs()
        self._wait()
        self.assertEqual(self.copied, [])
        self.assertEqual(self.window.toast.titles, [tr("toast.logs_empty", "en")])

    def test_a_failure_says_why(self):
        # journalctl exits non-zero when the unit has never run, and puts the
        # reason on stderr; a silent empty clipboard would be a dead button again.
        with self._journal(returncode=1, stderr="Unit wayvoice.service could not be found."):
            self.window._copy_logs()
        self._wait()
        self.assertEqual(self.copied, [])
        self.assertEqual(len(self.window.toast.titles), 1)
        self.assertIn("could not be found", self.window.toast.titles[0])

    def test_a_missing_journalctl_is_reported_not_raised(self):
        with mock.patch.object(ui.subprocess, "run", side_effect=FileNotFoundError("journalctl")):
            self.window._copy_logs()
        self._wait()
        self.assertEqual(self.copied, [])
        self.assertEqual(len(self.window.toast.titles), 1)

    def test_a_hanging_journal_is_not_left_to_hang_the_window(self):
        with mock.patch.object(
            ui.subprocess, "run",
            side_effect=subprocess.TimeoutExpired("journalctl", 5),
        ):
            self.window._copy_logs()
        self._wait()
        self.assertEqual(len(self.window.toast.titles), 1)


@needs_window
class ToastClippingTests(unittest.TestCase):
    """An ``Adw.Toast`` does not wrap, so a long title stretches across the window.

    The clipping used to live in a *second* ``_toast`` defined earlier in the class,
    which the later definition silently replaced - so nothing was ever clipped, and
    the default timeout was 3 s instead of 4. The two definitions coexisted for a
    while without a symptom, because a short title looks identical either way.
    """

    def setUp(self):
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.toast = FakeToast()
        self.window.t = lambda key, **kwargs: tr(key, "en", **kwargs)

    def test_a_long_title_is_clipped(self):
        self.window._toast("refused: " + "E: broken package " * 60)
        (title,) = self.window.toast.titles
        self.assertLessEqual(len(title), 200, f"{len(title)} characters in one toast")

    def test_a_short_title_is_left_alone(self):
        self.window._toast("Copied.")
        self.assertEqual(self.window.toast.titles, ["Copied."])

    def test_only_one_definition_of_toast_exists(self):
        import ast

        with open(ui.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        twice = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            count = sum(1 for item in node.body
                        if isinstance(item, ast.FunctionDef) and item.name == "_toast")
            if count > 1:
                twice.append(f"{node.name} x{count}")
        self.assertEqual(twice, [], "the later definition silently replaces the earlier")


@needs_window
class MainLoopTests(unittest.TestCase):
    """Nothing slow may run inside a GTK signal handler."""

    def setUp(self):
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.ui_lang = "en"
        self.window.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.toast = FakeToast()
        self.window._logs_copying = False
        self.window._logs_button = None
        patcher = mock.patch.object(
            ui.GLib, "idle_add",
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

        with mock.patch.object(ui.subprocess, "run", side_effect=slow):
            self.window._copy_logs()
            for thread in threading.enumerate():
                if thread is not threading.current_thread():
                    thread.join(timeout=10)
        self.assertTrue(where, "journalctl never ran at all")
        for thread in where:
            self.assertIsNot(thread, clicked_on,
                             "journalctl ran on the thread that got the click")
        self.assertFalse(self.window._logs_copying, "the button stayed disabled")

    def test_a_second_click_does_not_start_a_second_read(self):
        reads = []

        def record(*_args, **_kwargs):
            reads.append(1)
            return subprocess.CompletedProcess(["journalctl"], 0, "line\n", "")

        with mock.patch.object(ui.subprocess, "run", side_effect=record):
            # Hold the flag the way a running read does.
            self.window._logs_copying = True
            self.window._copy_logs()
        self.assertEqual(reads, [], "a second read started while one was in flight")

    def test_a_thread_that_cannot_start_leaves_the_button_usable(self):
        button = mock.Mock()
        with mock.patch.object(ui.threading, "Thread", side_effect=RuntimeError("no threads")):
            self.window._copy_logs(button)
        self.assertFalse(self.window._logs_copying)
        button.set_sensitive.assert_called_with(True)
        self.assertEqual(len(self.window.toast.titles), 1)


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

    def test_no_string_promises_a_command_instead_of_an_action(self):
        # find_spec rather than ui.__file__: this class runs without GTK, where the
        # import of wayvoice.ui fails and ui is None. Locating the file does not
        # import it, so the check works on CI as well as on a desktop.
        origin = importlib.util.find_spec("wayvoice.ui").origin
        with open(origin, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("toast.logs\"", source, "the old log toast key is still used")


@needs_window
class DownloadErrorTests(unittest.TestCase):
    """A download the daemon refused to start must not look like a dead button.

    The bug: selecting a Hub model without the Faster-Whisper runtime and pressing
    Download made the daemon answer with an error the window swallowed - no row,
    no toast, the fetch button hidden until the next poll - so the press did
    nothing visible at all.
    """

    def setUp(self):
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.ui_lang = "en"
        self.window.t = lambda key, **kwargs: tr(key, "en", **kwargs)
        self.window.model_download_row = FakeRow()
        self.window.model_download_bar = FakeBar()
        self.window.model_download_cancel_btn = FakeButton()
        self.window.model_fetch_btn = FakeButton()
        self.window._model_entry = {}
        self.window._download_report = {}
        self.window.toast = FakeToast()
        # GLib.idle_add runs its callback at once, so the reply is handled on
        # this thread the way the main loop would - without a running loop.
        patcher = mock.patch.object(
            ui.GLib, "idle_add",
            side_effect=lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entry(self, **entry):
        base = {"id": "small", "kind": model_store.KIND_REPO,
                "downloaded": False, "size_bytes": 1500}
        base.update(entry)
        self.window._model_entry = base

    def test_error_state_leaves_row_visible_with_error_subtitle(self):
        # The row is the only place a failed download explains itself; hiding it
        # turned an error into silence.
        self.window._apply_download_state({
            "supported": True, "present": False, "model": "small",
            "download": {"state": "error", "model": "small",
                         "error": "The Faster-Whisper runtime is not prepared"},
        })
        self.assertTrue(self.window.model_download_row.visible)
        self.assertIn("runtime is not prepared", self.window.model_download_row.subtitle)

    def test_refresh_fetch_button_shows_button_after_error_for_same_model(self):
        # A download that failed may succeed on the second press: the network
        # came back, the disk filled up - the button has to return.
        self.window._download_report = {
            "download": {"state": "error", "model": "small", "error": "404"}
        }
        self._entry()
        self.window._refresh_fetch_button()
        self.assertTrue(self.window.model_fetch_btn.visible)

    def test_a_daemon_refusal_becomes_a_toast(self):
        # The press went to the daemon, the daemon refused, and nothing said so:
        # exactly the "the button does nothing" report. The refusal names its
        # reason in the same field every daemon reply uses. Called directly:
        # the real path crosses a worker thread, and asserting on it from here
        # would race the thread instead of testing the handler.
        self.window._handle_prepare_model_reply({"ok": False, "error": "refused: busy"})
        self.assertEqual(self.window.toast.titles, ["refused: busy"])

    def test_the_reply_is_marshalled_to_the_main_loop(self):
        # The request runs on a worker thread; GTK is touched from the main loop
        # only. The marshalling is a source shape the runtime cannot observe
        # cheaply, so it is pinned the way the single-_toast rule is.
        origin = importlib.util.find_spec("wayvoice.ui").origin
        with open(origin, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("GLib.idle_add(self._handle_prepare_model_reply, reply)", source)

    def test_a_daemon_that_does_not_answer_says_so(self):
        # request() already answers with a dict when the daemon is gone; it is
        # handled like any other refusal instead of vanishing with the thread.
        self.window._handle_prepare_model_reply({"ok": False, "error": "no answer"})
        self.assertEqual(self.window.toast.titles, ["no answer"])

    def test_a_local_folder_is_not_toasted_on_every_selection(self):
        # A local path is a perfectly good model: its row already says "not
        # downloadable", and the daemon's not_applicable refusal repeats it.
        # Toasting that on every selection would be noise, not information.
        self.window._handle_prepare_model_reply(
            {"ok": False, "error": "no download", "state": "not_applicable"})
        self.assertEqual(self.window.toast.titles, [])

    def test_a_successful_reply_is_not_toasted(self):
        # The daemon accepted the preparation; the download row reports the
        # progress itself, and a toast would only duplicate it.
        self.window._handle_prepare_model_reply({"ok": True, "state": "downloading"})
        self.assertEqual(self.window.toast.titles, [])

    def test_a_download_offer_still_appears_for_a_missing_hub_model(self):
        # The normal case, kept honest after the rework: a Hub model that is not
        # on disk, with nothing running, offers the download.
        self._entry()
        self.window._apply_download_state({"supported": True, "present": False,
                                           "model": "small",
                                           "download": {"state": "idle"}})
        self.assertTrue(self.window.model_fetch_btn.visible)


if __name__ == "__main__":
    unittest.main()
