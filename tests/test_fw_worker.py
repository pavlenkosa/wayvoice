import json
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from wayvoice import fw_worker
from wayvoice.fw_worker import ModelCache, WorkerConfig, handle_request, run_server


class FakeSegment:
    """Minimal stand-in for a faster-whisper segment."""

    def __init__(self, text, no_speech_prob=0.0, avg_logprob=0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob


class FakeModel:
    """Fake WhisperModel: records calls and replays prepared segments."""

    def __init__(self, segments=()):
        self.segments = list(segments)
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return iter(list(self.segments)), None


class SlowModel(FakeModel):
    """Fake model whose decoding can be paused, to exercise cancellation."""

    def __init__(self, segments=()):
        super().__init__(segments)
        self.started = threading.Event()
        self.release = threading.Event()

    def transcribe(self, audio, **kwargs):
        self.started.set()
        self.release.wait(5.0)
        return super().transcribe(audio, **kwargs)


class RecordingFactory:
    """Model factory that counts instantiations."""

    def __init__(self, segments=(), model_cls=FakeModel):
        self.calls = []
        self.segments = segments
        self.model_cls = model_cls
        self.models = []

    def __call__(self, model_id, device, compute_type):
        self.calls.append((model_id, device, compute_type))
        model = self.model_cls(self.segments)
        self.models.append(model)
        return model


class FakeClock:
    """Clock the test advances by hand, so idle timeouts are deterministic."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _audio_file(tmpdir):
    path = Path(tmpdir) / "take.wav"
    path.write_bytes(b"RIFF")
    return str(path)


def _call(socket_path, payload, timeout=5.0):
    """Send one request over a fresh connection, as the daemon does."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(socket_path))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))
    finally:
        sock.close()


class HandleRequestTests(unittest.TestCase):
    def setUp(self):
        self.factory = RecordingFactory([FakeSegment("привет")])
        self.cache = ModelCache(self.factory)
        self.config = WorkerConfig(model="tiny", device="cpu", beam_size=3, vad=False)

    def _transcribe(self, request_id="r", language="ru", cache=None):
        with tempfile.TemporaryDirectory() as tmp:
            return handle_request(
                {"cmd": "transcribe", "audio": _audio_file(tmp), "language": language, "request_id": request_id},
                cache or self.cache,
                self.config,
            )

    def test_ping_reports_alive_and_model(self):
        reply = handle_request({"cmd": "ping"}, self.cache, self.config)
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["pong"])
        self.assertEqual(reply["model"], "tiny")
        self.assertEqual(reply["device"], "cpu")
        self.assertEqual(reply["compute_type"], "int8")
        # Lazy model: a liveness probe must not load anything.
        self.assertFalse(reply["warm"])
        self.assertEqual(self.factory.calls, [])

    def test_ping_reports_loaded_model_after_transcription(self):
        self._transcribe()
        reply = handle_request({"cmd": "ping"}, self.cache, self.config)
        self.assertTrue(reply["warm"])
        self.assertEqual(reply["loaded_model"], "tiny")

    def test_unknown_command_is_rejected(self):
        reply = handle_request({"cmd": "nonsense"}, self.cache, self.config)
        self.assertFalse(reply["ok"])
        self.assertIn("nonsense", reply["error"])

    def test_malformed_payload_is_rejected(self):
        self.assertFalse(handle_request("not a dict", self.cache, self.config)["ok"])

    def test_transcribe_joins_segments_and_echoes_request_id(self):
        factory = RecordingFactory([FakeSegment(" привет "), FakeSegment("мир")])
        cache = ModelCache(factory)
        with tempfile.TemporaryDirectory() as tmp:
            audio = _audio_file(tmp)
            reply = handle_request(
                {"cmd": "transcribe", "audio": audio, "language": "ru", "request_id": "abc-123"},
                cache,
                self.config,
            )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["text"], "привет мир")
        self.assertEqual(reply["request_id"], "abc-123")
        self.assertEqual(factory.calls, [("tiny", "cpu", "int8")])
        _audio, kwargs = cache.get("tiny", "cpu", "int8").calls[0]
        self.assertEqual(_audio, audio)
        self.assertEqual(kwargs["language"], "ru")
        self.assertEqual(kwargs["beam_size"], 3)
        self.assertFalse(kwargs["vad_filter"])

    def test_transcribe_auto_language_becomes_none(self):
        self._transcribe(language="auto")
        _audio, kwargs = self.cache.get("tiny", "cpu", "int8").calls[0]
        self.assertIsNone(kwargs["language"])

    def test_no_speech_filter_threshold(self):
        # This threshold is part of the engine contract: a segment is dropped
        # only when no_speech_prob > 0.72 *and* avg_logprob < -1.0.
        cases = [
            (FakeSegment("шум", no_speech_prob=0.9, avg_logprob=-1.5), ""),
            (FakeSegment("речь", no_speech_prob=0.9, avg_logprob=-0.5), "речь"),
            (FakeSegment("речь", no_speech_prob=0.72, avg_logprob=-1.5), "речь"),
            (FakeSegment("речь", no_speech_prob=0.73, avg_logprob=-1.0), "речь"),
        ]
        for index, (segment, expected) in enumerate(cases):
            cache = ModelCache(RecordingFactory([segment]))
            reply = self._transcribe(request_id=str(index), cache=cache)
            self.assertEqual(reply["text"], expected)

    def test_no_speech_filter_drops_only_the_quiet_segment(self):
        segments = [
            FakeSegment("тишина", no_speech_prob=0.95, avg_logprob=-2.0),
            FakeSegment("говорит", no_speech_prob=0.2, avg_logprob=-0.4),
        ]
        cache = ModelCache(RecordingFactory(segments))
        reply = self._transcribe(cache=cache)
        self.assertEqual(reply["text"], "говорит")

    def test_missing_audio_is_reported(self):
        reply = handle_request(
            {"cmd": "transcribe", "audio": "", "language": "ru", "request_id": "r"},
            self.cache,
            self.config,
        )
        self.assertFalse(reply["ok"])
        self.assertIn("audio", reply["error"])

    def test_cancel_event_stops_transcription(self):
        cancel = threading.Event()
        cancel.set()
        cache = ModelCache(RecordingFactory([FakeSegment("говорит")]))
        with tempfile.TemporaryDirectory() as tmp:
            reply = handle_request(
                {"cmd": "transcribe", "audio": _audio_file(tmp), "language": "ru", "request_id": "r"},
                cache,
                self.config,
                cancel,
            )
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["cancelled"])
        self.assertEqual(reply["request_id"], "r")
        # Nothing was decoded, so no model had to be loaded.
        self.assertEqual(self.factory.calls, [])


class ModelCacheTests(unittest.TestCase):
    def test_same_key_loads_once(self):
        factory = RecordingFactory()
        cache = ModelCache(factory)
        first = cache.get("small", "cpu", "int8")
        second = cache.get("small", "cpu", "int8")
        self.assertIs(first, second)
        self.assertEqual(factory.calls, [("small", "cpu", "int8")])
        self.assertEqual(cache.requests, 2)
        self.assertEqual(cache.loads, 1)

    def test_different_key_evicts_previous_model(self):
        factory = RecordingFactory()
        cache = ModelCache(factory)
        first = cache.get("small", "cpu", "int8")
        second = cache.get("large-v3", "cpu", "int8")
        # A different model id creates a new instance...
        self.assertIsNot(first, second)
        # ...and the previous one is no longer resident.
        self.assertEqual(cache.key, ("large-v3", "cpu", "int8"))
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(cache.loads, 2)
        # Asking for the evicted model again reloads it rather than growing.
        third = cache.get("small", "cpu", "int8")
        self.assertIsNot(third, second)
        self.assertEqual(len(factory.calls), 3)
        self.assertEqual(cache.key, ("small", "cpu", "int8"))

    def test_fresh_cache_is_empty(self):
        cache = ModelCache()
        self.assertIsNone(cache.key)
        self.assertFalse(cache.loaded())


class DeviceResolutionTests(unittest.TestCase):
    def _fake_ctranslate2(self, count):
        module = types.ModuleType("ctranslate2")
        module.get_cuda_device_count = lambda: count
        return module

    def test_explicit_devices_pass_through(self):
        self.assertEqual(fw_worker.resolve_device("cpu"), "cpu")
        self.assertEqual(fw_worker.resolve_device("cuda"), "cuda")

    def test_auto_without_ctranslate2_falls_back_to_cpu(self):
        # A ``None`` entry makes ``import ctranslate2`` raise, like a broken
        # runtime would.
        with mock.patch.dict(sys.modules, {"ctranslate2": None}):
            self.assertEqual(fw_worker.resolve_device("auto"), "cpu")

    def test_auto_with_cuda_available(self):
        with mock.patch.dict(sys.modules, {"ctranslate2": self._fake_ctranslate2(1)}):
            self.assertEqual(fw_worker.resolve_device("auto"), "cuda")

    def test_auto_without_cuda_available(self):
        with mock.patch.dict(sys.modules, {"ctranslate2": self._fake_ctranslate2(0)}):
            self.assertEqual(fw_worker.resolve_device("auto"), "cpu")

    def test_auto_when_cuda_probe_raises(self):
        module = types.ModuleType("ctranslate2")

        def boom():
            raise RuntimeError("no driver")

        module.get_cuda_device_count = boom
        with mock.patch.dict(sys.modules, {"ctranslate2": module}):
            self.assertEqual(fw_worker.resolve_device("auto"), "cpu")

    def test_compute_type_per_device(self):
        self.assertEqual(fw_worker.compute_type_for("cpu"), "int8")
        self.assertEqual(fw_worker.compute_type_for("cuda"), "float16")
        with mock.patch.dict(sys.modules, {"ctranslate2": self._fake_ctranslate2(2)}):
            self.assertEqual(fw_worker.compute_type_for("auto"), "float16")


def _start_worker_thread(socket_path, config, factory, idle_timeout, clock):
    """Start :func:`run_server` in a daemon thread and wait for its socket."""
    thread = threading.Thread(
        target=run_server,
        args=(socket_path, config),
        kwargs={"model_factory": factory, "idle_timeout": idle_timeout, "clock": clock},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            _call(socket_path, {"cmd": "ping"}, timeout=1.0)
            return thread
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError("worker socket did not appear")


class RunServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "fw.sock"
        self.audio = _audio_file(self.tmp.name)
        self.clock = FakeClock()
        self.factory = RecordingFactory([FakeSegment("привет")])
        self.config = WorkerConfig(model="tiny", device="cpu", beam_size=5, vad=True)
        self.thread = _start_worker_thread(self.socket_path, self.config, self.factory, 0.3, self.clock)

    def tearDown(self):
        # Move the fake clock past the idle timeout so the server exits at once.
        self.clock.advance(5.0)
        self.thread.join(timeout=5.0)
        self.tmp.cleanup()

    def test_ping_over_socket(self):
        reply = _call(self.socket_path, {"cmd": "ping"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["model"], "tiny")
        self.assertEqual(reply["device"], "cpu")

    def test_transcribe_over_socket_and_request_id(self):
        reply = _call(
            self.socket_path,
            {"cmd": "transcribe", "audio": self.audio, "language": "ru", "request_id": "req-7"},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["text"], "привет")
        self.assertEqual(reply["request_id"], "req-7")

    def test_unknown_command_over_socket(self):
        reply = _call(self.socket_path, {"cmd": "bogus"})
        self.assertFalse(reply["ok"])

    def test_server_exits_after_idle_timeout(self):
        self.clock.advance(5.0)
        self.thread.join(timeout=5.0)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.socket_path.exists())

    def test_early_cancel_does_not_reach_a_later_request(self):
        # A cancel that arrives before its request is remembered for a short
        # grace period, and only for the matching request id.
        reply = _call(self.socket_path, {"cmd": "cancel", "request_id": "req-late"})
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["cancelled"])
        good = _call(
            self.socket_path,
            {"cmd": "transcribe", "audio": self.audio, "language": "ru", "request_id": "req-next"},
        )
        self.assertTrue(good["ok"])
        self.assertEqual(good["text"], "привет")


class RunServerCancelTests(unittest.TestCase):
    """The daemon cancels through a second connection while decoding runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp.name) / "fw.sock"
        self.audio = _audio_file(self.tmp.name)
        self.clock = FakeClock()
        self.factory = RecordingFactory([FakeSegment("говорит")], model_cls=SlowModel)
        self.config = WorkerConfig(model="tiny", device="cpu", beam_size=5, vad=True)
        self.thread = _start_worker_thread(self.socket_path, self.config, self.factory, 1.0, self.clock)

    def tearDown(self):
        for model in self.factory.models:
            model.release.set()
        self.clock.advance(5.0)
        self.thread.join(timeout=5.0)
        self.tmp.cleanup()

    def _wait_for_model(self):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.factory.models:
                return self.factory.models[0]
            time.sleep(0.02)
        raise AssertionError("model was never created")

    def test_cancel_stops_a_running_request(self):
        result = {}
        client = threading.Thread(
            target=lambda: result.update(
                reply=_call(
                    self.socket_path,
                    {"cmd": "transcribe", "audio": self.audio, "language": "ru", "request_id": "slow-1"},
                )
            ),
            daemon=True,
        )
        client.start()
        model = self._wait_for_model()
        self.assertTrue(model.started.wait(5.0))

        reply = _call(self.socket_path, {"cmd": "cancel", "request_id": "slow-1"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["cancelled"])
        self.assertEqual(reply["request_id"], "slow-1")

        model.release.set()
        client.join(timeout=5.0)
        self.assertFalse(client.is_alive())
        self.assertFalse(result["reply"]["ok"])
        self.assertTrue(result["reply"]["cancelled"])
        self.assertEqual(result["reply"]["request_id"], "slow-1")


if __name__ == "__main__":
    unittest.main()
