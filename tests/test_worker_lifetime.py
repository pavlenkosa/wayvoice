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
from wayvoice.runtime import locations, worker_client

import subprocess
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

        with mock.patch.object(worker_client, "_worker_ping", side_effect=fake_ping), \
             mock.patch.object(locations, "worker_pid_path", return_value=self.pid_file), \
             mock.patch.object(locations, "worker_socket_path",
                               return_value=self.socket_file), \
             mock.patch.object(worker_client, "_is_worker_process",
                               return_value=stopped_by_signal), \
             mock.patch.object(worker_client, "_pid_alive", return_value=False), \
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

    def test_worker_settings_validate_beam_size(self):
        cases = [(None, 5), ("soon", 5), ([], 5), ({}, 5),
                 (float("nan"), 5), (float("inf"), 5), ("2.5", 5),
                 (3, 3), ("3", 3)]
        for value, expected in cases:
            with self.subTest(value=value):
                actual = worker_client._worker_settings({"beam_size": value})["beam_size"]
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), int)

    def test_the_helper_keeps_integers_and_floats_apart(self):
        from wayvoice.config import number

        for value, default, expected in (("2", 1.0, 2.0), ("2", 1, 2),
                                         ("2.5", 1, 1), ("soon", 1.5, 1.5)):
            with self.subTest(value=value, default=default):
                actual = number({"x": value}, "x", default)
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), type(expected))

    def test_malformed_numbers_reach_engine_commands_as_defaults(self):
        from wayvoice.runtime import parakeet, processes

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "bin").mkdir()
            (runtime / "bin/python").touch()
            model = runtime / "model.bin"
            model.touch()
            for value in (None, "soon", [], {}, float("nan"), float("inf")):
                cfg = {"engine_worker": False, "beam_size": value,
                       "transcription_timeout_sec": value, "custom_command": "recognize {audio}",
                       "whisper_cpp_model": str(model)}
                with self.subTest(value=value), \
                        mock.patch.object(locations, "faster_runtime", return_value=runtime), \
                        mock.patch.object(engine_mod, "_find_whisper_cpp", return_value="whisper-cli"), \
                        mock.patch.object(parakeet, "command", return_value=["parakeet"]), \
                        mock.patch.object(processes, "_run_cancelable") as run:
                    with mock.patch.object(worker_client, "ensure_worker", return_value=True), \
                            mock.patch.object(worker_client, "_worker_request", return_value={"ok": True, "text": "transcript"}) as request:
                        self.assertEqual(engine_mod._transcribe_via_worker(model, cfg, None), "transcript")
                        self.assertEqual(request.call_args.args[2], 90)
                        self.assertIs(type(request.call_args.args[2]), int)
                    for transcribe in (engine_mod._transcribe_faster, engine_mod._transcribe_custom,
                                       engine_mod._transcribe_whisper_cpp, parakeet.transcribe):
                        run.return_value = subprocess.CompletedProcess([], 0,
                            '{"text":"transcript"}' if transcribe is parakeet.transcribe else "transcript", "")
                        self.assertEqual(transcribe(model, cfg, None), "transcript")
                        self.assertEqual(run.call_args.kwargs["timeout"], 90)
                        self.assertIs(type(run.call_args.kwargs["timeout"]), int)
                        if transcribe is engine_mod._transcribe_faster:
                            command = run.call_args.args[0]
                            self.assertEqual(command[command.index("--beam-size") + 1], "5")

    def test_worker_spawn_validates_beam_and_idle_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "bin").mkdir()
            (runtime / "bin/python").touch()
            for value in (None, "soon", [], {}, float("nan"), float("inf")):
                with self.subTest(value=value), \
                        mock.patch.object(locations, "faster_runtime", return_value=runtime), \
                        mock.patch.object(locations, "worker_log_path", return_value=runtime / "worker.log"), \
                        mock.patch.object(locations, "worker_pid_path", return_value=runtime / "worker.pid"), \
                        mock.patch.object(locations, "worker_socket_path", return_value=runtime / "worker.sock"), \
                        mock.patch.object(worker_client, "_worker_procs", []), \
                        mock.patch.object(worker_client.subprocess, "Popen", return_value=mock.Mock(pid=123)) as spawn:
                    self.assertTrue(worker_client._start_worker({"beam_size": value, "engine_worker_idle_sec": value}))
                    command = spawn.call_args.args[0]
                    self.assertEqual(command[command.index("--beam-size") + 1], "5")
                    self.assertEqual(command[command.index("--idle-timeout") + 1], "900")


class PreparationThreadTests(unittest.TestCase):
    """No path out of the preparation thread may leave it permanently busy."""

    def setUp(self):
        import threading

        self.daemon = WayVoiceDaemon()
        self.daemon._lock = threading.RLock()
        self.daemon.preparation.running = True
        self.daemon.preparation.cancel_event = threading.Event()
        self.daemon.preparation.report = {}
        self.daemon.last_error = ""

    def _worker(self, **patch_kwargs):
        engine_patch = mock.patch("wayvoice.daemon_preparation.engine_from_config",
                                  return_value=mock.Mock())
        engine_patch.start()
        self.addCleanup(engine_patch.stop)
        prepare_patch = mock.patch("wayvoice.daemon_preparation.prepare_model", **patch_kwargs)
        prepare_patch.start()
        self.addCleanup(prepare_patch.stop)
        self.daemon.preparation.run(mock.Mock(), {"model": "small"}, False)

    def test_a_raising_prepare_model_does_not_wedge_the_daemon(self):
        # The regression: the thread ended at the raise, so _prepare_running stayed
        # True and every later request was refused with "already preparing".
        self._worker(side_effect=RuntimeError("probe failed"))
        self.assertFalse(self.daemon.preparation.running)
        self.assertIsNone(self.daemon.preparation.thread)
        self.assertEqual(self.daemon.preparation.report["state"], "error")
        self.assertIn("probe failed", self.daemon.preparation.report["error"])

    def test_the_answer_is_still_published_on_success(self):
        self._worker(return_value={"state": "ready", "done": 1, "total": 2, "error": ""})
        self.assertEqual(self.daemon.preparation.report["state"], "ready")
        self.assertEqual(self.daemon.preparation.report["total_bytes"], 2)
        self.assertFalse(self.daemon.preparation.report["warming"])
        self.assertFalse(self.daemon.preparation.running)

    def test_a_cancelled_prepare_model_is_not_an_error_state(self):
        from wayvoice.engine import TranscriptionCancelled

        self._worker(side_effect=TranscriptionCancelled())
        self.assertFalse(self.daemon.preparation.running)
        self.assertNotEqual(self.daemon.preparation.report["state"], "error")


if __name__ == "__main__":
    unittest.main()
