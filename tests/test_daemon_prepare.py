"""What the hot key does while a model is still being fetched.

The change these tests cover: pressing the key used to start a dictation that
could not finish, because the weights were being downloaded inside the
transcription - silently, for minutes, ending in a timeout.  Now the daemon
refuses with an explanation, fetches the model in the background, and reports
what that fetch is doing.

The engine is faked: what is under test is the daemon's own behaviour, not the
hub.
"""

import contextlib
import threading
import types
import time
import unittest
from unittest import mock

from wayvoice import daemon as daemon_mod
from wayvoice.daemon import WayVoiceDaemon
from wayvoice.engine import Engine
from wayvoice.protocol import socket_path

from support import isolate_engine, isolate_environment


def make_engine(*, present: bool, states: list[str] | None = None, progress=None,
                warm: bool = False, delay: float = 0.0):
    """A stand-in engine with the model hooks the daemon uses.

    ``states`` is consumed one entry per download call, so a test can run a
    download that reports progress and then fails.
    """
    calls = {"n": 0, "progress": 0, "state_index": 0}

    def reset():
        """Forget earlier runs; a daemon prepares its model on construction."""
        calls.update(n=0, progress=0, state_index=0)

    def model_present(cfg):
        return present

    def model_download(cfg, on_progress=None, cancel_event=None):
        index = calls["state_index"]
        calls["state_index"] += 1
        calls["n"] += 1
        state = states[index] if states and index < len(states) else "ready"
        for step in range(1, 4):
            if cancel_event is not None and cancel_event.is_set():
                return {"state": "cancelled", "error": "Download cancelled",
                        "done": step * 10, "total": 30}
            if progress is not None:
                on_progress(step * 10, 30)
                calls["progress"] += 1
            time.sleep(delay)
        return {
            "state": state,
            "error": "download failed" if state == "error" else "",
            "done": 30,
            "total": 30,
        }

    calls["reset"] = reset
    engine = Engine(
        id="faster-whisper",
        label="Faster-Whisper",
        transcribe=lambda *a, **k: "",
        status=lambda cfg: {"state": "ready", "message": ""},
        uses_models=True,
        needs_setup=False,
        # "engine_worker" is what says this engine has a warm worker at all; the
        # real registry lists it for Faster-Whisper only.
        settings=("model", "engine_worker"),
        model_present=model_present,
        model_download=model_download,
    )
    return engine, calls


class DaemonCase(unittest.TestCase):
    """A test with a place to keep the patches it started."""

    def setUp(self):
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        # A daemon reads the user's config, walks the user's model cache and
        # starts a warm worker for it; none of that belongs in a test run.
        self.root = isolate_environment(self)
        isolate_engine(self)

    def patch(self, target: str, **kwargs):
        return self.patches.enter_context(mock.patch(target, **kwargs))


class PrepareDaemon:
    """A daemon with its engine and settings replaced, and no socket.

    The config is pinned to one model so that what the tests assert about it is
    not whatever this machine happens to have selected.
    """

    #: What the daemon under test believes the user chose.
    #:
    #: The language is pinned because replies are translated: a test that asserts
    #: on a message must not depend on the locale of the machine it runs on.
    CONFIG = {
        "model": "small", "engine_worker": True, "notify": False,
        "ui_language": "en",
    }

    def __init__(self, test: DaemonCase, engine, calls, config=None):
        self.test = test
        self.config = dict(self.CONFIG)
        self.config.update(config or {})
        test.patch("wayvoice.daemon.engine_from_config", return_value=engine)
        # Warming is the worker's business; here it is only ever a return value.
        test.patch("wayvoice.engine.warm_worker", return_value=True)
        test.patch("wayvoice.daemon.load_config", return_value=dict(self.config))
        self.daemon = WayVoiceDaemon()
        # A Mock would answer "recording" to everything, and the daemon would
        # then take the hot key as a request to stop.
        self.daemon.recorder = mock.Mock(recording=False)
        self.daemon._prepare_engine = mock.Mock()
        # The daemon prepares its model as soon as it is constructed, which is
        # the behaviour under test elsewhere; here it only has to finish and be
        # forgotten, so that each test counts the downloads it asked for.
        self.settle()
        calls["reset"]()
        self.calls = calls

    def settle(self, timeout: float = 10.0) -> None:
        """Wait until no download is in flight."""
        deadline = time.monotonic() + timeout
        while self.daemon._prepare_thread is not None and time.monotonic() < deadline:
            time.sleep(0.02)

    @property
    def download(self):
        return self.daemon.status()["model"]["download"]

    def wait_for(self, state: str, timeout: float = 10.0) -> dict:
        """Wait until the daemon reports a given download state."""
        deadline = time.monotonic() + timeout
        seen = self.download
        while time.monotonic() < deadline:
            seen = self.download
            if seen.get("state") == state:
                return seen
            time.sleep(0.02)
        self.test.fail(f"the download never reached {state!r}; last was {seen!r}")


class PrepareTests(DaemonCase):
    """Fetching the model in the background."""

    def test_a_missing_model_starts_a_download(self):
        eng, calls = make_engine(present=False, progress=True)
        harness = PrepareDaemon(self, eng, calls)
        self.assertTrue(harness.daemon._start_model_prepare({}))
        harness.wait_for("ready")
        self.assertEqual(calls["n"], 1)
        self.assertGreater(calls["progress"], 0, "no progress reached the daemon")

    def test_progress_appears_in_the_status_the_window_reads(self):
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        harness.daemon._start_model_prepare({})
        seen = harness.wait_for("ready")
        self.assertEqual(seen["done_bytes"], 30)
        self.assertEqual(seen["total_bytes"], 30)
        self.assertEqual(seen["model"], "small")

    def test_the_model_is_reported_as_missing_while_it_is_missing(self):
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        harness.daemon._start_model_prepare({})
        report = harness.daemon.status()["model"]
        self.assertTrue(report["supported"])
        self.assertFalse(report["present"])
        self.assertEqual(report["download"]["state"], "downloading")

    def test_a_model_that_is_there_is_never_downloaded(self):
        # The daemon asks on every start; a download of something already
        # present would fight with the recognizer over the same files. Asking to
        # prepare such a model warms it instead - so what is checked here is that
        # nothing was fetched, and what phase was reported: "downloading" for a
        # model with its weights already on disk would be a promise of a progress
        # bar that never moves.
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls)
        self.assertTrue(harness.daemon._start_model_prepare({}))
        harness.wait_for("ready")
        self.assertEqual(calls["n"], 0, "a model that was there was fetched")
        report = harness.daemon.status()["model"]
        self.assertTrue(report["present"])
        self.assertEqual(report["download"]["state"], "ready")

    def test_a_model_that_is_there_with_the_worker_off_starts_nothing(self):
        # Nothing to fetch and nobody to load it into: there is no work here, and
        # saying otherwise would put a progress bar on the screen for a job that
        # does not exist.
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls, config={"engine_worker": False})
        self.assertFalse(harness.daemon._start_model_prepare({}))
        reply = harness.daemon.dispatch("prepare-model")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["state"], "ready")

    def test_two_downloads_are_not_started_at_once(self):
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        self.assertTrue(harness.daemon._start_model_prepare({}))
        self.assertFalse(
            harness.daemon._start_model_prepare({}),
            "a second download was started for the same model",
        )
        harness.wait_for("ready")
        self.assertEqual(calls["n"], 1)

    def test_a_failed_download_is_reported_with_its_reason(self):
        eng, calls = make_engine(present=False, states=["error"])
        harness = PrepareDaemon(self, eng, calls)
        harness.daemon._start_model_prepare({})
        seen = harness.wait_for("error")
        self.assertEqual(seen["error"], "download failed")

    def test_a_cancel_stops_the_download_and_says_so(self):
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        harness.daemon._start_model_prepare({})
        deadline = time.monotonic() + 5.0
        while harness.download.get("state") != "downloading" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(harness.daemon._cancel_model_prepare())
        seen = harness.wait_for("cancelled")
        self.assertEqual(seen["state"], "cancelled")

    def test_cancelling_when_nothing_runs_says_nothing_was_running(self):
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls)
        self.assertEqual(harness.daemon._cancel_model_prepare(), "nothing")
        reply = harness.daemon.dispatch("cancel-download")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["phase"], "nothing")

    def test_cancelling_a_warm_up_says_it_will_finish_by_itself(self):
        # The load cannot be interrupted, so the answer must not claim that it
        # was stopped: the window would keep waiting for a stop that never came.
        holding = threading.Event()
        release = threading.Event()

        def warm(cfg):
            holding.set()
            release.wait(10.0)
            return True

        eng, calls = make_engine(present=False, progress=True)
        harness = PrepareDaemon(self, eng, calls)
        self.patch("wayvoice.engine.warm_worker", side_effect=warm)
        self.addCleanup(release.set)
        harness.daemon._start_model_prepare({})
        self.assertTrue(holding.wait(10.0), "the warm-up never started")
        reply = harness.daemon.dispatch("cancel-download")
        self.assertEqual(reply["phase"], "warming")
        # The flag is not set for a load: there is nothing that could use it.
        self.assertFalse(harness.daemon._prepare_cancel.is_set())

    def test_an_engine_without_models_is_left_alone(self):
        eng, calls = make_engine(present=True)
        from dataclasses import replace

        bare = replace(eng, model_present=None, model_download=None)
        harness = PrepareDaemon(self, bare, calls)
        report = harness.daemon.status()["model"]
        self.assertFalse(report["supported"])
        self.assertTrue(report["present"])

    def test_warming_is_reported_while_it_happens(self):
        # Warming is the second half of the job, and from the user's side it is
        # the same wait: the model is not in memory yet. Reported only in the
        # final answer, the window would sit at 100% for the whole load.
        started = threading.Event()
        finish = threading.Event()

        def warm(cfg):
            # Hold the load open, so the report can be read while it happens.
            started.set()
            finish.wait(10.0)
            return True

        eng, calls = make_engine(present=False, progress=True)
        harness = PrepareDaemon(self, eng, calls)
        self.patch("wayvoice.engine.warm_worker", side_effect=warm)
        harness.daemon._start_model_prepare({})
        self.addCleanup(finish.set)
        self.assertTrue(started.wait(10.0), "the warm-up never started")
        deadline = time.monotonic() + 5.0
        reported = False
        while time.monotonic() < deadline and not reported:
            report = harness.daemon.status()["model"]
            reported = report["warming"]
            self.assertEqual(report["download"]["state"], "downloading")
            time.sleep(0.005)
        self.assertTrue(reported, "the warm-up was never reported")


class HotKeyTests(DaemonCase):
    """The key press itself."""

    def test_pressing_the_key_with_no_model_says_so_instead_of_recording(self):
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        reply = harness.daemon.dispatch("start")
        self.assertFalse(reply["ok"])
        self.assertIn("small", reply["error"])
        harness.daemon.recorder.start.assert_not_called()

    def test_pressing_the_key_does_not_start_a_download(self):
        # Pressing the key is not agreeing to spend the bandwidth: the settings
        # window asks before a download and the user may have said no. A daemon
        # that starts one here has turned that answer into a lie, and the
        # surprise arrives without a question attached to it.
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        reply = harness.daemon.dispatch("start")
        self.assertFalse(reply["ok"])
        self.assertIn("small", reply["error"])
        self.assertEqual(calls["n"], 0, "the hot key fetched a model")
        self.assertFalse(harness.daemon._prepare_running)
        harness.daemon.recorder.start.assert_not_called()



    def test_pressing_the_key_records_when_the_model_is_there(self):
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls)
        reply = harness.daemon.dispatch("start")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply.get("state"), "recording")

    def test_an_engine_without_models_never_refuses_to_record(self):
        from dataclasses import replace

        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, replace(eng, model_present=None, model_download=None), calls)
        self.assertTrue(harness.daemon.dispatch("start")["ok"])


class ForeignModelTests(DaemonCase):
    """Models the daemon does not manage must still be dictatable.

    A local directory is a supported model: the settings window says so, and the
    engine passes the value straight to Faster-Whisper, which loads it from disk.
    Nothing about it can be fetched or deleted, so the daemon has to answer "not
    our business" rather than "missing" - otherwise it refuses every hot-key
    press for a user who did nothing wrong.
    """

    def _daemon(self, model_value):
        eng, calls = make_engine(present=False, progress=True)
        harness = PrepareDaemon(self, eng, calls, config={"model": model_value})
        return harness, calls

    def test_a_local_directory_is_not_reported_as_missing(self):
        harness, calls = self._daemon("/home/u/models/my-model")
        report = harness.daemon.status()["model"]
        self.assertFalse(report["supported"])
        self.assertTrue(report["present"])
        self.assertEqual(calls["n"], 0, "a local directory must not be downloaded")

    def test_the_hot_key_still_records_with_a_local_directory(self):
        harness, calls = self._daemon("/home/u/models/my-model")
        reply = harness.daemon.dispatch("start")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply.get("state"), "recording")
        harness.daemon.recorder.start.assert_called_once()

    def test_a_nested_path_is_not_reported_as_missing_either(self):
        # Three components is a path as far as the hub cache is concerned; a
        # repository id is exactly org/name.
        harness, calls = self._daemon("org/name/subfolder")
        self.assertFalse(harness.daemon.status()["model"]["supported"])
        self.assertTrue(harness.daemon.dispatch("start")["ok"])

    def test_a_catalogue_model_is_still_managed(self):
        harness, calls = self._daemon("small")
        report = harness.daemon.status()["model"]
        self.assertTrue(report["supported"])

    def test_a_repository_id_typed_by_hand_is_still_managed(self):
        harness, calls = self._daemon("Systran/faster-whisper-base")
        self.assertTrue(harness.daemon.status()["model"]["supported"])

    def test_preparing_a_local_directory_reports_nothing_to_fetch(self):
        harness, calls = self._daemon("/home/u/models/my-model")
        reply = harness.daemon.dispatch("prepare-model")
        # Not an error: there is nothing to download and nothing to warm, and the
        # caller is told so rather than being handed a failure.  The wording is
        # the user's language, so what is asserted is the answer's shape.
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["state"], "not_applicable")
        self.assertTrue(reply["error"])
        self.assertEqual(calls["n"], 0)


class CommandTests(DaemonCase):
    """``prepare-model`` and ``cancel-download`` from the window."""

    def test_prepare_model_starts_the_download(self):
        eng, calls = make_engine(present=False, progress=True)
        harness = PrepareDaemon(self, eng, calls)
        reply = harness.daemon.dispatch("prepare-model")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["state"], "downloading")
        harness.wait_for("ready")

    def test_prepare_model_on_a_model_that_is_there_loads_it(self):
        # Nothing is fetched, and the reply says what was started rather than
        # what the background thread had reached by the time it was read: the
        # warm worker here is instant, so a reply that reported the state instead
        # of the phase would say "ready" for a load that had just begun - and say
        # so or not at all depending on how the scheduler felt.
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls)
        reply = harness.daemon.dispatch("prepare-model")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["state"], "warming")
        self.assertEqual(calls["n"], 0)

    def test_prepare_model_on_an_engine_without_models_says_why(self):
        from dataclasses import replace

        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, replace(eng, model_present=None, model_download=None), calls)
        reply = harness.daemon.dispatch("prepare-model")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["state"], "not_applicable")

    def test_prepare_model_while_one_is_running_reports_the_reason_it_is_running(self):
        eng, calls = make_engine(present=False, states=["error"], delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        self.assertTrue(harness.daemon.dispatch("prepare-model")["ok"])
        reply = harness.daemon.dispatch("prepare-model")
        self.assertFalse(reply["ok"])
        harness.wait_for("error")

    def test_two_callers_in_the_starting_window_start_one_download(self):
        # The window between "the thread exists" and "the thread runs" is real:
        # the guard has to be set before start(), not inferred from is_alive()
        # afterwards.  Two callers in that window used to both fetch the same
        # file at the same time, from two threads, into one cache directory.
        eng, calls = make_engine(present=False, states=["ready"], delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        window = threading.Event()
        release = threading.Event()
        first_answer = []
        second_answer = []
        real_thread = threading.Thread
        gated = []

        class SlowStartThread(real_thread):
            """A thread that cannot begin until the test lets it."""

            def start(self):
                if not gated:
                    gated.append(self)
                    window.set()
                    if not release.wait(20.0):
                        raise AssertionError("the test never opened the window")
                super().start()

        # Only the daemon's own threading is replaced, and only its first thread
        # waits; everything else in the process keeps the real module.
        shim = types.SimpleNamespace(
            Thread=SlowStartThread,
            Event=threading.Event,
            Timer=threading.Timer,
        )
        with mock.patch.object(daemon_mod, "threading", shim):
            first = threading.Thread(
                target=lambda: first_answer.append(
                    harness.daemon.dispatch("prepare-model")
                ),
                daemon=True,
            )
            first.start()
            self.assertTrue(window.wait(10.0), "the first download never reached start()")
            # Inside the window: the thread object exists, but nothing has run.
            second_answer.append(harness.daemon.dispatch("prepare-model"))
            release.set()
            first.join(timeout=20.0)
        self.assertTrue(first_answer and first_answer[0]["ok"], first_answer)
        self.assertFalse(
            second_answer[0]["ok"],
            "a second caller started a download while one was already starting",
        )
        harness.wait_for("ready")
        self.assertEqual(calls["n"], 1, f"the file was fetched {calls['n']} times")

    def test_cancel_download_is_answered_honestly(self):
        eng, calls = make_engine(present=False, progress=True, delay=0.05)
        harness = PrepareDaemon(self, eng, calls)
        harness.daemon._start_model_prepare({})
        self.assertTrue(harness.daemon.dispatch("cancel-download")["ok"])
        harness.wait_for("cancelled")

    def test_cancel_download_when_nothing_runs_says_nothing_was_running(self):
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls)
        self.assertFalse(harness.daemon.dispatch("cancel-download")["ok"])

    def test_an_unknown_command_is_still_an_error(self):
        eng, calls = make_engine(present=True)
        harness = PrepareDaemon(self, eng, calls)
        self.assertIn("Unknown command", harness.daemon.dispatch("nonsense")["error"])


class StartupTests(unittest.TestCase):
    """What starting the daemon does to the model, without being asked.

    Warming a model that is already on disk is free and makes the first
    dictation of the session as fast as the ones after it.  Fetching one is not
    free and is nobody's decision but the user's, so startup does not.

    Preparation happens from :meth:`prepare_on_start`, which ``serve`` calls once
    it holds the ownership lock - so these tests call it the way ``serve`` does,
    and the last one pins why the order matters.
    """

    def _start(self, engine, **config):
        cfg = {"model": "small", "engine_worker": True, "notify": False}
        cfg.update(config)
        # The patches have to outlive the construction: what the daemon does next
        # - on the hot key, on a command - is still answered through them.
        for target, value in (
            ("wayvoice.daemon.engine_from_config", engine),
            ("wayvoice.daemon.load_config", cfg),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        daemon = WayVoiceDaemon()
        self.addCleanup(daemon._prepare_cancel.set)
        daemon.prepare_on_start()
        return daemon

    def _settle(self, daemon, timeout=10.0):
        deadline = time.monotonic() + timeout
        while daemon._prepare_running and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_starting_the_daemon_fetches_nothing(self):
        # Several gigabytes over the network the moment WayVoice starts is a
        # decision nobody asked for - and on a tethered laptop it is somebody
        # else's bandwidth.
        eng, calls = make_engine(present=False, progress=True)
        with mock.patch("wayvoice.engine.warm_worker", return_value=True) as warm:
            daemon = self._start(eng)
            self._settle(daemon)
        self.assertEqual(calls["n"], 0, "the daemon started a download on its own")
        self.assertFalse(warm.called)
        self.assertIsNone(daemon._prepare_thread)

    def test_starting_the_daemon_warms_the_model_that_is_on_disk(self):
        eng, calls = make_engine(present=True)
        with mock.patch("wayvoice.engine.warm_worker", return_value=True) as warm:
            daemon = self._start(eng)
            self._settle(daemon)
        self.assertEqual(calls["n"], 0)
        self.assertTrue(warm.called)
        self.assertEqual(daemon.status()["model"]["download"]["state"], "ready")

    def test_a_disabled_worker_is_not_warmed_at_startup(self):
        eng, calls = make_engine(present=True)
        with mock.patch("wayvoice.engine.warm_worker") as warm:
            daemon = self._start(eng, engine_worker=False)
            self._settle(daemon)
        self.assertFalse(warm.called)
        self.assertIsNone(daemon._prepare_thread)

    def test_a_missing_model_is_fetched_when_the_user_asks_for_it(self):
        eng, calls = make_engine(present=False, progress=True)
        with mock.patch("wayvoice.engine.warm_worker", return_value=True):
            daemon = self._start(eng)
            self._settle(daemon)
            self.assertTrue(daemon.dispatch("prepare-model")["ok"])
            self._settle(daemon)
        self.assertEqual(calls["n"], 1)

    def test_a_daemon_that_is_refused_changes_nothing(self):
        # Ownership is decided before anything is prepared, and that order is
        # the point: preparing means warming the worker, which stops the running
        # daemon's worker because the two disagree about the model, and then
        # starts one of its own. A duplicate that did that first would leave the
        # real daemon paying a cold model load and a pid file owned by a process
        # that is already gone.
        holder_warmed = threading.Event()

        def warm_first(cfg):
            holder_warmed.set()
            return True

        first, _calls = make_engine(present=True)
        with mock.patch.object(daemon_mod, "engine_from_config", return_value=first), \
             mock.patch.object(daemon_mod, "load_config", return_value={"model": "small"}), \
             mock.patch("wayvoice.engine.warm_worker", side_effect=warm_first):
            holder = WayVoiceDaemon()
            holder.recorder = mock.Mock(recording=False)
            thread = threading.Thread(target=holder.serve, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 20.0)
            self.addCleanup(holder.request_shutdown)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and not socket_path().exists():
                time.sleep(0.02)
            self.assertTrue(socket_path().exists(), "the first daemon never served")
            # Wait for the holder's own warm-up to have happened, rather than for
            # a flag that is still false because the thread has not started yet:
            # a late call of the holder's would land in the mock below and look
            # like the duplicate's doing. This test passed or failed depending on
            # how the scheduler felt before that was noticed.
            self.assertTrue(
                holder_warmed.wait(20.0), "the first daemon never warmed its worker"
            )
            self._settle(holder)

            second_engine, _second_calls = make_engine(present=True)
            with mock.patch.object(daemon_mod, "engine_from_config", return_value=second_engine), \
                 mock.patch("wayvoice.engine.warm_worker", return_value=True) as warm:
                duplicate = WayVoiceDaemon()
                duplicate.recorder = mock.Mock(recording=False)
                duplicate.serve()  # refused by the lock, returns at once
                self.assertFalse(warm.called, "a refused daemon warmed a worker")
                self.assertFalse(duplicate._prepare_running)
            holder.request_shutdown()

    def test_the_warm_up_at_startup_is_reported_as_such(self):
        eng, calls = make_engine(present=True)
        with mock.patch("wayvoice.engine.warm_worker", return_value=True):
            daemon = self._start(eng)
            self._settle(daemon)
        report = daemon.status()["model"]
        self.assertFalse(report["warming"])
        self.assertTrue(report["present"])


if __name__ == "__main__":
    unittest.main()
