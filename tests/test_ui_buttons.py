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
import unittest
from unittest import mock

from wayvoice import injector
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
        self.copied = []
        patcher = mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=lambda text, language=None: self.copied.append(text),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(
            ui.WayVoiceWindow, "_diagnostics_text", return_value="WayVoice 0.6.2\nOS: Linux"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.copied = []
        patcher = mock.patch.object(
            injector, "copy_to_clipboard",
            side_effect=lambda text, language=None: self.copied.append(text),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _journal(self, stdout="2026-10-05 01:00:00 line\n", returncode=0, stderr=""):
        cp = subprocess.CompletedProcess(["journalctl"], returncode, stdout, stderr)
        return mock.patch.object(ui.subprocess, "run", return_value=cp)

    def test_the_journal_lands_in_the_clipboard(self):
        with self._journal():
            self.window._copy_logs()
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
        self.assertIn("--no-pager", seen["cmd"])
        self.assertIn("wayvoice.service", seen["cmd"])
        self.assertIs(seen["kwargs"].get("check"), False)
        self.assertIn("timeout", seen["kwargs"])

    def test_an_empty_journal_is_said_rather_than_copied(self):
        with self._journal(stdout="   \n"):
            self.window._copy_logs()
        self.assertEqual(self.copied, [])
        self.assertEqual(self.window.toast.titles, [tr("toast.logs_empty", "en")])

    def test_a_failure_says_why(self):
        # journalctl exits non-zero when the unit has never run, and puts the
        # reason on stderr; a silent empty clipboard would be a dead button again.
        with self._journal(returncode=1, stderr="Unit wayvoice.service could not be found."):
            self.window._copy_logs()
        self.assertEqual(self.copied, [])
        self.assertEqual(len(self.window.toast.titles), 1)
        self.assertIn("could not be found", self.window.toast.titles[0])

    def test_a_missing_journalctl_is_reported_not_raised(self):
        with mock.patch.object(ui.subprocess, "run", side_effect=FileNotFoundError("journalctl")):
            self.window._copy_logs()
        self.assertEqual(self.copied, [])
        self.assertEqual(len(self.window.toast.titles), 1)

    def test_a_hanging_journal_is_not_left_to_hang_the_window(self):
        with mock.patch.object(
            ui.subprocess, "run",
            side_effect=subprocess.TimeoutExpired("journalctl", 5),
        ):
            self.window._copy_logs()
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


if __name__ == "__main__":
    unittest.main()
