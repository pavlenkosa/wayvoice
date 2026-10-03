"""Getting the model onto the disk before anyone waits for it.

Recognition used to begin by fetching several gigabytes, silently, inside the
transcription: the user pressed the key, spoke, waited, and eventually either
saw text or a timeout.  These tests cover the replacement - an explicit,
visible, cancellable download - and the warm-up that follows it.

No network: the helper process is replaced by a script that speaks the same
protocol, and the cache is a temporary directory.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import engine, model_store

#: A stand-in for model_fetch.py: speaks the protocol, needs no network.
FAKE_HELPER = """
import sys, time
mode = sys.argv[sys.argv.index("--mode") + 1]
if mode == "fail":
    print("WV-PROGRESS 10 100")
    print("WV-ERROR 404 Client Error: repo not found")
    sys.exit(1)
if mode == "silent":
    time.sleep(30)
    print("WV-READY /nowhere")
if mode == "lying":
    print("WV-READY /nowhere")
    sys.exit(0)
if mode == "slow":
    for i in range(1, 6):
        print(f"WV-PROGRESS {i * 20} 100", flush=True)
        time.sleep(0.2)
    print("WV-READY /cache/snapshot", flush=True)
    sys.exit(0)
print("WV-READY /cache/snapshot", flush=True)
"""


def _helper_args(mode: str, cache: Path) -> list[str]:
    return [sys.executable, "-c", FAKE_HELPER, "--mode", mode, "--cache-dir", str(cache)]


class PresenceTests(unittest.TestCase):
    """Whether a model can be used without touching the network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        patch = mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(self.cache)})
        patch.start()
        self.addCleanup(patch.stop)

    def _snapshot(self, model: str, *, weights: bytes = b"x" * 32, config: bool = True) -> Path:
        repo = self.cache / "models--Systran--faster-whisper-small" / "snapshots" / "abc"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "model.bin").write_bytes(weights)
        if config:
            (repo / "config.json").write_text("{}", encoding="utf-8")
        return repo

    def test_a_model_with_weights_and_config_is_present(self):
        self._snapshot("small")
        self.assertTrue(engine.model_is_present("small"))

    def test_weights_without_the_config_are_not_a_model(self):
        # faster-whisper fails at load time with a message about a file the user
        # has never heard of; from here it is simply a model that is missing.
        self._snapshot("small", config=False)
        self.assertFalse(engine.model_is_present("small"))

    def test_empty_weights_are_not_a_model(self):
        self._snapshot("small", weights=b"")
        self.assertFalse(engine.model_is_present("small"))

    def test_an_empty_cache_means_missing(self):
        self.assertFalse(engine.model_is_present("small"))

    def test_a_local_path_is_never_reported_as_a_missing_model(self):
        # It is not ours to fetch, and claiming otherwise would offer a
        # download that cannot succeed.
        self.assertFalse(model_store.repo_id_for("/home/u/models/foo"))
        self.assertFalse(model_store.repo_id_for("~/models/foo"))


class DownloadTests(unittest.TestCase):
    """The download itself: progress, failure, cancellation, and no surprises."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)

    def _download(self, mode: str, **kwargs):
        with mock.patch.object(
            engine, "_model_download_args", return_value=_helper_args(mode, self.cache)
        ):
            return engine.download_model("small", **kwargs)

    def test_a_model_that_is_already_there_is_not_downloaded_again(self):
        # The daemon asks on every start; an answer that talks to the network
        # would be slow, rude, and wrong when the machine is offline.
        with mock.patch.object(engine, "model_is_present", return_value=True):
            with mock.patch.object(
                engine.subprocess, "Popen", side_effect=AssertionError("started a process")
            ):
                started = time.monotonic()
                result = engine.download_model("small")
        self.assertEqual(result["state"], "ready")
        self.assertLess(time.monotonic() - started, 0.5)

    def test_progress_is_reported_as_the_bytes_arrive(self):
        seen = []
        # Absent before the download, there afterwards - which is the real
        # sequence, and the only reason the second check exists.
        with mock.patch.object(engine, "model_is_present", side_effect=[False, True]):
            with mock.patch.object(
                engine, "_model_download_args", return_value=_helper_args("slow", self.cache)
            ):
                result = engine.download_model(
                    "small", on_progress=lambda done, total: seen.append((done, total))
                )
        self.assertEqual(result["state"], "ready")
        self.assertTrue(seen, "no progress was reported at all")
        self.assertEqual(seen[-1], (100, 100))
        # Monotonic, so a bar driven by it never goes backwards.
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))

    def test_a_failed_download_reports_the_reason(self):
        with mock.patch.object(engine, "model_is_present", return_value=False):
            result = self._download("fail")
        self.assertEqual(result["state"], "error")
        self.assertIn("404", result["error"])

    def test_a_helper_that_finished_without_a_model_is_not_a_success(self):
        # Otherwise the user is sent to the hotkey for weights that are not there.
        with mock.patch.object(engine, "model_is_present", return_value=False):
            result = self._download("lying")
        self.assertEqual(result["state"], "error")
        self.assertIn("not on disk", result["error"])

    def test_a_cancel_stops_the_download(self):
        with mock.patch.object(engine, "model_is_present", return_value=False):
            cancel = threading.Event()
            threading.Timer(0.4, cancel.set).start()
            started = time.monotonic()
            result = self._download("silent", cancel_event=cancel)
        self.assertEqual(result["state"], "cancelled")
        self.assertLess(time.monotonic() - started, 10.0)

    def test_a_helper_that_will_not_start_is_reported_not_raised(self):
        with mock.patch.object(engine, "model_is_present", return_value=False):
            with mock.patch.object(engine, "_model_download_args", return_value=["/nope"]):
                result = engine.download_model("small")
        self.assertEqual(result["state"], "error")

    def test_a_model_that_cannot_be_fetched_says_so(self):
        # No runtime, a local path, or a missing helper script: all three mean
        # "there is nothing this daemon can download", not "download failed".
        with mock.patch.object(engine, "model_is_present", return_value=False):
            with mock.patch.object(engine, "_model_download_args", return_value=None):
                result = engine.download_model("small")
        self.assertEqual(result["state"], "unsupported")

    def test_a_model_that_cannot_be_fetched_never_starts_a_process(self):
        with mock.patch.object(engine, "model_is_present", return_value=False):
            with mock.patch.object(engine, "_model_download_args", return_value=None):
                with mock.patch.object(engine.subprocess, "Popen") as popen:
                    engine.download_model("small")
        self.assertFalse(popen.called)


class PrepareTests(unittest.TestCase):
    """Download, then let the warm worker hold the model."""

    def _engine_with(self, **overrides):
        """The Faster-Whisper entry with some hooks replaced.

        ``Engine`` is frozen, so the registry entry is replaced wholesale rather
        than patched: the registered hooks are function objects captured at
        import time, and patching the module attribute would not reach them.
        """
        from dataclasses import replace

        base = engine.get_engine("faster-whisper")
        return replace(base, **overrides)

    def test_an_engine_without_models_needs_no_preparation(self):
        # whisper.cpp and an external command keep their weights elsewhere:
        # preparing them would report progress for nothing.
        reply = engine.prepare_model(engine.get_engine("whisper-cpp"), {"model": "small"})
        self.assertEqual(reply["state"], "ready")
        self.assertFalse(reply["warming"])

    def test_an_unknown_engine_is_not_prepared(self):
        self.assertEqual(engine.prepare_model(None, {})["state"], "ready")

    def test_a_finished_download_warms_the_worker(self):
        # The point of the whole exercise: the first dictation should not pay
        # for loading a model that nobody was waiting for.
        def download(cfg, on_progress=None, cancel_event=None):
            return {"state": "ready", "error": "", "done": 100, "total": 100}

        fake = self._engine_with(model_download=download)
        with mock.patch.object(engine, "warm_worker", return_value=True) as warm:
            reply = engine.prepare_model(fake, {"engine_worker": True})
        self.assertEqual(reply["state"], "ready")
        self.assertEqual(reply["done"], 100)
        self.assertTrue(reply["warming"])
        self.assertTrue(warm.called)

    def test_a_disabled_worker_is_not_warmed(self):
        def download(cfg, on_progress=None, cancel_event=None):
            return {"state": "ready", "error": ""}

        fake = self._engine_with(model_download=download)
        with mock.patch.object(engine, "warm_worker") as warm:
            reply = engine.prepare_model(fake, {"engine_worker": False})
        self.assertFalse(reply["warming"])
        self.assertFalse(warm.called)

    def test_a_failed_download_is_not_followed_by_a_warm_up(self):
        # Nothing to load, so warming would start a worker that then fails on
        # the same missing weights.
        def download(cfg, on_progress=None, cancel_event=None):
            return {"state": "error", "error": "404", "done": 0, "total": 100}

        fake = self._engine_with(model_download=download)
        with mock.patch.object(engine, "warm_worker") as warm:
            reply = engine.prepare_model(fake, {"engine_worker": True})
        self.assertEqual(reply["state"], "error")
        self.assertEqual(reply["error"], "404")
        self.assertFalse(reply["warming"])
        self.assertFalse(warm.called)

    def test_a_cancelled_download_is_not_a_failure_to_report(self):
        def download(cfg, on_progress=None, cancel_event=None):
            return {"state": "cancelled", "error": "Download cancelled"}

        fake = self._engine_with(model_download=download)
        with mock.patch.object(engine, "warm_worker") as warm:
            reply = engine.prepare_model(fake, {})
        self.assertEqual(reply["state"], "cancelled")
        self.assertFalse(warm.called)

    def test_progress_is_passed_through_to_the_caller(self):
        seen = []
        def download(cfg, on_progress=None, cancel_event=None):
            on_progress(30, 90)
            return {"state": "ready", "error": "", "done": 30, "total": 90}

        fake = self._engine_with(model_download=download)
        with mock.patch.object(engine, "warm_worker", return_value=False):
            reply = engine.prepare_model(fake, {}, lambda d, t: seen.append((d, t)))
        self.assertEqual(seen, [(30, 90)])
        self.assertEqual((reply["done"], reply["total"]), (30, 90))

    def test_the_registered_hook_fetches_the_model_the_config_names(self):
        # The hook is handed the whole config while the work takes a model id.
        # Getting that wrong did not raise: the model id became a dict, nothing
        # resolved, and every download reported "there is nothing to fetch".
        asked = []

        def capture(model_id):
            asked.append(model_id)
            return None

        with mock.patch.object(engine, "model_is_present", return_value=False):
            with mock.patch.object(engine, "_model_download_args", side_effect=capture):
                reply = engine.ENGINES["faster-whisper"].model_download(
                    {"model": "medium"}, None, None
                )
        self.assertEqual(asked, ["medium"])
        self.assertEqual(reply["state"], "unsupported")

    def test_the_registry_agrees_about_which_engines_have_downloadable_models(self):
        # A button that prepares nothing must not be shown, and an engine that
        # reports a model as missing has to be able to fetch it.
        for eng in engine.ENGINES.values():
            if eng.model_present is None:
                self.assertIsNone(eng.model_download, eng.id)
            else:
                self.assertIsNotNone(eng.model_download, eng.id)


class WarmWorkerTests(unittest.TestCase):
    """Asking the worker to load the model now instead of at first use."""

    def test_a_worker_that_answers_and_loads_the_model(self):
        with mock.patch.object(engine, "ensure_worker", return_value=True):
            with mock.patch.object(
                engine, "_worker_call", return_value={"ok": True, "warm": True}
            ) as call:
                self.assertTrue(engine.warm_worker({}))
        self.assertEqual(call.call_args[0][0]["cmd"], "warm")

    def test_a_worker_that_cannot_be_started_is_not_a_failure(self):
        # Recognition falls back to the one-shot runner, which loads the model
        # itself and works.
        with mock.patch.object(engine, "ensure_worker", return_value=False):
            self.assertFalse(engine.warm_worker({}))

    def test_a_worker_that_refuses_to_load_is_not_a_failure(self):
        with mock.patch.object(engine, "ensure_worker", return_value=True):
            with mock.patch.object(
                engine, "_worker_call", return_value={"ok": False, "error": "no such file"}
            ):
                self.assertFalse(engine.warm_worker({}))

    def test_a_worker_that_cannot_be_reached_is_not_a_failure(self):
        with mock.patch.object(engine, "ensure_worker", return_value=True):
            with mock.patch.object(engine, "_worker_call", side_effect=OSError("gone")):
                self.assertFalse(engine.warm_worker({}))


class FetchPatternsTests(unittest.TestCase):
    """The list of files has to match the engine that will read them."""

    def test_the_patterns_match_the_installed_runtime(self):
        # Same reasoning as the alias table: it is a copy, and a stale copy
        # leaves a model that cannot load. Skipped when there is no runtime.
        runtime = engine.faster_runtime()
        python = runtime / "bin/python"
        if not python.exists():
            self.skipTest("the Faster-Whisper runtime is not installed")
        probe = (
            "import inspect, json;"
            "from faster_whisper import utils;"
            "print(json.dumps(inspect.getsource(utils.download_model)))"
        )
        try:
            out = subprocess.run(
                [str(python), "-c", probe], capture_output=True, text=True,
                timeout=120, check=True,
            )
            # The probe prints the source as a JSON string, so quotes and
            # newlines survive the trip through the pipe.
            installed = engine_download_patterns(json.loads(out.stdout))
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.skipTest(f"the runtime could not be asked: {exc}")
        self.assertEqual(sorted(installed), sorted(model_store.FETCH_PATTERNS))

    def test_the_patterns_are_not_a_wildcard(self):
        # A wrong entry is not a crash: it is gigabytes fetched for nothing, or
        # a model that is missing a file at load time.
        self.assertNotIn("*", model_store.FETCH_PATTERNS)
        self.assertIn("vocabulary.*", model_store.FETCH_PATTERNS)


def engine_download_patterns(source: str) -> list[str]:
    """Pull the ``allow_patterns`` list out of a function's source."""
    import ast

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "allow_patterns"
            for target in node.targets
        ):
            continue
        try:
            return list(ast.literal_eval(node.value))
        except (ValueError, TypeError):
            continue
    return []


if __name__ == "__main__":
    unittest.main()
