"""The path that puts recognized text into the user's window.

Two ways this could break exactly there, both of which happened: waiting for
``wl-copy`` to claim the clipboard with ``poll()`` and no deadline, so a clipboard tool
that stopped making progress held the caller and, since the daemon serves one client
at a time, the whole daemon; and remembering the ``wl-copy`` process only after every
step that could fail, so a failure in between leaked a process that kept owning the
clipboard.

The process is faked throughout: what is under test is this module's own logic.
"""

import contextlib
import io
import os
import socket
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
        # An unbounded poll() meant the daemon's single-threaded accept loop sat inside
        # a clipboard copy for as long as wl-copy felt like staying quiet.
        proc = FakeProc(exit_after=None)  # never exits: it owns the selection
        with self._which(), self._popen(proc), mock.patch.object(
            injector.time, "sleep"
        ):
            injector.copy_to_clipboard("текст")  # must return
        self.assertIs(injector._clipboard_proc, proc)
        self.assertGreaterEqual(proc._polls, 1)

    def test_the_wait_for_the_selection_is_bounded(self):
        # The loop must be able to end on its deadline, not only on the exit of a tool
        # that stopped making progress. The clock here advances by exactly what the loop
        # sleeps, so the iteration count is the deadline: a loop that ignored it would
        # spin until the clock ran out or wait forever.
        proc = FakeProc(exit_after=None)
        slept = []
        now = [0.0]

        def monotonic():
            return now[0]

        budget = 20 * round(injector.CLIPBOARD_SETTLE_TIMEOUT / 0.005)

        def sleep(seconds):
            now[0] += seconds
            slept.append(seconds)
            # The guard is here and not only on the clock because a loop without a
            # deadline never reads the clock at all; without it the failure would be a
            # hung test.
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
        # The other end of the loop: a tool that exits on its own ends the wait early,
        # and its exit is a failure carrying the reason it gave - saying otherwise would
        # send the user to paste an empty selection.
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
        # proc was assigned to the module global only after the write, so a write that
        # raised lost the process - and a lost wl-copy keeps the Wayland clipboard.
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

    The bundled copy is what makes automatic paste work on a distribution that does not
    package the helper at all - Debian 13 has no ``ydotool`` - and it is the one the
    package manager is never asked about.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bundled = Path(self.tmp.name)
        injector._helper_started_at = None
        self.addCleanup(setattr, injector, "_helper_started_at", None)
        answering = mock.patch.object(injector, "helper_answering", return_value=True)
        answering.start()
        self.addCleanup(answering.stop)
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
        # Running "ydotool" by name again would ignore the resolution and find whatever
        # PATH has, which is the case this whole fallback exists for.
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


class HelperSocketTests(unittest.TestCase):
    """Whether the ydotoold helper is there, asked of the socket and not of the file.

    A real socket in a temporary directory: the difference between "the file exists" and
    "something answers" is the whole point, and a mock could not tell a lying filesystem
    from a working one.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ydotool.sock"
        patcher = mock.patch.object(injector, "LEGACY_SOCKET", str(Path(self.tmp.name) / "legacy.sock"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bind(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.path))
        return listener

    def test_a_socket_nobody_answers_is_not_a_running_helper(self):
        # ydotoold is killed without removing its socket, so a file that is there says
        # nothing. The listener is closed on purpose; leaving it open would test the
        # case where the helper is alive.
        listener = self._bind()
        listener.close()
        self.assertTrue(self.path.exists(), "the stale socket should be left behind")
        self.assertFalse(injector.helper_answering(self.path))

    def test_a_bound_socket_answers(self):
        self._bind()
        self.assertTrue(injector.helper_answering(self.path))

    def test_a_missing_socket_does_not_raise(self):
        self.assertFalse(injector.helper_answering(Path(self.tmp.name) / "absent.sock"))

    def test_the_socket_of_the_packaged_helper_is_the_one_used(self):
        runtime = Path(self.tmp.name) / "run"
        runtime.mkdir()
        socket_path = runtime / "wayvoice-ydotool.sock"
        socket_path.touch()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            self.assertEqual(injector.ydotool_socket(), socket_path)
            self.assertEqual(injector._ydotool_env()["YDOTOOL_SOCKET"], str(socket_path))

    def test_the_legacy_default_is_used_when_the_helper_uses_it(self):
        # ydotool 0.1.8 puts its socket in /tmp and ignores XDG_RUNTIME_DIR. The path is
        # redirected into the temporary directory rather than created for real: a socket
        # left in /tmp can confuse the next ydotoold on the machine.
        runtime = Path(self.tmp.name) / "run"
        runtime.mkdir()
        legacy = Path(self.tmp.name) / "legacy.sock"
        legacy.touch()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
             mock.patch.object(injector, "LEGACY_SOCKET", str(legacy)):
            self.assertEqual(injector.ydotool_socket(), legacy)
            self.assertEqual(injector._ydotool_env()["YDOTOOL_SOCKET"], str(legacy))

    def test_the_packaged_socket_wins_over_the_legacy_one(self):
        runtime = Path(self.tmp.name) / "run"
        runtime.mkdir()
        packaged = runtime / "wayvoice-ydotool.sock"
        packaged.touch()
        legacy = Path(self.tmp.name) / "legacy.sock"
        legacy.touch()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
             mock.patch.object(injector, "LEGACY_SOCKET", str(legacy)):
            self.assertEqual(injector.ydotool_socket(), packaged)

    def test_no_socket_anywhere_means_no_socket_is_configured(self):
        runtime = Path(self.tmp.name) / "run"
        runtime.mkdir()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}), \
             mock.patch.object(injector, "ydotool_socket", return_value=None):
            self.assertIsNone(injector.ydotool_socket())
            self.assertNotIn("YDOTOOL_SOCKET", injector._ydotool_env())


class HelperStartTests(unittest.TestCase):
    """The daemon raises ydotoold itself when it is not answering.

    The unit is enabled at installation time, which does nothing for a session that was
    already open when the package arrived: nobody starts it, and every dictation is
    recognized and then not pasted until the next login. That is the failure reported as
    "it cannot paste anything", and the fix is one systemctl call the daemon can make by
    itself.
    """

    def setUp(self):
        injector._helper_started_at = None
        self.addCleanup(setattr, injector, "_helper_started_at", None)
        patcher = mock.patch.object(injector, "_helper_start_lock",
                                    injector.threading.Lock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _not_answering(self):
        return mock.patch.object(injector, "helper_answering", return_value=False)

    def _answering(self):
        return mock.patch.object(injector, "helper_answering", return_value=True)

    def test_a_dead_helper_is_started(self):
        with self._not_answering() as answering, mock.patch(
            "wayvoice.service.start_user_unit", return_value=True
        ) as start:
            injector.ensure_helper_running(wait=0)
        start.assert_called_once_with("wayvoice-ydotool.service")

    def test_a_live_helper_is_left_alone(self):
        with self._answering(), mock.patch(
            "wayvoice.service.start_user_unit"
        ) as start:
            self.assertTrue(injector.ensure_helper_running(wait=0))
        self.assertFalse(start.called, "a working helper was restarted anyway")

    def test_a_helper_that_answers_after_the_start_is_reported_as_up(self):
        answers = iter([False, False, True])
        with mock.patch.object(injector, "helper_answering",
                               side_effect=lambda *a, **k: next(answers)), \
             mock.patch("wayvoice.service.start_user_unit", return_value=True):
            self.assertTrue(injector.ensure_helper_running(wait=5))

    def test_a_helper_that_never_comes_up_is_reported_as_down(self):
        # Bounded, so a paste cannot hang on a systemctl that is stuck: the text is
        # already in the clipboard and the user is waiting for it.
        now = [0.0]
        slept = []

        def fake_sleep(seconds):
            now[0] += seconds
            slept.append(seconds)

        with mock.patch.object(injector, "helper_answering", return_value=False), \
             mock.patch("wayvoice.service.start_user_unit", return_value=True), \
             mock.patch.object(injector.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(injector.time, "monotonic", side_effect=lambda: now[0]):
            self.assertFalse(injector.ensure_helper_running(wait=1.0))
        self.assertTrue(slept, "the wait gave up without ever trying")
        self.assertLessEqual(sum(slept), 1.0 + 0.05,
                             "the wait ran past its deadline")

    def test_a_second_dictation_does_not_spawn_systemctl_again(self):
        with self._not_answering(), mock.patch(
            "wayvoice.service.start_user_unit", return_value=False
        ) as start:
            injector.ensure_helper_running(wait=0)
            injector.ensure_helper_running(wait=0)
            injector.ensure_helper_running(wait=0)
        self.assertEqual(start.call_count, 1,
                         "a helper that cannot start was asked for on every dictation")

    def test_the_start_is_tried_again_after_the_cooldown(self):
        later = injector.time.monotonic() + injector.HELPER_RETRY_INTERVAL + 1
        with self._not_answering(), mock.patch(
            "wayvoice.service.start_user_unit", return_value=False
        ) as start:
            injector.ensure_helper_running(wait=0)
            with mock.patch.object(injector.time, "monotonic", return_value=later):
                injector.ensure_helper_running(wait=0)
        self.assertEqual(start.call_count, 2)

    def test_a_system_without_a_user_manager_is_not_asked_to_start_anything(self):
        # A Flatpak sandbox has systemctl nowhere near it; the check is what keeps
        # the daemon from trying there on every dictation.
        with self._not_answering(), mock.patch(
            "wayvoice.service.systemd_available", return_value=False
        ), mock.patch("wayvoice.service._systemctl") as systemctl:
            self.assertFalse(injector.ensure_helper_running(wait=0))
        self.assertFalse(systemctl.called)


class PasteDiagnosticsTests(unittest.TestCase):
    """What the user is told when the paste fails, and what lands in the log."""

    def setUp(self):
        injector._helper_started_at = None
        self.addCleanup(setattr, injector, "_helper_started_at", None)
        # The failure line for the service log is asserted on its own below;
        # letting it reach the test output in the other cases would make a
        # passing run look like a failing one.
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def _ydotool(self, returncode=1, stdout="", stderr=""):
        cp = subprocess.CompletedProcess(["ydotool"], returncode, stdout, stderr)
        return mock.patch.object(injector, "_run", return_value=cp)

    def _present(self):
        return mock.patch.object(injector.shutil, "which", return_value="/usr/bin/ydotool")

    def test_ydotools_own_explanation_is_not_thrown_away(self):
        # ydotool prints its failures on stdout, and stdout went to /dev/null, so the
        # user got "ydotool exited with an error" and the only line naming the cause was
        # discarded.
        with self._present(), self._ydotool(
            stdout="failed to connect socket `/run/user/1000/wayvoice-ydotool.sock': "
                   "No such file or directory\nPlease check if ydotoold is running.\n"
        ), mock.patch.object(injector, "helper_answering", return_value=True), \
             mock.patch("wayvoice.service.start_user_unit", return_value=True):
            ok, message = injector.paste_with_ydotool("standard", "en")
        self.assertFalse(ok)
        self.assertIn("ydotoold is running", message)

    def test_stdout_is_captured_for_the_ydotool_call(self):
        seen = {}

        def record(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with self._present(), mock.patch.object(
            injector, "_run", side_effect=record
        ), mock.patch.object(injector, "helper_answering", return_value=True):
            injector.paste_with_ydotool("standard", "en")
        self.assertTrue(seen.get("capture_stdout"),
                        "the reason would be discarded again")

    def test_stderr_still_wins_when_ydotool_writes_there(self):
        with self._present(), self._ydotool(
            stdout="noise from stdout", stderr="ydotool: cannot open /dev/uinput"
        ), mock.patch.object(injector, "helper_answering", return_value=True):
            ok, message = injector.paste_with_ydotool("standard", "en")
        self.assertIn("/dev/uinput", message)
        self.assertNotIn("noise from stdout", message)

    def test_a_failure_that_leaves_nothing_to_say_is_still_reported(self):
        with self._present(), self._ydotool(returncode=1), \
             mock.patch.object(injector, "helper_answering", return_value=True):
            ok, message = injector.paste_with_ydotool("standard", "en")
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_a_failure_is_written_to_the_service_log(self):
        # "It does not paste" has to be a line in journalctl, or the only way to find
        # out is to ask the user to describe what they see.
        stderr = io.StringIO()
        with self._present(), self._ydotool(stdout="Please check if ydotoold is running."), \
             mock.patch.object(injector, "helper_answering", return_value=True), \
             contextlib.redirect_stderr(stderr):
            injector.paste_with_ydotool("standard", "en")
        self.assertIn("auto-paste failed", stderr.getvalue())

    def test_a_helper_that_will_not_start_names_the_unit_to_start(self):
        with self._present(), self._ydotool(stdout="Please check if ydotoold is running."), \
             mock.patch.object(injector, "helper_answering", return_value=False), \
             mock.patch("wayvoice.service.systemd_available", return_value=False):
            ok, message = injector.paste_with_ydotool("standard", "en")
        self.assertFalse(ok)
        self.assertIn("wayvoice-ydotool.service", message)

    def test_the_helper_is_raised_before_typing_into_the_void(self):
        order = []

        def probe(*_a, **_k):
            order.append("probe")
            return True

        def run(cmd, **_k):
            order.append("type")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with self._present(), mock.patch.object(injector, "helper_answering", side_effect=probe), \
             mock.patch.object(injector, "_run", side_effect=run):
            injector.paste_with_ydotool("standard", "en")
        self.assertEqual(order[0], "probe", "the helper was checked after typing")


class InjectTests(unittest.TestCase):
    """The three paste modes, with the clipboard step faked away."""

    def setUp(self):
        injector._clipboard_proc = None
        self.addCleanup(setattr, injector, "_clipboard_proc", None)
        injector._helper_started_at = None
        self.addCleanup(setattr, injector, "_helper_started_at", None)
        # The helper probe talks to a real socket and, when nothing answers, to a real
        # systemd. Depending on whether the machine running this has ydotoold up is
        # depending on the wrong computer, so the answer is fixed here.
        answering = mock.patch.object(injector, "helper_answering", return_value=True)
        answering.start()
        self.addCleanup(answering.stop)

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
        # The program that runs is the resolved path, not the bare name: that is what
        # lets the bundled copy be used on a system whose PATH has no ydotool in it.
        self.assertEqual(seen["cmd"][0], "/usr/bin/ydotool")
        # 29:x are the bracket-key codes: the terminal profile sends ESC [ once.
        self.assertEqual(seen["cmd"][1:3], ["key", "29:1"])
        self.assertIn("47:1", seen["cmd"])


if __name__ == "__main__":
    unittest.main()
