"""Local model availability, explicit downloads and preparation coordination."""

from __future__ import annotations

from . import locations, processes, worker_client

import os
import subprocess
import threading
import time
from threading import Event, Lock
from typing import Any, Callable, TYPE_CHECKING

from ..i18n import tr
from ..paths import script_path
from .errors import TranscriptionCancelled

if TYPE_CHECKING:
    from ..engine import Engine

_download_lock = Lock()
DOWNLOAD_TIMEOUT = 6 * 3600.0
_PROGRESS_PREFIX = "WV-PROGRESS"
_READY_PREFIX = "WV-READY"
_ERROR_PREFIX = "WV-ERROR"


def model_is_present(model_id: str) -> bool:
    """Whether ``model_id`` can be used without touching the network.

    This shares the runtime completeness check, including the tokenizer that
    upstream would otherwise silently download. Disk accounting stays separate.
    """
    from .. import model_store

    try:
        model_store.inference_dir(model_id)
    except RuntimeError:
        return False
    return True


def _model_download_args(model_id: str) -> list[str] | None:
    """Command that fetches ``model_id``, or ``None`` when there is nothing to fetch:
    a local directory, or weights an engine keeps somewhere else.
    """
    from .. import model_store

    repo = model_store.repo_id_for(model_id)
    runtime_python = locations.faster_runtime() / "bin/python"
    # Return None for local directories or when there's no runtime to fetch with
    if repo is None:
        return None
    if not runtime_python.exists():
        # This is a hub model but no runtime is prepared - we can't download it,
        # but it's not fundamentally unsupported. Return None to indicate this.
        # The caller (download_model) will detect this condition and return an error.
        return None
    fetch = script_path("model_fetch.py")
    if not fetch.exists():
        return None
    return [
        str(runtime_python), str(fetch),
        "--repo", repo,
        "--cache-dir", str(model_store.hub_root()),
    ]


def download_model(
    model_id: str,
    on_progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Fetch a model into the hub cache, reporting progress as it arrives.

    Returns ``{"state": "ready" | "error" | "cancelled" | "unsupported", "error":
    str, "done": int, "total": int}`` and never raises: this runs on the daemon's
    preparation path. A model that is already present returns without a request,
    because the daemon asks on every start.
    """
    result: dict[str, Any] = {"state": "ready", "error": "", "done": 0, "total": 0}
    if model_is_present(model_id):
        return result
    args = _model_download_args(model_id)
    if args is None:
        from .. import model_store
        if model_store.repo_id_for(model_id) is not None:
            # A hub model the daemon could fetch if the tooling were there.
            # "unsupported" would tell the user this model can never be
            # downloaded, and the window would hide the row and the button for
            # it; naming what is missing keeps both visible.
            if not (locations.faster_runtime() / "bin/python").exists():
                # The runtime that runs the fetcher is a setup step away, not
                # a property of the model.
                return {
                    "state": "error",
                    "error": tr("engine.download_needs_runtime", language),
                    "done": 0,
                    "total": 0,
                }
            # The runtime is there, so the fetcher itself is gone: a damaged
            # install. The fetcher is a package file, so only reinstalling the
            # package (or the Flatpak image) restores it - the engine setup
            # builds the runtime, not the package files, and would answer
            # "ready" without touching anything.
            return {
                "state": "error",
                "error": tr("engine.download_missing_helper", language),
                "done": 0,
                "total": 0,
            }
        return {
            "state": "unsupported",
            "error": tr("engine.download_unsupported", language, model=model_id),
            "done": 0,
            "total": 0,
        }
    env = os.environ.copy()
    # The helper speaks its own progress protocol on stdout; the hub's bars
    # would interleave with it on stderr.
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = processes._spawn_job(
            args, cancel_event=cancel_event,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except TranscriptionCancelled:
        return {"state": "cancelled", "error": "Download cancelled", "done": 0, "total": 0}
    except (OSError, ValueError) as exc:
        return {"state": "error", "error": str(exc), "done": 0, "total": 0}

    try:
        lines: list[str] = []
        errors: list[str] = []
        readers = _start_line_readers(proc, lines, errors)
        started = time.monotonic()
        state = "ready"
        detail = ""
        while True:
            if proc.poll() is not None:
                break
            if cancel_event is not None and cancel_event.is_set():
                processes._terminate_process(proc)
                state = "cancelled"
                detail = tr("engine.download_cancelled", language)
                break
            if time.monotonic() - started >= DOWNLOAD_TIMEOUT:
                processes._terminate_process(proc)
                state = "error"
                detail = tr("engine.download_timeout", language, hours=int(DOWNLOAD_TIMEOUT // 3600))
                break
            _apply_download_lines(lines, result, on_progress)
            time.sleep(0.2)
        processes._terminate_process(proc)
        for reader in readers:
            reader.join(timeout=5.0)
        _apply_download_lines(lines, result, on_progress)
        processes._close_pipes(proc)

        stderr = "".join(errors).strip()
        if state != "ready":
            result["state"] = state
            result["error"] = detail
            return result
        if proc.returncode != 0:
            result["state"] = "error"
            # The helper's own WV-ERROR line is the real reason; stderr is the
            # library's own last word, and the exit code is the last resort.
            result["error"] = (
                str(result.get("error") or "")
                or detail
                or (stderr.splitlines()[-1] if stderr else "")
                or f"Download failed with code {proc.returncode}"
            )
            return result
        if not model_is_present(model_id):
            # The helper said it was done and the weights are not there; reporting
            # success would send the user to the hotkey for a model that is not on
            # disk.
            result["state"] = "error"
            result["error"] = tr("engine.download_missing_after", language)
        return result
    finally:
        processes._release_job(proc)


def download_configured_model(
    cfg: dict[str, Any],
    on_progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Fetch the model the *config* names; the registry hook.

    :func:`download_model` takes a model id, a registered hook is handed the whole
    config.
    """
    return download_model(
        str(cfg.get("model", "")),
        on_progress,
        cancel_event,
        language=cfg.get("ui_language"),
    )


def _start_line_readers(
    proc: subprocess.Popen[str], out: list[str], err: list[str]
) -> list[threading.Thread]:
    """Read the helper's output line by line, off the main thread."""

    def read(stream, sink):
        try:
            if sink is err:
                while chunk := stream.read(8192):
                    sink[:] = [("".join(sink) + chunk)[-processes.PROCESS_ERROR_TAIL:]]
            else:
                for line in iter(stream.readline, ""):
                    sink.append(line)
        except Exception:
            pass

    threads = []
    for stream, sink in ((proc.stdout, out), (proc.stderr, err)):
        if stream is None:
            continue
        thread = threading.Thread(target=read, args=(stream, sink), daemon=True)
        thread.start()
        threads.append(thread)
    return threads


def _apply_download_lines(
    lines: list[str],
    result: dict[str, Any],
    on_progress: Callable[[int, int], None] | None,
) -> None:
    """Fold whatever the helper has printed so far into ``result``.

    The buffer is drained in one go while the lock is held: two reader threads append
    to it while this runs.
    """
    while True:
        with _download_lock:
            if not lines:
                return
            line = lines.pop(0)
        line = line.strip()
        if line.startswith(_PROGRESS_PREFIX):
            parts = line.split()
            if len(parts) == 3:
                try:
                    done, total = int(parts[1]), int(parts[2])
                except ValueError:
                    continue
                result["done"] = done
                result["total"] = total
                if on_progress is not None:
                    try:
                        on_progress(done, total)
                    except Exception:
                        # A progress callback that raises must not end a download that is
                        # going fine.
                        pass
        elif line.startswith(_ERROR_PREFIX):
            result["error"] = line[len(_ERROR_PREFIX):].strip()
        elif line.startswith(_READY_PREFIX):
            result["done"] = max(int(result.get("done") or 0), 1)


def model_state(engine: Engine | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """What is known about the weights the config names.

    ``{"supported": bool, "present": bool, "model": str}``. ``supported`` asks
    whether *this* value is a model WayVoice manages: does the engine have hub models
    at all, and is the value one of them.

    A local directory is a perfectly good model - the window asks for exactly that,
    and the engine hands the path straight to Faster-Whisper, which loads it from disk
    - but nothing about it can be fetched, counted or deleted. Answering "missing"
    would make the daemon refuse every hot-key press, so a value that is not a hub
    repository is reported as "not ours": neither missing nor downloadable.
    """
    model_id = str(cfg.get("model", ""))
    foreign = {"supported": False, "present": True, "model": model_id}
    if engine is None or engine.model_present is None:
        return foreign
    from .. import model_store

    if model_store.repo_id_for(model_id) is None:
        # Not a hub repository: a local path, a nested path, or a free-form
        # value.  ``repo_id_for`` is what the cache layout is built from, so "not
        # downloadable" and "not in our cache" cannot disagree.
        return foreign
    try:
        present = bool(engine.model_present(cfg))
    except Exception:
        # A cache that cannot be walked is no reason to call the model missing:
        # that would start a download of something already there.
        present = True
    return {"supported": True, "present": present, "model": model_id}


def prepare_model(
    engine: Engine | None,
    cfg: dict[str, Any],
    on_progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
    on_warming: Callable[[], None] | None = None,
    download: bool = True,
) -> dict[str, Any]:
    """Fetch the engine's weights and load them into the warm worker.

    Runs in the background, because the daemon has to keep answering the hot key while
    a 3 GB model comes down. The reply carries what happened, the same shape
    :func:`download_model` returns; the warm-up is reported separately in ``warming``,
    since it is the second half of the job and can fail on its own.

    ``on_warming`` is called once the weights are down and the model is going into
    memory, which on a slow disk can take as long as the download itself - a window
    told only that "the download" runs would sit at 100% with nothing happening.

    ``download=False`` warms without fetching, which is the daemon's startup path:
    reading a model the user already has is free, while fetching three gigabytes the
    moment WayVoice starts is nobody's decision. Engines with nothing to fetch report
    ``ready`` without doing anything, which is what lets the daemon call this
    unconditionally.
    """
    reply: dict[str, Any] = {
        "state": "ready", "error": "", "done": 0, "total": 0, "warming": False,
    }
    if download and engine is not None and engine.model_download is not None:
        result = engine.model_download(cfg, on_progress, cancel_event)
        reply["state"] = str(result.get("state") or "ready")
        reply["error"] = str(result.get("error") or "")
        reply["done"] = int(result.get("done") or 0)
        reply["total"] = int(result.get("total") or 0)
        if reply["state"] != "ready":
            return reply
    if not cfg.get("engine_worker", True):
        return reply
    if engine is None or "engine_worker" not in engine.settings:
        # No worker for this engine: the warm one speaks the Faster-Whisper
        # protocol and would be asked to hold a model it cannot.
        return reply
    if on_warming is not None:
        try:
            on_warming()
        except Exception:
            # Telling the caller about a phase must never break that phase.
            pass
    if cancel_event is not None and cancel_event.is_set():
        raise TranscriptionCancelled("Transcription cancelled")
    reply["warming"] = (worker_client.warm_worker(cfg) if cancel_event is None
                        else worker_client.warm_worker(cfg, cancel_event=cancel_event))
    if not reply["warming"]:
        reply["state"] = "error"
        reply["error"] = "Model warm-up failed. Try again or select another prepared model."
    return reply
