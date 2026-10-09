"""Three ways the warm worker stopped being used, none of them reported.

1. ``stop_worker()`` unlinked a live socket. It unlinked when ``stopped`` was true,
   and ``stopped`` says "a pid that looked like a worker was signalled" - not "the
   worker that owns this socket is gone". With a stale pid file the daemon signalled
   the wrong process, started a second worker that could not bind because the first
   still held the socket, wrote the second pid, and then stopped *that* one - at
   which point the first worker's socket was unlinked. It kept running, holding the
   model and gigabytes of RAM, invisible to ``_worker_ping()``, which gives up on a
   path that does not exist. Every dictation then fell back to loading the model from
   scratch: for ``large-v3`` that is minutes instead of seconds, with no error, no
   ``last_error`` and no notification anywhere.

2. A number in ``config.json`` that is not a number raised inside the preparation
   thread, which had no exception handler, so ``_prepare_running`` stayed set and
   preparation was refused for the rest of the session.

3. ``prepare_model`` raising at all - not only because of a bad number - had the same
   effect, and its own contract says it answers with a dict.

The first is the reason these were hard to see: nothing fails, nothing is logged, the
application simply gets slower and stays slower.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine as engine_mod
from wayvoice.daemon import WayVoiceDaemon


class StopWorkerSocketTests(unittest.TestCase):
    """The socket belongs to whoever is listening on it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.pid_file = self.dir / "worker.pid"
        self.pid_file.write_text("1234\n", encoding="utf-8")
        self.socket_file = self.dir / "worker.sock"
        # A real file: the point of the test is whether it is still there afterwards,
        # and a path that never existed would make missing_ok swallow the answer.
        self.socket_file.touch()

    def _stop(self, *, alive, stopped_by_signal):
        """Run stop_worker with the three pieces that matter replaced."""
        def fake_ping():
            return {"ok": True} if alive else None

        with mock.patch.object(engine_mod, "_worker_ping", side_effect=fake_ping), \
             mock.patch.object(engine_mod, "worker_pid_path", return_value=self.pid_file), \
             mock.patch.object(engine_mod, "worker_socket_path",
                               return_value=self.socket_file), \
             mock.patch.object(engine_mod, "_is_worker_process",
                               return_value=stopped_by_signal), \
             mock.patch.object(engine_mod, "_pid_alive", return_value=False), \
             mock.patch.object(engine_mod.os, "kill"):
            return engine_mod.stop_worker()

    def test_a_socket_that_still_answers_is_left_alone(self):
        # The regression. The worker we failed to identify is still listening, and
        # unlinking its socket is what made it invisible for the rest of the session.
        self._stop(alive=True, stopped_by_signal=True)
        self.assertTrue(self.socket_file.exists(),
                        "the live worker's socket was unlinked because a signal was sent")

    def test_a_dead_socket_is_cleaned_up(self):
        # The other half: a socket nobody answers on belongs to nobody, and leaving it
        # makes every later connection fail.
        self._stop(alive=False, stopped_by_signal=True)
        self.assertFalse(self.socket_file.exists(), "a stale socket was left behind")

    def test_a_socket_is_removed_even_when_nothing_was_signalled(self):
        self._stop(alive=False, stopped_by_signal=False)
        self.assertFalse(self.socket_file.exists())

    def test_the_pid_file_is_removed_even_when_nothing_was_signalled(self):
        self._stop(alive=False, stopped_by_signal=False)
        self.assertFalse(self.pid_file.exists(), "a stale pid file was left behind")


class NumericSettingsTests(unittest.TestCase):
    """A config file a person edited must not raise inside the engine."""

    def test_beam_size_falls_back_instead_of_raising(self):
        for value in (None, "soon", [], {}, "5"):
            with self.subTest(value=value):
                settings = engine_mod._worker_settings({"beam_size": value})
                self.assertIsInstance(settings["beam_size"], int)

    def test_the_default_is_used_when_the_value_is_not_a_number(self):
        self.assertEqual(engine_mod._worker_settings({"beam_size": None})["beam_size"], 5)
        self.assertEqual(engine_mod._worker_settings({"beam_size": "x"})["beam_size"], 5)

    def test_a_good_value_is_kept(self):
        self.assertEqual(engine_mod._worker_settings({"beam_size": 3})["beam_size"], 3)
        self.assertEqual(engine_mod._worker_settings({"beam_size": "3"})["beam_size"], 3)

    def test_the_helper_keeps_integers_and_floats_apart(self):
        from wayvoice.config import number

        self.assertEqual(number({"x": "2"}, "x", 1.0), 2.0)
        self.assertEqual(number({"x": "2"}, "x", 1), 2)
        # "2.5" is not an integer, and quietly dropping the fraction would change the
        # setting the user wrote, so the default stands.
        self.assertEqual(number({"x": "2.5"}, "x", 1), 1)
        self.assertEqual(number({"x": "soon"}, "x", 1.5), 1.5)

    def test_no_unguarded_conversion_of_a_config_value_is_left(self):
        # A guard rather than a test of one line: the sixth such conversion is what
        # the next bad config file finds.
        import ast

        offenders = []
        for path in sorted(Path(engine_mod.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id not in {"int", "float"}:
                    continue
                for argument in node.args:
                    if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Attribute) \
                            and argument.func.attr == "get":
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "cfg.get() converted without a floor:\n" + "\n".join(offenders))


class PreparationThreadTests(unittest.TestCase):
    """No path out of the preparation thread may leave it permanently busy."""

    def setUp(self):
        import threading

        self.daemon = WayVoiceDaemon.__new__(WayVoiceDaemon)
        self.daemon._lock = threading.RLock()
        self.daemon._prepare_running = True
        self.daemon._prepare_cancel = threading.Event()
        self.daemon._download = {}
        self.daemon.last_error = ""

    def _worker(self, **patch_kwargs):
        engine_patch = mock.patch("wayvoice.daemon.engine_from_config",
                                  return_value=mock.Mock())
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        prepare_patch = mock.patch("wayvoice.daemon.prepare_model", **patch_kwargs)
        prepare_patch.start()
        self.addCleanup(prepare_patch.stop)
        self.daemon._prepare_model_worker(mock.Mock(), {"model": "small"}, False)

    def test_a_raising_prepare_model_does_not_wedge_the_daemon(self):
        # The regression: the thread ended at the raise, so _prepare_running stayed
        # True and every later request was refused with "already preparing".
        self._worker(side_effect=RuntimeError("probe failed"))
        self.assertFalse(self.daemon._prepare_running)
        self.assertIsNone(self.daemon._prepare_thread)
        self.assertEqual(self.daemon._download["state"], "error")
        self.assertIn("probe failed", self.daemon._download["error"])

    def test_the_answer_is_still_published_on_success(self):
        self._worker(return_value={"state": "ready", "done": 1, "total": 2, "error": ""})
        self.assertEqual(self.daemon._download["state"], "ready")
        self.assertEqual(self.daemon._download["total_bytes"], 2)
        self.assertFalse(self.daemon._download["warming"])
        self.assertFalse(self.daemon._prepare_running)

    def test_a_cancelled_prepare_model_is_not_an_error_state(self):
        from wayvoice.engine import TranscriptionCancelled

        self._worker(side_effect=TranscriptionCancelled())
        self.assertFalse(self.daemon._prepare_running)
        self.assertNotEqual(self.daemon._download["state"], "error")


if __name__ == "__main__":
    unittest.main()
