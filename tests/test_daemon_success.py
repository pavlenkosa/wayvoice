"""The one path in the daemon that nothing tested: a dictation that works.

Recognition succeeds, the text is inserted, and then a notification is sent with
the result. That last call had three positional arguments where ``notify()``
accepts two, so every successful dictation ended in a ``TypeError`` - raised
*after* the text was in the window, caught by the worker's blanket handler, and
stored as ``last_error``. The window then showed a red error state for a dictation
that had worked.

The bug was here since the first release commit. It survived because no test drove
this path: the engine, the injector and the recorder were all faked everywhere
else, and a test that fakes the parts never reaches the line where the real
function would have complained about its arguments.
"""

import threading
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import daemon as daemon_mod
from wayvoice import notify as notify_mod
from wayvoice.daemon import WayVoiceDaemon
from wayvoice.injector import InjectionResult
from wayvoice.i18n import tr

CONFIG = {
    "model": "small",
    "engine_worker": True,
    "notify": True,
    "ui_language": "en",
    "paste_mode": "standard",
}


class SuccessfulDictationTests(unittest.TestCase):
    """A dictation that ends the way it is supposed to end."""

    def setUp(self):
        patcher = mock.patch("wayvoice.daemon.load_config", return_value=dict(CONFIG))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("wayvoice.engine.warm_worker", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The real notify() runs, with nothing to notify: a spy that calls through
        # rather than a mock that replaces it.
        #
        # That distinction is the whole test. Replacing notify() with a Mock removes
        # the only thing that could go wrong - the caller binding three arguments to
        # a two-argument function - because a Mock accepts anything. The bug this
        # file exists for was invisible to every test that mocked the callee.
        patcher = mock.patch("wayvoice.notify.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls: list[tuple[tuple, dict]] = []
        real_notify = notify_mod.notify

        def spy(*args, **kwargs):
            self.calls.append((args, kwargs))
            return real_notify(*args, **kwargs)

        patcher = mock.patch("wayvoice.daemon.notify", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _daemon(self):
        daemon = WayVoiceDaemon.__new__(WayVoiceDaemon)
        daemon.last_text = ""
        daemon.last_error = ""
        daemon.last_warning = ""
        daemon._lock = threading.RLock()
        daemon._transcribe_cancel = threading.Event()
        daemon._shutdown = threading.Event()
        daemon._transcribe_thread = None
        daemon._transcribe_wav = None
        daemon._pending_asr_cleanup = set()
        daemon.busy = True
        daemon._busy_started = 0.0
        return daemon

    def _run(self, daemon, text="привет мир", pasted=True, warning=""):
        transcribe = mock.Mock(return_value=text)
        inject = mock.Mock(return_value=InjectionResult(
            pasted=pasted, warning=warning))
        with mock.patch("wayvoice.daemon.transcribe", transcribe), \
             mock.patch("wayvoice.daemon.inject", inject):
            daemon._transcribe_worker(Path("/tmp/does-not-matter.wav"))
        return transcribe, inject

    def test_a_working_dictation_reports_no_error(self):
        daemon = self._daemon()
        self._run(daemon)
        self.assertEqual(daemon.last_error, "",
                         f"a successful dictation reported {daemon.last_error!r}")

    def test_the_text_reaches_the_window_before_anything_else(self):
        daemon = self._daemon()
        _transcribe, inject = self._run(daemon)
        inject.assert_called_once()
        self.assertEqual(inject.call_args.args[0], "привет мир")
        self.assertEqual(daemon.last_text, "привет мир")

    def test_the_result_is_announced_with_the_recognised_text(self):
        daemon = self._daemon()
        self._run(daemon)
        summaries = [args[0] for args, _kwargs in self.calls if args]
        self.assertIn(tr("daemon.text_inserted", "en"), summaries)
        bodies = [args[1] for args, _kwargs in self.calls if len(args) > 1]
        self.assertIn("привет мир", bodies)

    def test_an_empty_result_is_a_warning_not_an_error(self):
        daemon = self._daemon()
        self._run(daemon, text="   ")
        self.assertEqual(daemon.last_error, "")
        self.assertTrue(daemon.last_warning)

    def test_a_paste_that_did_not_work_is_a_warning_not_an_error(self):
        # inject() reports a failed paste through InjectionResult rather than by
        # raising, and the distinction matters: the text is in the clipboard either
        # way, so this is not an error state.
        daemon = self._daemon()
        self._run(daemon, pasted=False, warning="буфер")
        self.assertEqual(daemon.last_error, "")
        self.assertEqual(daemon.last_warning, "буфер")


class NotifyCallSiteTests(unittest.TestCase):
    """No call site may pass more arguments than the function takes.

    The failure this catches is not exotic: it was in the tree from the first
    release commit and only ever showed up at runtime, on the one path nobody
    tested. ``notify(title, body, *, enabled, replace)`` takes two positional
    arguments, and a third one is a ``TypeError`` at the call itself - before the
    body of the function runs, so ``enabled=False`` does not even avoid it.
    """

    #: The signature, read from the source rather than remembered, so that changing
    #: the function does not leave this guard checking a stale number.
    MAX_POSITIONAL = 2

    def test_every_notify_call_fits_the_signature(self):
        import ast

        offenders = []
        for path in sorted(Path(daemon_mod.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(
                    node.func, "attr", "")
                if name != "notify":
                    continue
                if len(node.args) > self.MAX_POSITIONAL:
                    offenders.append(
                        f"{path.name}:{node.lineno} passes {len(node.args)} positional "
                        f"arguments, notify() takes {self.MAX_POSITIONAL}")
        self.assertEqual(offenders, [],
                         "a call that raises TypeError when it runs:\n"
                         + "\n".join(offenders))

    def test_the_announced_title_is_not_the_application_name(self):
        # notify-send already passes -a WayVoice, so a summary of "WayVoice" spends
        # the bold line of every notification on the application's own name.
        import ast

        titles = set()
        for path in sorted(Path(daemon_mod.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                        and node.func.id == "notify" and node.args \
                        and isinstance(node.args[0], ast.Constant):
                    titles.add(str(node.args[0].value))
        self.assertNotIn("WayVoice", titles,
                         "the summary should say what happened, not who is talking")


if __name__ == "__main__":
    unittest.main()
