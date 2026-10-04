"""One notification per dictation, updated in place.

A dictation passes through three states - recording, transcribing, done - and
each of them used to arrive as its own popup, so the notification center filled
up with three lines per dictation and stopped being read. The notification the
server already has is now replaced instead of repeated.

``notify-send`` is faked; what is under test is which id this module remembers
and what it does with it.
"""

import subprocess
import threading
import time
import unittest
from unittest import mock

from wayvoice import notify as notify_mod


class FakeProcess:
    """The part of a started program this module uses."""

    def __init__(self, printed=""):
        self.printed = printed
        self.waited = 0
        self.killed = False

    def communicate(self, input=None, timeout=None):
        self.waited += 1
        return self.printed, ""

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class FakeNotifySend:
    """A stand-in for ``notify-send``: records its arguments, hands out ids."""

    def __init__(self, printed="7"):
        self.calls = []
        self.kwargs = []
        self.printed = printed
        self.processes = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.kwargs.append(kwargs)
        process = FakeProcess(self.printed)
        self.processes.append(process)
        return process

    def has(self, flag, index=0):
        return flag in self.calls[index]

    def value_after(self, flag, index=0):
        args = self.calls[index]
        return args[args.index(flag) + 1] if flag in args else None


class NotifyTests(unittest.TestCase):
    def setUp(self):
        notify_mod.reset_notification_id()
        self.addCleanup(notify_mod.reset_notification_id)
        patch = mock.patch.object(
            notify_mod.shutil, "which", return_value="/usr/bin/notify-send"
        )
        patch.start()
        self.addCleanup(patch.stop)

        self.fake = FakeNotifySend()
        popen = mock.patch.object(
            notify_mod.subprocess, "Popen", side_effect=self.fake
        )
        popen.start()
        self.addCleanup(popen.stop)

        # notify() hands the program to a thread of its own, so the id is not
        # remembered until that thread runs. Only the threads this module starts
        # are recorded and joined - every other thread in the process belongs to
        # somebody else, and joining one of those would be a test that hangs for
        # reasons of its own.
        self.threads = []
        threads = self.threads
        real_thread = threading.Thread

        def recording_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            if kwargs.get("target") is notify_mod._deliver:
                threads.append(thread)
            return thread

        patch = mock.patch.object(
            notify_mod.threading, "Thread", recording_thread
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _deliver(self):
        for thread in list(self.threads):
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "the delivery thread did not finish")

    def test_the_first_notification_asks_for_no_replacement(self):
        # Nothing of ours is on the screen yet, so there is nothing to replace;
        # an invented id would land on another program's notification.
        notify_mod.notify("WayVoice", "recording started")
        self._deliver()
        self.assertFalse(self.fake.has("-r"))
        self.assertEqual(notify_mod.notification_id(), 7)

    def test_the_next_one_replaces_the_last(self):
        notify_mod.notify("WayVoice", "recording started")
        self._deliver()
        notify_mod.notify("WayVoice", "transcribing")
        self._deliver()
        self.assertTrue(self.fake.has("-r", 1))
        self.assertEqual(self.fake.value_after("-r", 1), "7")

    def test_the_id_the_server_returns_wins_over_the_one_we_had(self):
        # A notification the user dismissed is no longer replaceable. The id this
        # call prints is the truth, so it is what the next call uses.
        self.fake.printed = "8"
        notify_mod.notify("WayVoice", "text inserted")
        self._deliver()
        self.assertEqual(notify_mod.notification_id(), 8)
        notify_mod.notify("WayVoice", "recording started")
        self._deliver()
        self.assertEqual(self.fake.value_after("-r", 1), "8")

    def test_three_states_of_one_dictation_are_three_updates_of_one_entry(self):
        for state in ("recording started", "transcribing", "text inserted"):
            notify_mod.notify("WayVoice", state)
            self._deliver()
        self.assertEqual(len(self.fake.calls), 3)
        fresh = [i for i, _call in enumerate(self.fake.calls) if "-r" not in _call]
        self.assertEqual(fresh, [0],
                         "a state started a new notification instead of updating one")

    def test_forgetting_the_id_starts_a_new_entry(self):
        # What the daemon does between dictations: the transcript of the last one
        # stays readable instead of being overwritten by the next.
        notify_mod.notify("WayVoice", "hello")
        self._deliver()
        notify_mod.reset_notification_id()
        notify_mod.notify("WayVoice", "recording started", replace=False)
        self._deliver()
        self.assertFalse(self.fake.has("-r", 1))

    def test_output_that_is_not_an_id_is_not_used_as_one(self):
        # notify-send may print something else entirely, and a wrong id would
        # replace a notification that belongs to another program.
        self.fake.printed = "Gdk-Message: failed to send message"
        notify_mod.notify("WayVoice", "recording started")
        self._deliver()
        self.assertIsNone(notify_mod.notification_id())
        notify_mod.notify("WayVoice", "transcribing")
        self._deliver()
        self.assertFalse(self.fake.has("-r", 1),
                         "a notification was replaced by an id that was never an id")

    def test_empty_output_is_not_an_id_either(self):
        self.fake.printed = ""
        notify_mod.notify("WayVoice", "recording started")
        self._deliver()
        self.assertIsNone(notify_mod.notification_id())

    def test_the_id_is_read_from_stdout(self):
        # Without the pipe the id never arrives, and every notification after the
        # first is a new popup: the behaviour this replaces.
        notify_mod.notify("WayVoice", "recording started")
        self._deliver()
        self.assertEqual(self.fake.kwargs[0].get("stdout"), subprocess.PIPE)

    def test_notify_returns_before_the_program_finishes(self):
        # The id is read on the background thread, so the hot key is never held
        # waiting for a session bus that may be slow or gone.
        began = time.monotonic()
        notify_mod.notify("WayVoice", "recording started")
        self.assertLess(time.monotonic() - began, 0.5)

    def test_nothing_is_raised_when_the_delivery_reports_nothing(self):
        fake = mock.Mock()
        fake.communicate.side_effect = OSError("no session bus")
        with mock.patch.object(notify_mod.subprocess, "Popen", return_value=fake):
            notify_mod.notify("WayVoice", "text")
            self._deliver()
        self.assertIsNone(notify_mod.notification_id())

    def test_a_program_that_overstays_is_killed_not_waited_for(self):
        fake = mock.Mock()
        fake.communicate.side_effect = subprocess.TimeoutExpired("notify-send", 5)
        with mock.patch.object(notify_mod.subprocess, "Popen", return_value=fake):
            notify_mod.notify("WayVoice", "text")
            self._deliver()
        self.assertTrue(fake.kill.called, "a wedged notification program was left")

    def test_a_stand_in_that_returns_nothing_does_not_break_the_next_call(self):
        # A test double elsewhere in the suite returns a bare object from
        # communicate(); unpacking that blindly would raise on every dictation.
        fake = mock.Mock()
        fake.communicate.return_value = None
        with mock.patch.object(notify_mod.subprocess, "Popen", return_value=fake):
            notify_mod.notify("WayVoice", "text")
            self._deliver()
        self.assertIsNone(notify_mod.notification_id())


if __name__ == "__main__":
    unittest.main()
