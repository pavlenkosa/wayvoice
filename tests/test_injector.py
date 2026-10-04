"""The path that puts recognized text into the user's window.

This module had no tests at all, and reading it turned up two ways it could
break exactly there:

* it waited for ``wl-copy`` to claim the clipboard with ``poll()`` and no
  deadline, so a clipboard tool that stopped making progress held the caller -
  and because the daemon serves one client at a time, the whole daemon;
* it remembered the ``wl-copy`` process only after every step that could fail,
  so a failure in between leaked a process that kept owning the clipboard.

The process is faked throughout: what is under test is this module's own logic,
not wl-copy.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import injector


class FakeProc:
    """The part of ``Popen`` this module touches."""

    def __init__(self, *, exit_after=None, stderr="", stdin_fails=False):
        self.pid = 4242
        self.stdin = mock.Mock()
        self.stderr = mock.Mock()
        self.stderr.read.return_value = stderr
        if stdin_fails:
            self.stdin.write.side_effect = BrokenPipeError("pipe closed")
        self._exit_after = exit_after
        self._polls = 0
        self.terminated = False
        self.killed = False
        self.waited = 0

    def poll(self):
        self._polls += 1
        if self._exit_after is None:
            return None
        if self._polls >= self._exit_after:
            return 1
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited += 1
        return 0


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        injector._clipboard_proc = None
        self.addCleanup(setattr, injector, "_clipboard_proc", None)

    def _popen(self, proc):
        return mock.patch.object(injector.subprocess, "Popen", return_value=proc)

    def _which(self, path="/usr/bin/wl-copy"):
        return mock.patch.object(injector.shutil, "which", return_value=path)

    def test_a_hanging_wl_copy_cannot_hold_the_caller(self):
        # The regression: an unbounded poll() meant the daemon's single-threaded
        # accept loop sat inside a clipboard copy for as long as wl-copy felt
        # like staying quiet.
        proc = FakeProc(exit_after=None)  # never exits: it owns the selection
        with self._which(), self._popen(proc), mock.patch.object(
            injector.time, "sleep"
        ):
            injector.copy_to_clipboard("текст")  # must return
        self.assertIs(injector._clipboard_proc, proc)
        self.assertGreaterEqual(proc._polls, 1)

    def test_the_wait_for_the_selection_is_bounded(self):
        # The loop must be able to end on its deadline, not only on the exit of a
        # clipboard tool that has stopped making progress. The clock here steps
        # forward by exactly what the loop sleeps, so the number of iterations is
        # the deadline: a loop that ignored it would either spin until the clock
        # ran out or wait forever.
        proc = FakeProc(exit_after=None)
        slept = []
        now = [0.0]

        def monotonic():
            return now[0]

        budget = 20 * round(injector.CLIPBOARD_SETTLE_TIMEOUT / 0.005)

        def sleep(seconds):
            now[0] += seconds
            slept.append(seconds)
            # The guard is here and not only on the clock because a loop without
            # a deadline never reads the clock at all: without this the failure
            # would be a hung test, and a hung test is a test nobody waits for.
            if len(slept) > budget:
                raise AssertionError(
                    "the settle loop kept going long past its deadline"
                )

        with self._which(), self._popen(proc), mock.patch.object(
            injector.time, "sleep", side_effect=sleep
        ), mock.patch.object(injector.time, "monotonic", side_effect=monotonic):
            injector.copy_to_clipboard("текст")
        self.assertEqual(
            len(slept), round(injector.CLIPBOARD_SETTLE_TIMEOUT / 0.005),
            "the settle loop did not end on its deadline",
        )
        # And the process it was waiting for is deliberately left alone: that is
        # the process holding the selection.
        self.assertIs(injector._clipboard_proc, proc)
        self.assertFalse(proc.terminated)

    def test_a_tool_that_exits_is_waited_for_only_until_it_does(self):
        # The other end of the loop: a tool that exits on its own ends the wait
        # early, and its exit is a failure with the reason it gave - the text did
        # not make it into the clipboard, and saying otherwise would send the
        # user to paste an empty selection.
        proc = FakeProc(exit_after=10, stderr="wl-copy: no Wayland display")
        slept = []
        with self._which(), self._popen(proc), mock.patch.object(
            injector.time, "sleep", side_effect=lambda s: slept.append(s)
        ):
            with self.assertRaises(injector.InjectionError) as caught:
                injector.copy_to_clipboard("текст")
        self.assertEqual(len(slept), 9, "the loop did not follow the process")
        self.assertIn("no Wayland display", str(caught.exception))
        self.assertIsNone(injector._clipboard_proc, "an exited tool was kept")

    def test_a_failed_write_does_not_leak_the_process(self):
        # The regression: proc was assigned to the module global only after the
        # write, so a write that raised lost the process - and a lost wl-copy
        # keeps the Wayland clipboard for the rest of the session.
        proc = FakeProc(stdin_fails=True)
        with self._which(), self._popen(proc):
            with self.assertRaises(injector.InjectionError):
                injector.copy_to_clipboard("текст")
        self.assertIsNone(injector._clipboard_proc, "the process was lost")
        self.assertTrue(proc.terminated or proc.killed, "the process was not stopped")

    def test_an_early_exit_reports_the_real_error_and_forgets_the_process(self):
        proc = FakeProc(exit_after=1, stderr="No Wayland display")
        with self._which(), self._popen(proc):
            with self.assertRaises(injector.InjectionError) as caught:
                injector.copy_to_clipboard("текст")
        self.assertIn("No Wayland display", str(caught.exception))
        self.assertIsNone(injector._clipboard_proc)

    def test_a_new_copy_replaces_the_previous_clipboard_owner(self):
        old = FakeProc()
        new = FakeProc()
        with self._which(), mock.patch.object(injector.subprocess, "Popen", return_value=new):
            injector._clipboard_proc = old
            injector.copy_to_clipboard("текст")
        self.assertTrue(old.terminated, "the previous wl-copy was left running")
        self.assertIs(injector._clipboard_proc, new)

    def test_without_wl_copy_the_error_names_the_dependency(self):
        with mock.patch.object(injector.shutil, "which", return_value=None):
            with self.assertRaises(injector.InjectionError) as caught:
                injector.copy_to_clipboard("текст", "ru")
        self.assertIn("wl-copy", str(caught.exception))

    def test_cleanup_does_not_raise_without_a_process(self):
        injector._clipboard_proc = None
        injector._cleanup_clipboard()  # must not raise
        self.assertIsNone(injector._clipboard_proc)

    def test_cleanup_stops_a_process_that_is_still_holding_the_selection(self):
        # The exit hook runs at interpreter shutdown, when the process that owns
        # the Wayland clipboard has to be released: wl-copy would otherwise keep
        # owning it for the rest of the session with nobody to paste from.
        proc = FakeProc(exit_after=None)
        injector._clipboard_proc = proc
        injector._cleanup_clipboard()
        self.assertTrue(proc.terminated or proc.killed, "the clipboard tool survived")
        self.assertIsNone(injector._clipboard_proc, "a dead process was kept")


class WhichYdotoolTests(unittest.TestCase):
    """Which ``ydotool`` runs the paste: the system's if there is one.

    The bundled copy is what makes automatic paste work on a distribution that
    does not package the helper at all - Debian 13 has no ``ydotool`` - and it is
    the one the package manager is never asked about.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundled = Path(self.tmp.name)
        patcher = mock.patch.object(
            injector, "find_command", side_effect=injector.find_command
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        dir_patch = mock.patch.object(
            injector.find_command.__module__ and __import__(
                "wayvoice.deps", fromlist=["bundled_dir"]
            ),
            "bundled_dir",
            return_value=self.bundled,
        )
        dir_patch.start()
        self.addCleanup(dir_patch.stop)

    def _bundled_program(self) -> Path:
        path = self.bundled / "ydotool"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_the_system_copy_is_preferred(self):
        self._bundled_program()
        with mock.patch.object(injector.shutil, "which", return_value="/usr/bin/ydotool"):
            self.assertEqual(injector.ydotool_command(), "/usr/bin/ydotool")

    def test_the_bundled_copy_is_used_when_there_is_no_system_one(self):
        bundled = self._bundled_program()
        with mock.patch.object(injector.shutil, "which", return_value=None):
            self.assertEqual(injector.ydotool_command(), str(bundled))

    def test_the_resolved_path_is_the_one_that_runs(self):
        # Running "ydotool" by name again would ignore the resolution and find
        # whatever PATH has - which is the case this whole fallback exists for.
        bundled = self._bundled_program()
        ran = []
        with mock.patch.object(injector.shutil, "which", return_value=None), \
             mock.patch.object(
                 injector, "_run",
                 side_effect=lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
             ):
            ok, _message = injector.paste_with_ydotool("standard", "en")
        self.assertTrue(ok)
        self.assertEqual(ran[0][0], str(bundled))

    def test_without_any_copy_the_paste_says_so_and_types_nothing(self):
        with mock.patch.object(injector.shutil, "which", return_value=None), \
             mock.patch.object(injector, "_run") as run:
            ok, message = injector.paste_with_ydotool("standard", "en")
        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertFalse(run.called)


class InjectTests(unittest.TestCase):
    """The three paste modes, with the clipboard step faked away."""

    def setUp(self):
        injector._clipboard_proc = None
        self.addCleanup(setattr, injector, "_clipboard_proc", None)

    def _no_clipboard(self):
        return mock.patch.object(injector, "copy_to_clipboard")

    def _ydotool(self, returncode=0, stderr=""):
        cp = subprocess.CompletedProcess(["ydotool"], returncode, "", stderr)
        return mock.patch.object(injector, "_run", return_value=cp)

    def _ydotool_present(self):
        return mock.patch.object(
            injector.shutil, "which", return_value="/usr/bin/ydotool"
        )

    def test_copy_mode_never_touches_the_keyboard(self):
        with self._no_clipboard(), mock.patch.object(injector, "paste_with_ydotool") as paste:
            result = injector.inject("текст", {"paste_mode": "copy"})
        self.assertFalse(result.pasted)
        self.assertFalse(paste.called)

    def test_standard_mode_pastes(self):
        with self._no_clipboard(), self._ydotool(), self._ydotool_present():
            result = injector.inject("текст", {"paste_mode": "standard"})
        self.assertTrue(result.pasted)
        self.assertEqual(result.warning, "")

    def test_a_failing_keyboard_paste_is_reported_not_raised(self):
        # The text is in the clipboard either way, so the caller has to be able
        # to tell the user "Ctrl+V" without losing the result.
        with (
            self._no_clipboard(),
            self._ydotool(returncode=1, stderr="no socket"),
            self._ydotool_present(),
        ):
            result = injector.inject("текст", {"paste_mode": "standard"})
        self.assertFalse(result.pasted)
        self.assertIn("no socket", result.warning)

    def test_a_missing_ydotool_is_reported(self):
        with self._no_clipboard(), mock.patch.object(
            injector.shutil, "which", return_value=None
        ):
            result = injector.inject("текст", {"paste_mode": "standard"})
        self.assertFalse(result.pasted)
        self.assertIn("ydotool", result.warning)

    def test_empty_text_is_not_injected_at_all(self):
        with self._no_clipboard() as copy:
            result = injector.inject("", {"paste_mode": "standard"})
        self.assertFalse(copy.called)
        self.assertFalse(result.pasted)

    def test_terminal_mode_uses_the_bracket_sequence(self):
        seen = {}

        def record(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with (
            self._no_clipboard(),
            self._ydotool_present(),
            mock.patch.object(injector, "_run", side_effect=record),
        ):
            injector.inject("текст", {"paste_mode": "terminal"})
        # The program that runs is the resolved path, not the bare name: that is
        # what lets the bundled copy be used at all on a system whose PATH has
        # no ydotool in it.
        self.assertEqual(seen["cmd"][0], "/usr/bin/ydotool")
        # 29:x are the bracket-key codes: the terminal profile sends ESC [ once.
        self.assertEqual(seen["cmd"][1:3], ["key", "29:1"])
        self.assertIn("47:1", seen["cmd"])


if __name__ == "__main__":
    unittest.main()
