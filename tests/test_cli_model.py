"""``wayvoice model`` from a terminal.

The download is the daemon's child process, so the command line can only ask
the daemon to start or stop it.  These tests pin that routing down, including
the case where nothing is running to ask - which has to be said out loud,
because a download started here would land in a cache no daemon reads.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from wayvoice import cli


def run_cli(argv):
    """Run ``main`` with a fake argv, returning (exit code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(cli.sys, "argv", ["wayvoice"] + argv):
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class ModelCommandTests(unittest.TestCase):
    def test_it_reports_the_model_state_the_daemon_knows(self):
        reply = {"ok": True, "model": {"present": False, "model": "medium",
                                       "download": {"state": "idle"}}}
        with mock.patch.object(cli, "request", return_value=reply) as ask:
            code, out, _err = run_cli(["model"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["model"], "medium")
        self.assertEqual(ask.call_args[0][0], "status")

    def test_download_asks_the_daemon_to_start_one(self):
        with mock.patch.object(cli, "request", return_value={"ok": True}) as ask:
            code, out, _err = run_cli(["model", "--download"])
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        self.assertEqual(ask.call_args[0][0], "prepare-model")
        # The daemon starts a thread and returns; the reply must not wait for
        # a download that can take hours.
        self.assertLessEqual(ask.call_args[1]["timeout"], 10.0)

    def test_cancel_asks_the_daemon_to_stop_one(self):
        with mock.patch.object(cli, "request", return_value={"ok": True}) as ask:
            code, _out, _err = run_cli(["model", "--cancel"])
        self.assertEqual(code, 0)
        self.assertEqual(ask.call_args[0][0], "cancel-download")

    def test_a_daemon_that_refuses_says_why(self):
        with mock.patch.object(
            cli, "request", return_value={"ok": False, "error": "the model is downloading"}
        ):
            code, _out, err = run_cli(["model", "--download"])
        self.assertEqual(code, 1)
        self.assertIn("the model is downloading", err)

    def test_nothing_running_is_reported_rather_than_downloaded_here(self):
        # Downloading into a cache the daemon does not read would look like it
        # worked and change nothing.
        with mock.patch.object(
            cli, "request", return_value={"ok": False, "error": "not running"}
        ):
            code, out, err = run_cli(["model", "--download"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("not running", err)

    def test_an_unknown_option_is_a_usage_error(self):
        code, _out, err = run_cli(["model", "--wat"])
        self.assertEqual(code, 2)
        self.assertIn("--download", err)

    def test_a_model_name_is_refused_rather_than_ignored(self):
        # It looks like the natural spelling, and it used to be accepted and
        # silently ignored: the daemon downloaded the model the settings named
        # and the command reported success.  A quiet wrong answer is worse than
        # a refusal, so the name is rejected and the user is told where the
        # model is chosen.
        # The message is the user's language, so the test asks for one.
        with mock.patch.object(cli, "request") as ask, \
             mock.patch.object(cli, "_language", return_value="en"):
            code, out, err = run_cli(["model", "--download", "medium"])
        self.assertEqual(code, 2)
        self.assertFalse(ask.called, "the command was sent anyway")
        self.assertEqual(out, "")
        self.assertIn("settings", err.lower())

    def test_the_command_is_listed_in_the_usage_line(self):
        code, _out, err = run_cli(["nonsense"])
        self.assertEqual(code, 2)
        self.assertIn("model", err)


if __name__ == "__main__":
    unittest.main()
