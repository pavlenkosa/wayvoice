"""Recognition engine registry, public compatibility exports and transcription pipeline."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable

from . import languages
from .config import number
from . import service
from .models import forced_language
from .paths import script_path
from .postprocess import normalize

from .runtime import processes, worker_client, model_preparation, locations
from .runtime.errors import (
    TranscriptionCancelled,
    TranscriptionTimeout,
    WorkerUnavailable,
    WorkerBusyError
)
from .runtime.locations import (
    _data_home,
    _state_home,
    faster_runtime,
    faster_stamp,
    setup_status_path,
    worker_socket_path,
    worker_pid_path,
    worker_log_path,
    RUNTIME_STAMP
)
from .runtime.processes import (
    _spawn_job,
    _release_job,
    _terminate_process,
    _start_readers,
    _close_pipes,
    _finish,
    _run_cancelable,
    TRANSCRIPT_OUTPUT_LIMIT,
    PROCESS_ERROR_TAIL,
    _job_lock,
    _job_procs
)
from .runtime.worker_client import (
    _worker_job,
    _worker_guard,
    _worker_call,
    _worker_ping,
    _worker_settings,
    _worker_settings_match,
    _pid_alive,
    _pid_is_zombie,
    _reap_workers,
    _is_worker_process,
    _worker_identity,
    stop_worker,
    worker_info,
    _start_worker,
    ensure_worker,
    _worker_send_cancel,
    _forget_unresolved_worker,
    _retry_unresolved_workers,
    _recover_worker_request,
    _recover_lost_worker_request,
    _worker_request,
    warm_worker,
    WORKER_START_TIMEOUT,
    WORKER_POLL_INTERVAL,
    WORKER_PING_TIMEOUT,
    WARM_TIMEOUT,
    WORKER_CANCEL_GRACE,
    WORKER_RETRY_BACKOFF,
    _worker_jobs,
    _unresolved_worker_jobs,
    _worker_lock,
    _worker_procs
)
from .runtime.model_preparation import (
    model_is_present,
    _model_download_args,
    download_model,
    download_configured_model,
    _start_line_readers,
    _apply_download_lines,
    model_state,
    prepare_model,
    DOWNLOAD_TIMEOUT,
    _PROGRESS_PREFIX,
    _READY_PREFIX,
    _ERROR_PREFIX,
    _download_lock
)

# Engines register at the bottom of this module; nothing above the registry
# branches on an engine id.
#
#: Engine used when a config names none. Must match ``config.DEFAULTS`` and the
#: id registered below.
DEFAULT_ENGINE = "faster-whisper"


def _read_setup_status() -> dict[str, Any]:
    try:
        raw = json.loads(locations.setup_status_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def request_faster_setup() -> None:
    """Ask for the Faster-Whisper runtime to be prepared in the background.

    :mod:`wayvoice.service` decides how the request reaches the machine: the user
    unit where one exists, a spawned ``wayvoice.engine_setup`` otherwise.
    """
    service.request_engine_setup()


def _find_whisper_cpp(cfg: dict[str, Any]) -> str | None:
    explicit = str(cfg.get("whisper_cpp_binary") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None
    for name in ("whisper-cli", "whisper.cpp", "main"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _status_faster(cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether the Faster-Whisper runtime is prepared, installing or broken."""
    runtime = locations.faster_runtime()
    # Accept previous WayVoice-ready stamps and migrate lazily, so an update does
    # not force a runtime rebuild.
    ready_stamps = list(runtime.glob(".engine-*-ready")) if runtime.exists() else []
    if (locations.faster_stamp().exists() or ready_stamps) and (runtime / "bin/python").exists():
        if ready_stamps and not locations.faster_stamp().exists():
            try:
                locations.faster_stamp().touch()
            except Exception:
                pass
        return {"state": "ready", "message": "Ready"}
    status = _read_setup_status()
    if status.get("state") == "installing":
        return {"state": "installing", "message": str(status.get("message") or "Preparing…")}
    if status.get("state") == "error":
        return {
            "state": "error",
            "message": str(status.get("message") or "Faster-Whisper setup failed"),
            "log": str(status.get("log") or ""),
        }
    return {"state": "missing", "message": "Faster-Whisper is not prepared"}


def _status_whisper_cpp(cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether whisper-cli and a model file are both there."""
    binary = _find_whisper_cpp(cfg)
    model = Path(str(cfg.get("whisper_cpp_model") or "")).expanduser()
    if not binary:
        return {"state": "missing", "message": "whisper-cli was not found"}
    if not model.is_file():
        return {"state": "missing", "message": "Select a whisper.cpp GGML/GGUF model"}
    return {"state": "ready", "message": f"Ready · {Path(binary).name}"}


def _status_custom(cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether an external command is configured."""
    command = str(cfg.get("custom_command") or "").strip()
    if not command:
        return {"state": "missing", "message": "Configure a command that prints text to stdout"}
    return {"state": "ready", "message": "Ready"}


def engine_status(cfg: dict[str, Any]) -> dict[str, Any]:
    """State of the engine a config selects: id, label, state and message.

    An id no engine claims is an error, never a fallback to another recognizer.
    """
    engine_id = str(cfg.get("engine", DEFAULT_ENGINE))
    engine = get_engine(engine_id)
    if engine is None:
        return {
            "id": engine_id,
            "label": engine_label(engine_id),
            "state": "error",
            "message": "Unknown recognition engine",
        }
    return {"id": engine.id, "label": engine.label, **engine.status(cfg)}


def cancel_jobs(*events):
    """Cancel owners after atomically snapshotting worker ownership."""
    return worker_client.cancel_jobs(*events)


def stop_jobs(*events, deadline=None, active_worker=False):
    """Drain each runtime owner without losing cancellation provenance."""
    processes.stop_jobs(*events, deadline=deadline)
    worker_client.stop_jobs(*events, deadline=deadline, active_worker=active_worker)


def _language(cfg: dict[str, Any]) -> str:
    """Effective recognition language for a config.

    A single-language model overrides the setting. A stale value from an old
    ``config.json`` normalizes to a real code or to ``auto``, and ``auto`` is passed
    on: faster-whisper turns it into ``None``, whisper.cpp detects the language itself
    with ``-l auto``.
    """
    model = str(cfg.get("model", "small"))
    engine = engine_from_config(cfg)
    forced = forced_language(model) if engine and engine.uses_models else None
    return languages.normalize(forced or cfg.get("language"))


def _transcribe_via_worker(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    """Transcribe through the warm worker.

    :class:`WorkerUnavailable` when the worker itself is unreachable, so the caller
    can retry through the one-shot runner; a plain :class:`RuntimeError` when the
    worker reported a genuine engine error.
    """
    if not worker_client.ensure_worker(cfg, cancel_event=cancel_event):
        raise WorkerUnavailable("the Faster-Whisper worker is not available")
    request_id = uuid.uuid4().hex
    payload = {
        "cmd": "transcribe",
        "audio": str(audio),
        "language": _language(cfg),
        "request_id": request_id,
    }
    reply = worker_client._worker_request(
        payload,
        request_id,
        number(cfg, "transcription_timeout_sec", 90),
        cancel_event,
    )
    if reply.get("cancelled"):
        raise TranscriptionCancelled("Transcription cancelled")
    if not reply.get("ok"):
        raise RuntimeError(str(reply.get("error") or "Faster-Whisper failed"))
    return str(reply.get("text") or "")


def _postprocess(text: str, cfg: dict[str, Any]) -> str:
    text = text.strip()
    if cfg.get("auto_punctuation", True):
        text = normalize(
            text,
            spoken_punctuation=bool(cfg.get("spoken_punctuation", True)),
            ensure_terminal_punctuation=bool(cfg.get("ensure_terminal_punctuation", True)),
            # The spoken-punctuation words follow the language the speech was
            # in, which is the model's own language when it forces one.
            language=_language(cfg),
        )
    from .vocabulary import apply_replacements
    text = apply_replacements(text, cfg)
    if text and cfg.get("append_space", True):
        text += " "
    return text


def _transcribe_faster(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    runtime_python = locations.faster_runtime() / "bin/python"
    if not runtime_python.exists():
        raise RuntimeError("Faster-Whisper is not ready")
    worker_client.retry_pending(cancel_event)
    if bool(cfg.get("engine_worker", True)):
        # The warm worker only takes the model load off the critical path. When it
        # is unusable we fall back to the one-shot runner; an error it reported is
        # not retried, which would run the same failing job twice.
        try:
            return _transcribe_via_worker(audio, cfg, cancel_event)
        except (TranscriptionCancelled, TranscriptionTimeout):
            raise
        except WorkerUnavailable:
            pass
    runner = str(script_path("fw_runner.py"))
    args = [
        str(runtime_python), runner,
        "--audio", str(audio),
        "--model", str(cfg.get("model", "small")),
        "--language", _language(cfg),
        "--device", str(cfg.get("device", "auto")),
        "--beam-size", str(number(cfg, "beam_size", 5)),
    ]
    if cfg.get("vad_filter", True):
        args.append("--vad")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    cp = processes._run_cancelable(
        args,
        timeout=number(cfg, "transcription_timeout_sec", 90),
        cancel_event=cancel_event,
        env=env,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "Faster-Whisper failed")
    return (cp.stdout or "").strip()


def _thread_count() -> int:
    """How many threads to give a CPU-bound helper process.

    whisper.cpp defaults to four, and that default is the reason the engine looked
    slow: on a sixteen-thread laptop the same medium model took 21.8 s for twelve
    seconds of speech with the default and 12.5 s with ``-t 16``. Nothing about the
    model or the machine had changed, only the number the program was told to use.
    """
    try:
        count = int(os.cpu_count() or 1)
    except (TypeError, ValueError):
        count = 1
    # Bounded because a wrong os.cpu_count() would otherwise be passed straight to
    # the program, and because a machine reporting hundreds of CPUs is a container
    # rather than a laptop.
    return max(1, min(count, 64))


def _transcribe_whisper_cpp(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    binary = _find_whisper_cpp(cfg)
    model = Path(str(cfg.get("whisper_cpp_model") or "")).expanduser()
    if not binary:
        raise RuntimeError("whisper-cli was not found")
    if not model.is_file():
        raise RuntimeError("whisper.cpp model was not found")
    language = _language(cfg)
    args = [binary, "-m", str(model), "-f", str(audio), "-l", language, "-nt", "-np",
            "-t", str(_thread_count())]
    if not bool(cfg.get("whisper_cpp_gpu", True)):
        args.append("-ng")
    cp = processes._run_cancelable(
        args,
        timeout=number(cfg, "transcription_timeout_sec", 90),
        cancel_event=cancel_event,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "whisper.cpp failed")
    return (cp.stdout or "").strip()


def _transcribe_custom(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    template = str(cfg.get("custom_command") or "").strip()
    if not template:
        raise RuntimeError("External command is not configured")
    rendered = template.replace("{audio}", shlex.quote(str(audio)))
    cp = processes._run_cancelable(
        ["/bin/sh", "-lc", rendered],
        timeout=number(cfg, "transcription_timeout_sec", 90),
        cancel_event=cancel_event,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "External recognition command failed")
    return (cp.stdout or "").strip()


def transcribe(audio: Path, cfg: dict[str, Any], cancel_event: Event | None = None) -> str:
    """Recognize ``audio`` with the configured engine and postprocess the text.

    The engine returns the raw transcript; punctuation and the trailing space are
    shared by all engines and stay here.
    """
    engine_id = str(cfg.get("engine", DEFAULT_ENGINE))
    engine = get_engine(engine_id)
    if engine is None:
        raise RuntimeError(f"Unknown engine: {engine_id}")
    return _postprocess(engine.transcribe(audio, cfg, cancel_event), cfg)


# --------------------------------------------------------------------------
# Engine registry
#
# The only place that knows which engines exist. The daemon, the CLI and the
# settings window ask the registry instead of comparing ids, so an engine is one
# entry here plus its ``_transcribe_*``/``_status_*`` functions.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Engine:
    """One recognition engine: how to run it and what its settings look like.

    ``transcribe`` returns the raw text, ``status`` a ``state``/``message`` pair, and
    :func:`engine_status` adds the id and label so every engine reports alike.

    ``settings`` lists the config keys that belong to this engine alone; the settings
    window shows a row when its key is in there. Keys every engine shares (language,
    timeouts) are in none of them. ``setup`` prepares the engine's runtime and is what
    ``needs_setup`` advertises to the UI, so the two must agree; ``model_present`` and
    ``model_download`` are the same agreement for weights.
    """

    id: str
    label: str
    transcribe: Callable[[Path, dict[str, Any], Event | None], str]
    status: Callable[[dict[str, Any]], dict[str, Any]]
    uses_models: bool
    needs_setup: bool
    settings: tuple[str, ...]
    setup: Callable[[], None] | None = None
    #: Whether the engine's weights are usable right now. ``None`` for an engine
    #: whose weights live outside the hub cache - a local folder, or an external
    #: command.
    model_present: Callable[[dict[str, Any]], bool] | None = None
    #: Fetch the weights, reporting progress; ``None`` when there is nothing to
    #: fetch.  See :func:`download_model` for the reply shape.
    model_download: Callable[..., dict[str, Any]] | None = None


#: Registered engines by id.  Insertion order is the order of the UI list.
ENGINES: dict[str, Engine] = {}


def _register(engine: Engine) -> None:
    """Add one engine; the registration order is the order of the UI list."""
    ENGINES[engine.id] = engine


_register(Engine(
    id=DEFAULT_ENGINE,
    label="Faster-Whisper",
    transcribe=_transcribe_faster,
    status=_status_faster,
    uses_models=True,
    needs_setup=True,
    # ``custom_model`` is the "custom" entry of the model list, and the worker
    # settings exist for this engine only. Compute type is selected automatically
    # by fw_worker for both persistent and one-shot operation.
    settings=(
        "model",
        "custom_model",
        "device",
        "beam_size",
        "vad_filter",
        "engine_worker",
        "engine_worker_idle_sec",
    ),
    setup=request_faster_setup,
    model_present=lambda cfg: model_preparation.model_is_present(str(cfg.get("model", ""))),
    model_download=model_preparation.download_configured_model,
))

_register(Engine(
    id="whisper-cpp",
    label="whisper.cpp",
    transcribe=_transcribe_whisper_cpp,
    status=_status_whisper_cpp,
    uses_models=False,
    needs_setup=False,
    settings=("whisper_cpp_binary", "whisper_cpp_model", "whisper_cpp_gpu"),
))

_register(Engine(
    id="custom",
    label="External command",
    transcribe=_transcribe_custom,
    status=_status_custom,
    uses_models=False,
    needs_setup=False,
    settings=("custom_command",),
))


def engine_ids() -> list[str]:
    """Engine ids in registration order -- the order of the settings list."""
    return list(ENGINES)


def get_engine(engine_id: str | None) -> Engine | None:
    """Return a registered engine, or ``None`` when nothing claims the id."""
    return ENGINES.get(str(engine_id or ""))


def engine_label(engine_id: str | None) -> str:
    """Name of an engine as the user knows it; its own id when unknown."""
    engine = get_engine(engine_id)
    return engine.label if engine else str(engine_id or "")


def engine_from_config(cfg: dict[str, Any]) -> Engine | None:
    """Engine a config selects, or ``None`` when its id is unknown.

    No default is substituted: a config naming a nonexistent engine is broken, and
    only the caller knows how much to say about it.
    """
    return get_engine(cfg.get("engine", DEFAULT_ENGINE))


def request_engine_setup(engine: Engine | None) -> bool:
    """Ask for the runtime of ``engine`` to be prepared in the background.

    ``False`` when that engine needs no preparation, so a caller can tell the user
    instead of doing nothing quietly.
    """
    if engine is None or not engine.needs_setup or engine.setup is None:
        return False
    engine.setup()
    return True
