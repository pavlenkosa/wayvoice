from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable

from . import fw_worker
from . import languages
from .config import number
from .i18n import tr
from . import __version__
from . import service
from .models import forced_language
from .paths import app_dir, script_path
from .postprocess import normalize

# Engines register at the bottom of this module; nothing above the registry
# branches on an engine id.
#
#: Engine used when a config names none. Must match ``config.DEFAULTS`` and the
#: id registered below.
DEFAULT_ENGINE = "faster-whisper"

RUNTIME_STAMP = ".engine-v1-ready"

# Starting a worker must not delay the daemon, and a worker that failed to
# start is not retried on every dictation.
WORKER_START_TIMEOUT = 20.0
WORKER_POLL_INTERVAL = 0.15
WORKER_PING_TIMEOUT = 1.5
#: How long a model may take to load into the warm worker before the daemon stops
#: watching for it. ``large-v3`` is three gigabytes, and a cold page cache can
#: make the read take minutes.
WARM_TIMEOUT = 900.0
WORKER_CANCEL_GRACE = 3.0
WORKER_RETRY_BACKOFF = 60.0

_job_lock = Lock()
_job_procs: dict[subprocess.Popen, Event | None] = {}
_worker_jobs: dict[Event, int] = {}
_unresolved_worker_jobs: dict[Event | None, set[tuple[int, str]]] = {}

_worker_lock = Lock()
#: Guards the reader threads' line buffers of a running download.
_download_lock = Lock()
_worker_retry_after = 0.0
# Handles of the workers we spawned, so an idle one that exited can be reaped.
_worker_procs: list[subprocess.Popen[str]] = []


class TranscriptionCancelled(RuntimeError):
    pass


class TranscriptionTimeout(RuntimeError):
    pass


class WorkerUnavailable(RuntimeError):
    """The warm worker could not be reached or started.

    Only this failure is worth retrying through the one-shot runner; an error the
    worker reported itself is a real result.
    """


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def faster_runtime() -> Path:
    """Directory holding the Faster-Whisper runtime, in order of preference.

    1. ``WAYVOICE_RUNTIME``;
    2. ``<prefix>/lib/wayvoice/runtime``, used only when it holds an interpreter -
    this is how the Flatpak build finds the engine baked into the image, since
    ``XDG_DATA_HOME`` is a run-time directory inside the sandbox;
    3. ``$XDG_DATA_HOME/wayvoice/runtime``, created on first use by
    :mod:`wayvoice.engine_setup`. This is what the Debian package uses.
    """
    override = os.environ.get("WAYVOICE_RUNTIME")
    if override:
        return Path(override).expanduser()
    bundled = app_dir().parent / "runtime"
    if (bundled / "bin" / "python").exists():
        return bundled
    return _data_home() / "wayvoice" / "runtime"


def faster_stamp() -> Path:
    return faster_runtime() / RUNTIME_STAMP


def setup_status_path() -> Path:
    return _state_home() / "wayvoice" / "engine-status.json"


def worker_socket_path() -> Path:
    """Unix socket of the warm worker.

    The path is owned by :mod:`wayvoice.fw_worker`, which binds it, so the daemon
    and the worker cannot disagree about it.
    """
    return fw_worker.default_socket_path()


# --------------------------------------------------------------------------
# Fetching a model
# --------------------------------------------------------------------------
#: Longest a single model download may take. ``large-v3`` is about 3 GB and a
#: slow line is not a failure, but the download belongs to the user session, so
#: a stalled connection has to end somewhere.
DOWNLOAD_TIMEOUT = 6 * 3600.0

#: Download lines are parsed from the helper's stdout, which speaks
#: ``WV-PROGRESS <done> <total>``; anything else is not progress.
_PROGRESS_PREFIX = "WV-PROGRESS"
_READY_PREFIX = "WV-READY"
_ERROR_PREFIX = "WV-ERROR"


def model_is_present(model_id: str) -> bool:
    """Whether ``model_id`` can be used without touching the network.

    This shares the runtime completeness check, including the tokenizer that
    upstream would otherwise silently download. Disk accounting stays separate.
    """
    from . import model_store

    try:
        model_store.inference_dir(model_id)
    except RuntimeError:
        return False
    return True


def _model_download_args(model_id: str) -> list[str] | None:
    """Command that fetches ``model_id``, or ``None`` when there is nothing to fetch:
    a local directory, or weights an engine keeps somewhere else.
    """
    from . import model_store

    repo = model_store.repo_id_for(model_id)
    runtime_python = faster_runtime() / "bin/python"
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
        from . import model_store
        if model_store.repo_id_for(model_id) is not None:
            # A hub model the daemon could fetch if the tooling were there.
            # "unsupported" would tell the user this model can never be
            # downloaded, and the window would hide the row and the button for
            # it; naming what is missing keeps both visible.
            if not (faster_runtime() / "bin/python").exists():
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
        proc = _spawn_job(
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
                _terminate_process(proc)
                state = "cancelled"
                detail = tr("engine.download_cancelled", language)
                break
            if time.monotonic() - started >= DOWNLOAD_TIMEOUT:
                _terminate_process(proc)
                state = "error"
                detail = tr("engine.download_timeout", language, hours=int(DOWNLOAD_TIMEOUT // 3600))
                break
            _apply_download_lines(lines, result, on_progress)
            time.sleep(0.2)
        _terminate_process(proc)
        for reader in readers:
            reader.join(timeout=5.0)
        _apply_download_lines(lines, result, on_progress)
        _close_pipes(proc)

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
        _release_job(proc)


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


def worker_pid_path() -> Path:
    """Return the file holding the pid of the running worker."""
    return _state_home() / "wayvoice" / "engine-worker.pid"


def worker_log_path() -> Path:
    """Return the log file the worker writes stdout/stderr into."""
    return _state_home() / "wayvoice" / "engine-worker.log"


def _read_setup_status() -> dict[str, Any]:
    try:
        raw = json.loads(setup_status_path().read_text(encoding="utf-8"))
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
    runtime = faster_runtime()
    # Accept previous WayVoice-ready stamps and migrate lazily, so an update does
    # not force a runtime rebuild.
    ready_stamps = list(runtime.glob(".engine-*-ready")) if runtime.exists() else []
    if (faster_stamp().exists() or ready_stamps) and (runtime / "bin/python").exists():
        if ready_stamps and not faster_stamp().exists():
            try:
                faster_stamp().touch()
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


def _spawn_job(args, *, cancel_event, **kwargs):
    """Publish a cancellable child atomically; shutdown cannot miss a late spawn."""
    with _job_lock:
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled("Transcription cancelled")
        proc = subprocess.Popen(args, **kwargs)
        _job_procs[proc] = cancel_event
        return proc


def _release_job(proc):
    try:
        # The leader may have exited while descendants still hold its pipes.
        _terminate_process(proc)
        _close_pipes(proc)
    finally:
        with _job_lock:
            _job_procs.pop(proc, None)


@contextlib.contextmanager
def _worker_job(event):
    if event is not None:
        with _job_lock:
            if event.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            _worker_jobs[event] = _worker_jobs.get(event, 0) + 1
    try:
        yield
    finally:
        if event is not None:
            with _job_lock:
                count = _worker_jobs[event] - 1
                if count:
                    _worker_jobs[event] = count
                else:
                    del _worker_jobs[event]


@contextlib.contextmanager
def _worker_guard(event):
    while not _worker_lock.acquire(timeout=WORKER_POLL_INTERVAL):
        if event is not None and event.is_set():
            raise TranscriptionCancelled("Transcription cancelled")
    try:
        yield
    finally:
        _worker_lock.release()


def cancel_jobs(*events):
    """Snapshot active worker ownership before cancelled waiters can unwind."""
    with _job_lock:
        active_worker = any(owner in _worker_jobs or owner in _unresolved_worker_jobs for owner in events)
        for event in events:
            event.set()
        return active_worker


def stop_jobs(*events, deadline=None, active_worker=False):
    """Force cleanup of this owner's cancellable groups; leave idle worker alone."""
    with _job_lock:
        procs = [proc for proc, event in _job_procs.items()
                 if any(event is owner for owner in events)]
        current_worker = any(owner in _worker_jobs for owner in events)
        unresolved = {identity for owner, identities in _unresolved_worker_jobs.items()
                      if any(owner is event for event in events) for identity in identities}
        active_worker = current_worker or (active_worker and not unresolved)
    for proc in procs:
        grace = 1.5 if deadline is None else min(1.5, max(0.0, (deadline - time.monotonic()) / 2))
        _terminate_process(proc, timeout=grace)
    for identity in unresolved:
        grace = 0.5 if deadline is None else max(0.0, (deadline - time.monotonic()) / 2)
        stop_worker(timeout=grace, expected_identity=identity)
        if _worker_identity(identity[0]) != identity or not _pid_alive(identity[0]):
            _forget_unresolved_worker(identity)
    if active_worker:
        grace = 2.0 if deadline is None else max(0.0, (deadline - time.monotonic()) / 2)
        stop_worker(timeout=grace)


def _terminate_process(proc: subprocess.Popen[str], timeout: float = 1.5) -> bool:
    """Stop a child and its group; return whether it is gone.

    Failures are swallowed because the caller is already on an error path, but the
    answer is reported: a process that survived both signals would leave the caller
    waiting on its pipes.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return proc.poll() is not None
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return proc.poll() is not None
    return True


def _start_readers(proc: subprocess.Popen[str]) -> tuple[list[threading.Thread], list[str], list[str]]:
    """Begin draining the child's pipes at once and return the buffers.

    :func:`_run_cancelable` polls the child rather than talking to it, so nothing
    else drains the pipes; an engine that writes more than one buffer blocks on the
    write and never exits.
    """
    out: list[str] = []
    err: list[str] = []

    def read(stream, sink):
        try:
            sink.append(stream.read() or "")
        except Exception:
            sink.append("")

    threads: list[threading.Thread] = []
    for stream, sink in ((proc.stdout, out), (proc.stderr, err)):
        if stream is None:
            continue
        thread = threading.Thread(target=read, args=(stream, sink), daemon=True)
        thread.start()
        threads.append(thread)
    return threads, out, err


def _close_pipes(proc: subprocess.Popen[str]) -> None:
    """Close a child's pipes, on every path out.

    Otherwise every dictation leaks two descriptors until the session ends.
    """
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _finish(
    proc: subprocess.Popen[str],
    readers: list[threading.Thread],
    error: Exception,
) -> Exception:
    """Stop a child, let its readers finish, and return the error to raise."""
    _terminate_process(proc)
    deadline = time.monotonic() + 5.0
    for thread in readers:
        thread.join(timeout=max(0.1, deadline - time.monotonic()))
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    return error


def _run_cancelable(
    args: list[str],
    *,
    timeout: float,
    cancel_event: Event | None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = _spawn_job(
        args, cancel_event=cancel_event,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        readers, out, err = _start_readers(proc)
        started = time.monotonic()
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                raise _finish(
                    proc, readers, TranscriptionCancelled("Transcription cancelled")
                )
            if timeout > 0 and time.monotonic() - started >= timeout:
                raise _finish(
                    proc,
                    readers,
                    TranscriptionTimeout(f"Transcription exceeded {int(timeout)} seconds"),
                )
            time.sleep(0.12)
        _terminate_process(proc)
        for thread in readers:
            thread.join(timeout=5.0)
        _close_pipes(proc)
        return subprocess.CompletedProcess(
            args, proc.returncode, "".join(out), "".join(err)
        )
    finally:
        _release_job(proc)


# --------------------------------------------------------------------------
# Warm Faster-Whisper worker
#
# Every entry point here falls back to the one-shot runner below when the worker
# is missing, broken or disabled, and that runner stays the reference behaviour.
# --------------------------------------------------------------------------


def _worker_call(payload: dict[str, Any], timeout: float, path: Path | None = None) -> dict[str, Any]:
    """Send one request to the worker and return its reply.

    Wire format as in :mod:`wayvoice.protocol`: one JSON line in, one out.
    """
    target = path or worker_socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(max(0.1, timeout))
    try:
        sock.connect(str(target))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
        if not data:
            raise OSError("worker closed the connection")
        reply = json.loads(bytes(data).decode("utf-8"))
    finally:
        sock.close()
    if not isinstance(reply, dict):
        raise ValueError("malformed worker reply")
    return reply


def _worker_ping(path: Path | None = None) -> dict[str, Any] | None:
    """Return the reply of a healthy worker, or ``None`` when there is none."""
    target = path or worker_socket_path()
    if not target.exists():
        return None
    try:
        reply = _worker_call({"cmd": "ping"}, WORKER_PING_TIMEOUT, target)
    except (OSError, ValueError, TimeoutError):
        return None
    return reply if reply.get("ok") else None


def _worker_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return the engine settings a worker must have been started with."""
    return {
        "model": str(cfg.get("model", "small")),
        "device": str(cfg.get("device", "auto")),
        "beam_size": number(cfg, "beam_size", 5),
        "vad": bool(cfg.get("vad_filter", True)),
    }


def _worker_settings_match(reply: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Whether the running worker matches the current settings.

    An unknown worker (no ``config`` in its reply) is left alone. The version is part
    of the answer: the worker outlives daemon restarts, so after an update the daemon
    would otherwise keep talking to the previous version of the code.
    """
    remote_version = str(reply.get("version") or "")
    if remote_version and (remote_version != __version__ or reply.get("request_status") is not True
                           or reply.get("warm_recovery") is not True or reply.get("request_completion") is not True):
        return False
    remote = reply.get("config")
    if not isinstance(remote, dict):
        return True
    try:
        return {
            "model": str(remote.get("model") or ""),
            "device": str(remote.get("device") or "auto"),
            "beam_size": int(remote.get("beam_size") or 5),
            "vad": bool(remote.get("vad")),
        } == _worker_settings(cfg)
    except (TypeError, ValueError):
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # No permission to signal it, but it exists.
        return True
    return not _pid_is_zombie(pid)


def _pid_is_zombie(pid: int) -> bool:
    """Return whether ``pid`` already exited and only waits to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # "<pid> (<comm>) <state> ..."; comm may contain spaces and parentheses.
    state = stat.rpartition(")")[2].split()
    return bool(state) and state[0] == "Z"


def _reap_workers() -> None:
    """Drop the handles of workers that have already exited."""
    for proc in list(_worker_procs):
        if proc.poll() is not None:
            _worker_procs.remove(proc)


def _is_worker_process(pid: int) -> bool:
    """Whether ``pid`` looks like a WayVoice worker process.

    The pid file can outlive a crash and pids get recycled, so the command line is
    verified before anything is signalled. ``--serve`` is required too: stopping
    "the worker" must not kill a transcription running on its own.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv = raw.decode("utf-8", "replace")
    return "fw_runner.py" in argv and "--serve" in argv


def _worker_identity(pid: int) -> tuple[int, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
        return pid, fields[19]  # Linux field 22: process start time.
    except (OSError, IndexError):
        return None


def stop_worker(timeout: float = 2.0, expected_identity=None, cleanup=True) -> bool:
    """Stop the running worker, if any.

    Best effort: this is used to replace a worker that was started for stale
    settings.
    """
    stopped = False
    pid = 0
    try:
        pid = int(worker_pid_path().read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        pid = 0
    if expected_identity is not None:
        pid = expected_identity[0]
        if _worker_identity(pid) != expected_identity:
            return False
    if pid > 1 and _is_worker_process(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if expected_identity is not None and _worker_identity(pid) != expected_identity:
                break
            try:
                os.kill(pid, sig)
            except OSError:
                break
            stopped = True
            deadline = time.monotonic() + timeout
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not _pid_alive(pid):
                break
    if expected_identity is not None and _pid_alive(pid):
        return False
    if not cleanup:
        return stopped
    try:
        if expected_identity is None:
            worker_pid_path().unlink()
        elif not _pid_alive(pid):
            # Another operation may already have published a replacement.
            current_pid = int(worker_pid_path().read_text().split()[0])
            if current_pid == pid:
                worker_pid_path().unlink()
    except (OSError, ValueError, IndexError):
        pass
    # The socket is unlinked only when nothing answers on it.
    #
    # ``stopped`` says that a pid which looked like a worker was signalled - not
    # that the worker this socket belongs to is gone. With a stale pid file (a
    # recycled pid, a cleared XDG_STATE_HOME, a pid file already consumed by an
    # earlier call) the sequence was: fail to identify the running worker, unlink
    # its pid file, start a second one that cannot bind because the first still
    # holds the socket, write the second one's pid, then stop *that* one - and
    # ``stopped`` being true unlinked the first worker's live socket. It kept
    # running, holding the model and several gigabytes of RAM, invisible to
    # _worker_ping() - which gives up on a path that does not exist. Every dictation
    # then quietly fell back to loading the model from scratch, with no error
    # anywhere.
    if _worker_ping() is None:
        try:
            worker_socket_path().unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


def worker_info() -> dict[str, Any]:
    """What the warm worker is holding: ``{"running", "model", "version"}``.

    The settings window asks before it offers to delete a model, and a model in the
    worker's memory is one the user is about to need again. The model is the raw
    configured value, so the caller compares it with what the window shows rather
    than with a repository id.

    Never blocks for long: a worker that does not answer the ping within
    :data:`WORKER_PING_TIMEOUT` counts as not running, which is what
    :func:`stop_worker` acts on.
    """
    reply = _worker_ping()
    if reply is None:
        return {"running": False, "model": "", "version": ""}
    remote = reply.get("config")
    model = str(remote.get("model") or "") if isinstance(remote, dict) else ""
    return {
        "running": True,
        "model": model,
        "version": str(reply.get("version") or ""),
    }


def _start_worker(cfg: dict[str, Any]) -> bool:
    """Spawn the worker process in its own session; never blocks."""
    runtime_python = faster_runtime() / "bin/python"
    if not runtime_python.exists():
        return False
    args = [
        str(runtime_python), str(script_path("fw_runner.py")),
        "--serve",
        "--socket", str(worker_socket_path()),
        "--model", _worker_settings(cfg)["model"],
        "--device", str(cfg.get("device", "auto")),
        "--beam-size", str(number(cfg, "beam_size", 5)),
    ]
    if cfg.get("vad_filter", True):
        args.append("--vad")
    args += ["--idle-timeout", str(max(0.0, number(cfg, "engine_worker_idle_sec", 900)))]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    log = None
    try:
        log_path = worker_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("a", encoding="utf-8")
    except OSError:
        log = None
    try:
        proc = subprocess.Popen(
            args,
            stdout=log or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False
    finally:
        if log is not None:
            log.close()
    _worker_procs.append(proc)
    try:
        worker_pid_path().write_text(f"{proc.pid}\n", encoding="utf-8")
    except OSError:
        pass
    return True


def ensure_worker(cfg: dict[str, Any], cancel_event: Event | None = None) -> bool:
    """Make sure a warm worker matching ``cfg`` is available.

    ``False`` means the caller should fall back to the one-shot runner. A worker that
    cannot be started is not retried for a while, so a broken runtime cannot add its
    start timeout to every dictation.
    """
    global _worker_retry_after

    if cancel_event is not None and cancel_event.is_set():
        raise TranscriptionCancelled("Transcription cancelled")
    path = worker_socket_path()
    with _worker_guard(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled("Transcription cancelled")
        _retry_unresolved_workers()
        _reap_workers()
        reply = _worker_ping(path)
        if reply is not None:
            if _worker_settings_match(reply, cfg):
                return True
            # The worker was started for other settings (typically a changed
            # model); replace it so it cannot keep serving a stale one.
            stop_worker()
        if time.monotonic() < _worker_retry_after:
            return False
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled("Transcription cancelled")
        with _job_lock:
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            started_worker = _start_worker(cfg)
        if not started_worker:
            _worker_retry_after = time.monotonic() + WORKER_RETRY_BACKOFF
            return False
        deadline = time.monotonic() + WORKER_START_TIMEOUT
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            reply = _worker_ping(path)
            if reply is not None:
                if _worker_settings_match(reply, cfg):
                    return True
                stop_worker()
                _worker_retry_after = time.monotonic() + WORKER_RETRY_BACKOFF
                return False
            if time.monotonic() >= deadline:
                _worker_retry_after = time.monotonic() + WORKER_RETRY_BACKOFF
                return False
            time.sleep(WORKER_POLL_INTERVAL)


def _worker_send_cancel(request_id: str) -> None:
    """Ask the worker to abort a request; best effort by design."""
    try:
        _worker_call({"cmd": "cancel", "request_id": request_id}, WORKER_PING_TIMEOUT)
    except (OSError, ValueError, TimeoutError):
        pass


def _forget_unresolved_worker(identity):
    with _job_lock:
        for owner in list(_unresolved_worker_jobs):
            _unresolved_worker_jobs[owner].discard(identity)
            if not _unresolved_worker_jobs[owner]:
                del _unresolved_worker_jobs[owner]


def _retry_unresolved_workers():
    with _job_lock:
        identities = {identity for owned in _unresolved_worker_jobs.values() for identity in owned}
    for identity in identities:
        stop_worker(timeout=0.5, expected_identity=identity)
        if _worker_identity(identity[0]) == identity and _pid_alive(identity[0]):
            raise RuntimeError("Previous worker request is still running; automatic retry was blocked.")
        _forget_unresolved_worker(identity)


def _recover_worker_request(request_id, identity, cancel_event=None):
    """Retire only the instance still executing this abandoned request."""
    if identity is None or _worker_identity(identity[0]) != identity:
        return True
    try:
        status = _worker_call({"cmd": "request-status", "request_id": request_id}, WORKER_PING_TIMEOUT)
    except (OSError, ValueError, TimeoutError):
        status = None
    if isinstance(status, dict) and status.get("pid") == identity[0] and status.get("request_id") == request_id:
        if status.get("state") == "finished" or (status.get("state") == "queued" and status.get("cancelled") is True):
            return True
    # Startup may own this lock for seconds. Process identity makes retirement
    # safe without waiting indefinitely; only the lock owner mutates endpoints.
    acquired = _worker_lock.acquire(timeout=WORKER_POLL_INTERVAL)
    try:
        stop_worker(timeout=0.5, expected_identity=identity, cleanup=acquired)
    finally:
        if acquired:
            _worker_lock.release()
    if _worker_identity(identity[0]) != identity or not _pid_alive(identity[0]):
        _forget_unresolved_worker(identity)
        return True
    with _job_lock:
        _unresolved_worker_jobs.setdefault(cancel_event, set()).add(identity)
    return False


def _recover_lost_worker_request(request_id, identity, deadline=None, cancel_event=None):
    """Keep ownership after socket loss until completion or bounded retirement."""
    if deadline is None:
        _worker_send_cancel(request_id)
        deadline = time.monotonic() + WORKER_CANCEL_GRACE
    while time.monotonic() < deadline:
        if identity is None or _worker_identity(identity[0]) != identity:
            return True
        remaining = max(0.001, deadline - time.monotonic())
        try:
            status = _worker_call({"cmd": "request-status", "request_id": request_id},
                                  min(WORKER_PING_TIMEOUT, remaining))
        except (OSError, ValueError, TimeoutError):
            status = None
        if isinstance(status, dict) and status.get("pid") == identity[0] and status.get("request_id") == request_id:
            if status.get("state") == "finished":
                return True
        time.sleep(min(WORKER_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
    return _recover_worker_request(request_id, identity, cancel_event)


def _worker_request(payload: dict[str, Any], request_id: str, timeout: float, cancel_event: Event | None) -> dict[str, Any]:
    """Run an addressed worker request while honouring cancel and timeout.

    The socket is polled rather than read in one blocking call, so a cancel is
    noticed within a fraction of a second even though the model may be busy for a
    minute. Raises :class:`WorkerUnavailable` when the worker cannot be talked to, and
    :class:`TranscriptionCancelled` as soon as the user asked to stop, including when
    the answer arrived first.
    """
    with _worker_job(cancel_event):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(WORKER_POLL_INTERVAL)
        try:
            try:
                sock.connect(str(worker_socket_path()))
                peer_pid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0]
            except OSError as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise TranscriptionCancelled("Transcription cancelled") from exc
                raise WorkerUnavailable("worker connection is unavailable") from exc
            identity = _worker_identity(peer_pid)
            if identity is None:
                raise WorkerUnavailable("worker identity cannot be verified")
            try:
                health = _worker_call({"cmd": "request-status", "request_id": request_id}, WORKER_PING_TIMEOUT)
            except (OSError, ValueError, TimeoutError) as exc:
                raise WorkerUnavailable("worker recovery protocol is unavailable") from exc
            if not (health.get("ok") and health.get("pid") == peer_pid
                    and health.get("request_id") == request_id
                    and health.get("state") in {"queued", "running", "finished", "unknown"}
                    and health.get("request_completion") is True
                    and (payload.get("cmd") != "warm" or health.get("warm_recovery") is True)):
                raise WorkerUnavailable("worker does not support request recovery")
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            try:
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            except OSError as exc:
                resolved = _recover_lost_worker_request(request_id, identity, cancel_event=cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    raise TranscriptionCancelled("Transcription cancelled") from exc
                if not resolved:
                    raise RuntimeError("Worker request is still running; fallback was blocked.") from exc
                raise WorkerUnavailable(f"worker send failed: {exc}") from exc
            started = time.monotonic()
            cancel_deadline: float | None = None
            failure = None
            data = bytearray()
            while True:
                if cancel_event is not None and cancel_event.is_set() and cancel_deadline is None:
                    _worker_send_cancel(request_id)
                    cancel_deadline = time.monotonic() + WORKER_CANCEL_GRACE
                    failure = TranscriptionCancelled("Transcription cancelled")
                if cancel_deadline is None and timeout > 0 and time.monotonic() - started >= timeout:
                    _worker_send_cancel(request_id)
                    cancel_deadline = time.monotonic() + WORKER_CANCEL_GRACE
                    failure = TranscriptionTimeout(f"Transcription exceeded {int(timeout)} seconds")
                if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                    _recover_worker_request(request_id, identity, cancel_event)
                    raise failure
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                except OSError as exc:
                    resolved = _recover_lost_worker_request(request_id, identity, cancel_deadline, cancel_event)
                    if failure is not None:
                        raise failure
                    if cancel_event is not None and cancel_event.is_set():
                        raise TranscriptionCancelled("Transcription cancelled") from exc
                    if not resolved:
                        raise RuntimeError("Worker request is still running; fallback was blocked.") from exc
                    raise WorkerUnavailable(f"worker connection failed: {exc}") from exc
                if not chunk:
                    resolved = _recover_lost_worker_request(request_id, identity, cancel_deadline, cancel_event)
                    if failure is not None:
                        raise failure
                    if cancel_event is not None and cancel_event.is_set():
                        raise TranscriptionCancelled("Transcription cancelled")
                    if not resolved:
                        raise RuntimeError("Worker request is still running; fallback was blocked.")
                    raise WorkerUnavailable("worker closed the connection")
                data.extend(chunk)
                if data.endswith(b"\n"):
                    break
            if failure is not None:
                raise failure
            if cancel_event is not None and cancel_event.is_set():
                # The answer was in flight when the user pressed cancel: honour it,
                # the one-shot runner would have killed the process instead.
                raise TranscriptionCancelled("Transcription cancelled")
            try:
                reply = json.loads(bytes(data).decode("utf-8"))
            except ValueError as exc:
                raise WorkerUnavailable(f"malformed worker reply: {exc}") from exc
        finally:
            sock.close()
        if not isinstance(reply, dict) or reply.get("request_id") != request_id:
            raise WorkerUnavailable("malformed worker reply or mismatched request id")
        return reply


def _worker_transcribe(payload, request_id, timeout, cancel_event):
    return _worker_request(payload, request_id, timeout, cancel_event)


def _language(cfg: dict[str, Any]) -> str:
    """Effective recognition language for a config.

    A single-language model overrides the setting. A stale value from an old
    ``config.json`` normalizes to a real code or to ``auto``, and ``auto`` is passed
    on: faster-whisper turns it into ``None``, whisper.cpp detects the language itself
    with ``-l auto``.
    """
    model = str(cfg.get("model", "small"))
    return languages.normalize(forced_language(model) or cfg.get("language"))


def _transcribe_via_worker(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    """Transcribe through the warm worker.

    :class:`WorkerUnavailable` when the worker itself is unreachable, so the caller
    can retry through the one-shot runner; a plain :class:`RuntimeError` when the
    worker reported a genuine engine error.
    """
    if not ensure_worker(cfg, cancel_event=cancel_event):
        raise WorkerUnavailable("the Faster-Whisper worker is not available")
    request_id = uuid.uuid4().hex
    payload = {
        "cmd": "transcribe",
        "audio": str(audio),
        "language": _language(cfg),
        "request_id": request_id,
    }
    reply = _worker_transcribe(
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
    if text and cfg.get("append_space", True):
        text += " "
    return text


def _transcribe_faster(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    runtime_python = faster_runtime() / "bin/python"
    if not runtime_python.exists():
        raise RuntimeError("Faster-Whisper is not ready")
    with _job_lock:
        unresolved = bool(_unresolved_worker_jobs)
    if unresolved:
        with _worker_guard(cancel_event):
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            _retry_unresolved_workers()
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
    cp = _run_cancelable(
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
    cp = _run_cancelable(
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
    cp = _run_cancelable(
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
    model_present=lambda cfg: model_is_present(str(cfg.get("model", ""))),
    model_download=download_configured_model,
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
    from . import model_store

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
    reply["warming"] = (warm_worker(cfg) if cancel_event is None
                        else warm_worker(cfg, cancel_event=cancel_event))
    return reply


def warm_worker(cfg: dict[str, Any], timeout: float = WARM_TIMEOUT,
                cancel_event: Event | None = None) -> bool:
    """Load the model through an owned, cancellable addressed request.

    Timeout/unavailable workers return False; cancellation remains explicit.
    Native loads that cannot unwind within grace retire only their own worker.
    """
    with _worker_job(cancel_event):
        if timeout <= 0:
            return False
        if not ensure_worker(cfg, cancel_event=cancel_event):
            return False
        request_id = uuid.uuid4().hex
        try:
            reply = _worker_request({"cmd": "warm", "request_id": request_id},
                                    request_id, timeout, cancel_event)
        except (WorkerUnavailable, TranscriptionTimeout, OSError):
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled("Warm-up cancelled")
            return False
        if reply.get("cancelled"):
            raise TranscriptionCancelled("Warm-up cancelled")
        return bool(reply.get("ok") and reply.get("warm"))


def request_engine_setup(engine: Engine | None) -> bool:
    """Ask for the runtime of ``engine`` to be prepared in the background.

    ``False`` when that engine needs no preparation, so a caller can tell the user
    instead of doing nothing quietly.
    """
    if engine is None or not engine.needs_setup or engine.setup is None:
        return False
    engine.setup()
    return True
