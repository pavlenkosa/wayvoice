"""Warm worker process for the Faster-Whisper engine.

The one-shot runner loads the model on every dictation, which costs seconds and
several gigabytes before the first word appears. This module keeps one model instance
in memory instead and serves requests over a unix socket, speaking the same
line-delimited JSON as the rest of WayVoice.

Three things to know when reading it:

* neither ``faster_whisper`` nor ``ctranslate2`` is imported at module level - the
worker runs on the engine runtime interpreter while the tests run on the system one
- so the heavy imports live in :func:`default_model_factory` and
:func:`resolve_device`;
* the model is created lazily on the first transcription, so merely starting the
worker costs nothing;
* the cache holds at most one model, so switching settings cannot accumulate copies.

The daemon side lives in :mod:`wayvoice.engine`; this module never imports it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__

SOCKET_NAME = "wayvoice-fw.sock"
#: Private directory inside the runtime dir that holds the socket.
SOCKET_DIR = "wayvoice"

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

    A directory of our own making, mode 0700, inside ``XDG_RUNTIME_DIR`` (or ``/tmp`` when
    there is none). The runtime directory itself is not safe enough - not always ours
    alone - and ``/tmp`` is world-writable with predictable names, so whoever wins the
    race to create the socket answers the daemon's requests: another local user could hand
    back the text that gets typed into the victim's window.

    The directory is created rather than trusted, so an unusual ``XDG_RUNTIME_DIR`` costs
    nothing. Refusing to listen is the last resort, since it silently costs every
    dictation the warm worker.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    private = base / SOCKET_DIR
    try:
        private.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(private, 0o700)
    except OSError:
        # Unwritable runtime dir: fall back to a per-user directory in /tmp,
        # which is private by construction rather than by convention.
        fallback = Path(tempfile.gettempdir()) / f"wayvoice-{os.getuid()}"
        try:
            fallback.mkdir(mode=0o700, exist_ok=True)
            os.chmod(fallback, 0o700)
        except OSError:
            return private / SOCKET_NAME
        return fallback / SOCKET_NAME
    return private / SOCKET_NAME


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

    Imported here rather than at module level, which would make this module unusable
    without the engine runtime installed.
    """
    from .model_store import inference_dir

    path = inference_dir(model_id)
    from faster_whisper import WhisperModel

    # Pin the small tokenizer in a private view. Upstream ignores local_files_only
    # for its tokenizer fallback; deleting the cache during load must not enable it.
    # Weight files remain symlinks, avoiding a second multi-GB copy in RAM or disk.
    view = tempfile.TemporaryDirectory(prefix="wayvoice-model-")
    try:
        directory = Path(view.name)
        shutil.copyfile(path / "tokenizer.json", directory / "tokenizer.json")
        if (directory / "tokenizer.json").stat().st_size == 0:
            raise RuntimeError("Local tokenizer.json became empty; prepare the model again.")
        for source in path.iterdir():
            if source.name != "tokenizer.json":
                (directory / source.name).symlink_to(source.absolute())
        model = WhisperModel(
            str(directory), device=device, compute_type=compute_type, local_files_only=True,
        )
        # Keep the view valid if the backend unloads/reloads weights later. Its
        # lifetime follows the cached model, including eviction and load failure.
        model._wayvoice_model_files = view
        weakref.finalize(model, view.cleanup)
        return model
    except Exception:
        view.cleanup()
        raise


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

    A different request evicts the previous model before the new one is created, so at
    most one copy of the weights is ever in RAM.
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
        # This code answers requests, and it is the code that was running when the worker
        # started. A worker outlives the daemon - that is what keeping it warm means -
        # so after an update the new daemon would otherwise hand its requests to a
        # process running the previous version, and nothing would say so.
        "version": __version__,
        "model": config.model,
        "device": device,
        "compute_type": compute_type_for(device),
        "warm": cache.loaded(),
        "loaded_model": key[0] if key is not None else None,
        "config": config.as_dict(),
    }


def warm_reply(cache: ModelCache, config: WorkerConfig) -> dict[str, Any]:
    """Load the model into memory and report how long that took.

    This is the slow part of a dictation - seconds for ``small``, minutes for
    ``large-v3`` - and it is pure waiting. The daemon does it once after starting, so the
    first dictation costs the same as the ones after it.

    A failure is reported rather than raised: the model stays unloaded and the worker
    keeps serving, which is where a lazily loaded model would have been anyway.
    """
    if cache.loaded():
        return {"ok": True, "warm": True, "model": config.model, "seconds": 0.0}
    started = time.monotonic()
    try:
        device = resolve_device(config.device)
        cache.get(config.model, device, compute_type_for(device))
    except Exception as exc:
        return {
            "ok": False,
            "warm": False,
            "model": config.model,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "warm": True,
        "model": config.model,
        "seconds": round(time.monotonic() - started, 2),
    }


def handle_request(
    payload: dict[str, Any],
    cache: ModelCache,
    config: WorkerConfig,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Serve one request and return the reply object.

    Commands are ``ping``, ``warm`` and ``transcribe``; anything else is reported as an
    error rather than raised, so a misbehaving client cannot take the worker down. A
    cancelled transcription returns ``{"ok": False, "cancelled": True, ...}``.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "Malformed request"}

    command = str(payload.get("cmd") or "").strip()
    if command == "ping":
        return ping_reply(cache, config)
    if command == "warm":
        return warm_reply(cache, config)
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


class _Hold:
    """Context manager that keeps the idle timer off a worker for a while.

    Re-entrant through a counter, so nested work does not release the hold early.
    """

    def __init__(self, state: "_WorkerState") -> None:
        self._state = state

    def __enter__(self) -> "_Hold":
        with self._state._lock:
            self._state._holding += 1
        return self

    def __exit__(self, *_exc) -> bool:
        with self._state._lock:
            self._state._holding = max(0, self._state._holding - 1)
        return False


class _WorkerState:
    """Shared server state: activity clock, serialisation and cancellations.

    Cancels are matched by ``request_id``. A cancel for a request that has not registered
    itself yet is remembered for a short grace period and applied when that request
    starts; it cannot reach a later, unrelated request, because every request carries a
    fresh id.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, grace: float = _CANCEL_GRACE) -> None:
        self._clock = clock
        self._grace = grace
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}
        self._early: dict[str, float] = {}
        #: Work that is not a cancellable request - a model load.
        self._holding = 0
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
            return bool(self._active) or self._holding > 0

    def hold(self) -> "_Hold":
        """Mark the worker as working without a request to cancel.

        A model load holds the lock a transcription holds and can take minutes, so counting
        only requests would let the idle timer decide the worker is unused and exit in the
        middle of a load.
        """
        return _Hold(self)

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

    A leftover socket from a crashed worker is replaced, and a socket a live worker owns
    is left alone, so two workers can never steal it from each other.

    The parent directory has to be private to this user: a socket in a world-writable
    directory is answerable by anyone on the machine, and what this worker returns is text
    that gets typed into the user's window. Group-writable is tolerated, since the socket
    itself is 0600 and refusing over it would cost every dictation the warm worker.
    """
    try:
        stat_result = path.parent.stat()
    except OSError:
        return None
    if not stat.S_ISDIR(stat_result.st_mode) or stat_result.st_uid != os.getuid():
        return None
    if stat_result.st_mode & stat.S_IWOTH:
        return None
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
    if command == "ping":
        # Never blocked and never waits for the lock: this is how the daemon checks
        # whether a warm-up that takes minutes is finished, and a ping that waited
        # for the model would answer exactly when it is not needed.
        return handle_request(payload, cache, config)
    if command == "warm":
        # Under the same lock as a transcription: loading weights into RAM while
        # something is being decoded would double the memory for nothing. Counted as
        # work, so the idle timer does not call this worker unused mid-load.
        with state.serial, state.hold():
            return handle_request(payload, cache, config)
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

    The model is not loaded here; the first transcription creates it, so an idle worker
    that never transcribed stays at zero megabytes. ``clock`` is injectable so tests can
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
