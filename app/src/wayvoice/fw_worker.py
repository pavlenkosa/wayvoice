"""Warm worker process for the Faster-Whisper engine.

The one-shot runner loads the Whisper model on every dictation, which costs
seconds and several gigabytes of RAM before the first word appears.  This
module implements the alternative: a long-lived worker that keeps a single
model instance in memory and serves requests over a unix socket, using the
same line-delimited JSON protocol as the rest of WayVoice.

Design notes:

* neither ``faster_whisper`` nor ``ctranslate2`` is imported at module level.
  The worker runs on the engine runtime interpreter created by
  ``engine_setup``, while the unit tests run on the system interpreter, so the
  heavy imports are deferred into :func:`default_model_factory` and
  :func:`resolve_device`;
* the model is created lazily, on the first transcription request.  Merely
  starting the worker must not cost gigabytes of RAM;
* the cache holds at most one model, so switching settings can never accumulate
  several copies of a multi-gigabyte model.

The daemon side lives in :mod:`wayvoice.engine`; this module never imports it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

SOCKET_NAME = "wayvoice-fw.sock"

# Conservative no-speech guard. It intentionally requires both a high
# no-speech probability and a poor log probability so quiet real speech is
# not discarded just because it is difficult to decode.
NO_SPEECH_PROB_THRESHOLD = 0.72
AVG_LOGPROB_THRESHOLD = -1.0

DEFAULT_IDLE_TIMEOUT = 900.0

# How long the accept loop waits before re-checking the idle timer.  Small
# enough that the worker exits promptly, large enough to be invisible.
_POLL_INTERVAL = 0.2
# Generous read timeout for the request line of a single connection.
_CONN_TIMEOUT = 30.0
# Upper bound for one request (base64-free, but be forgiving).
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
# A cancel that arrives before its request is honoured for this long only, so
# that a stale cancel can never cancel an unrelated later request.
_CANCEL_GRACE = 5.0


def default_socket_path() -> Path:
    """Return the default worker socket path.

    ``XDG_RUNTIME_DIR`` is preferred because it is per-user and removed on
    logout; ``/tmp`` is the fallback for sessions without it.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    base = Path(runtime) if runtime else Path("/tmp")
    return base / SOCKET_NAME


@dataclass(frozen=True)
class WorkerConfig:
    """Everything that influences which model is loaded and how it decodes."""

    model: str = "small"
    device: str = "auto"
    beam_size: int = 5
    vad: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "device": self.device,
            "beam_size": self.beam_size,
            "vad": bool(self.vad),
        }


def default_model_factory(model_id: str, device: str, compute_type: str) -> Any:
    """Create a real ``WhisperModel``.

    Imported here on purpose: importing at module level would make this module
    unusable without the engine runtime installed.
    """
    from faster_whisper import WhisperModel

    return WhisperModel(model_id, device=device, compute_type=compute_type)


def resolve_device(device: str) -> str:
    """Return the concrete device to load a model on.

    ``auto`` becomes ``cuda`` when ctranslate2 sees at least one CUDA device
    and ``cpu`` otherwise.  Any failure degrades to ``cpu``: a missing CUDA
    runtime must not break dictation.
    """
    requested = str(device or "auto").strip().lower()
    if requested in {"cpu", "cuda"}:
        return requested
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def compute_type_for(device: str) -> str:
    """Return the quantization to use for ``device``."""
    return "float16" if resolve_device(device) == "cuda" else "int8"


class ModelCache:
    """Keeps at most one loaded model, keyed by model/device/compute type.

    Loading a model is expensive, so the instance is reused for identical
    requests.  A different request evicts the previous model *before* the new
    one is created, which keeps at most one copy of the weights in RAM.
    """

    def __init__(self, factory: Callable[[str, str, str], Any] | None = None) -> None:
        self._factory = factory or default_model_factory
        self._lock = threading.Lock()
        self._model: Any = None
        self._key: tuple[str, str, str] | None = None
        #: Number of :meth:`get` calls; useful for tests and diagnostics.
        self.requests = 0
        #: Number of models actually created.
        self.loads = 0

    @property
    def key(self) -> tuple[str, str, str] | None:
        """Return the key of the resident model, or ``None`` when empty."""
        return self._key

    def loaded(self) -> bool:
        return self._model is not None

    def get(self, model_id: str, device: str, compute_type: str) -> Any:
        key = (str(model_id), str(device), str(compute_type))
        with self._lock:
            self.requests += 1
            if self._model is not None and self._key == key:
                return self._model
            # Drop the reference first so the evicted weights are released
            # before the next model allocates its own copy.
            self._model = None
            self._key = None
            model = self._factory(*key)
            self._model = model
            self._key = key
            self.loads += 1
            return model


def _segment_text(segments: Iterable[Any], cancel: threading.Event | None = None) -> str | None:
    """Join decoded segments, returning ``None`` when the run was cancelled."""
    accepted: list[str] = []
    for segment in segments:
        if cancel is not None and cancel.is_set():
            return None
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        if no_speech > NO_SPEECH_PROB_THRESHOLD and avg_logprob < AVG_LOGPROB_THRESHOLD:
            continue
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            accepted.append(text)
    return " ".join(accepted).strip()


def ping_reply(cache: ModelCache, config: WorkerConfig) -> dict[str, Any]:
    """Answer a liveness probe with the loaded and the wanted model."""
    device = resolve_device(config.device)
    key = cache.key
    return {
        "ok": True,
        "pong": True,
        "pid": os.getpid(),
        "model": config.model,
        "device": device,
        "compute_type": compute_type_for(device),
        "warm": cache.loaded(),
        "loaded_model": key[0] if key is not None else None,
        "config": config.as_dict(),
    }


def handle_request(
    payload: dict[str, Any],
    cache: ModelCache,
    config: WorkerConfig,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Serve one request and return the reply object.

    Supported commands are ``ping`` and ``transcribe``; anything else is
    reported as an error instead of raising, so a misbehaving client cannot
    take the worker down.  A cancelled transcription returns
    ``{"ok": False, "cancelled": True, ...}``.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Malformed request"}

    command = str(payload.get("cmd") or "").strip()
    if command == "ping":
        return ping_reply(cache, config)
    if command != "transcribe":
        return {"ok": False, "error": f"Unknown command: {command or '<empty>'}"}

    request_id = str(payload.get("request_id") or "")
    if cancel is not None and cancel.is_set():
        return {
            "ok": False,
            "cancelled": True,
            "request_id": request_id,
            "error": "Transcription cancelled",
        }

    audio = str(payload.get("audio") or "").strip()
    if not audio:
        return {"ok": False, "request_id": request_id, "error": "audio path is missing"}
    if not os.path.exists(audio):
        return {"ok": False, "request_id": request_id, "error": "audio file is not available"}

    language = str(payload.get("language") or "auto")
    try:
        device = resolve_device(config.device)
        model = cache.get(config.model, device, compute_type_for(device))
        kwargs: dict[str, Any] = {
            "language": None if language in {"", "auto"} else language,
            "beam_size": max(1, int(config.beam_size)),
            "vad_filter": bool(config.vad),
            "condition_on_previous_text": False,
        }
        if config.vad:
            kwargs["vad_parameters"] = {
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 180,
            }
        segments, _ = model.transcribe(audio, **kwargs)
        text = _segment_text(segments, cancel)
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "error": f"{type(exc).__name__}: {exc}"}

    if text is None:
        return {
            "ok": False,
            "cancelled": True,
            "request_id": request_id,
            "error": "Transcription cancelled",
        }
    return {"ok": True, "text": text, "request_id": request_id}


class _WorkerState:
    """Shared server state: activity clock, serialisation and cancellations.

    Cancels are matched by ``request_id``.  A cancel for a request that has not
    registered itself yet is remembered for a short grace period and applied
    when that request starts; it can never reach a later, unrelated request
    because every request carries a fresh id.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, grace: float = _CANCEL_GRACE) -> None:
        self._clock = clock
        self._grace = grace
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._early: dict[str, float] = {}
        #: Only one transcription at a time; the daemon drives a single one.
        self.serial = threading.Lock()
        self.last_activity = clock()

    def touch(self) -> None:
        with self._lock:
            self.last_activity = self._clock()

    def idle_seconds(self) -> float:
        with self._lock:
            return self._clock() - self.last_activity

    def busy(self) -> bool:
        with self._lock:
            return bool(self._active)

    def _expire(self) -> None:
        deadline = self._clock() - self._grace
        for key in [k for k, seen in self._early.items() if seen < deadline]:
            self._early.pop(key, None)

    def begin(self, request_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._expire()
            if self._early.pop(request_id, None) is not None:
                event.set()
            self._active[request_id] = event
        return event

    def finish(self, request_id: str) -> None:
        with self._lock:
            self._active.pop(request_id, None)
            self._expire()

    def cancel(self, request_id: str) -> bool:
        """Cancel a request; return ``True`` when it was already running."""
        with self._lock:
            event = self._active.get(request_id)
            if event is not None:
                event.set()
                return True
            self._expire()
            self._early[request_id] = self._clock()
            return False


def _probe(path: Path, timeout: float = 0.5) -> bool:
    """Return ``True`` when something already listens on ``path``."""
    if not path.exists():
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _bind(path: Path) -> socket.socket | None:
    """Listen on ``path``; return ``None`` when the socket cannot be served.

    A leftover socket file from a crashed worker is replaced.  When a live
    worker already owns the socket nothing is touched and ``None`` is
    returned, so two workers can never steal the socket from each other.
    """
    if _probe(path):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
    except OSError:
        return None
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(16)
    except OSError:
        listener.close()
        return None
    return listener


def _read_request(conn: socket.socket) -> bytes | None:
    """Read one newline-terminated request, or ``None`` on an empty stream."""
    data = bytearray()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return bytes(data) if data else None
        data.extend(chunk)
        if b"\n" in chunk:
            line, _, _ = bytes(data).partition(b"\n")
            return line
        if len(data) > _MAX_REQUEST_BYTES:
            raise ValueError("request is too large")


def _send(conn: socket.socket, reply: dict[str, Any]) -> None:
    conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))


def _dispatch(payload: Any, cache: ModelCache, config: WorkerConfig, state: _WorkerState) -> dict[str, Any]:
    command = str(payload.get("cmd") or "").strip() if isinstance(payload, dict) else ""
    if command != "transcribe":
        return handle_request(payload, cache, config)
    request_id = str(payload.get("request_id") or "")
    with state.serial:
        event = state.begin(request_id)
        try:
            return handle_request(payload, cache, config, event)
        finally:
            state.finish(request_id)


def _serve_connection(conn: socket.socket, cache: ModelCache, config: WorkerConfig, state: _WorkerState) -> None:
    try:
        conn.settimeout(_CONN_TIMEOUT)
        raw = _read_request(conn)
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            _send(conn, {"ok": False, "error": "Malformed request"})
            return
        if isinstance(payload, dict) and str(payload.get("cmd") or "").strip() == "cancel":
            request_id = str(payload.get("request_id") or "")
            running = state.cancel(request_id)
            _send(conn, {"ok": True, "cancelled": running, "request_id": request_id})
            return
        reply = _dispatch(payload, cache, config, state)
        conn.settimeout(_CONN_TIMEOUT)
        _send(conn, reply)
    except (OSError, ValueError):
        # A client that hangs up or speaks nonsense must not kill the worker.
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run_server(
    socket_path: Path,
    config: WorkerConfig,
    model_factory: Callable[[str, str, str], Any] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Serve requests on ``socket_path`` until idle for ``idle_timeout``.

    The model is not loaded here: it is created by the first transcription
    request, so starting the worker is cheap and an idle worker that never
    transcribed stays at zero megabytes.  ``clock`` is injectable so tests can
    drive the idle timer deterministically.
    """
    path = Path(socket_path)
    cache = ModelCache(model_factory)
    state = _WorkerState(clock=clock)
    listener = _bind(path)
    if listener is None:
        return
    try:
        while True:
            if state.busy():
                # Never expire a worker that is still decoding.
                state.touch()
            elif 0 < idle_timeout <= state.idle_seconds():
                break
            listener.settimeout(_POLL_INTERVAL)
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            state.touch()
            threading.Thread(
                target=_serve_connection,
                args=(conn, cache, config, state),
                daemon=True,
            ).start()
    finally:
        try:
            listener.close()
        finally:
            try:
                path.unlink()
            except OSError:
                pass
