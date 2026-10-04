from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable

from . import fw_worker
from . import languages
from . import __version__
from . import service
from .models import forced_language
from .paths import app_dir, script_path
from .postprocess import normalize

# The engines are registered at the very bottom of this module, in
# :data:`ENGINES`, because their ``transcribe``/``status`` callables are the
# functions defined here.  Nothing above the registry branches on an engine id.
#
#: Engine used when a config does not name one.  It has to agree with the id
#: registered below and with the shipped ``config.DEFAULTS``; a test holds the
#: three together.
DEFAULT_ENGINE = "faster-whisper"

RUNTIME_STAMP = ".engine-v1-ready"

# Warm-worker limits.  Starting the worker must never delay the daemon for
# long, and a worker that failed to start is not retried on every dictation.
WORKER_START_TIMEOUT = 20.0
WORKER_POLL_INTERVAL = 0.15
WORKER_PING_TIMEOUT = 1.5
WORKER_CANCEL_GRACE = 3.0
WORKER_RETRY_BACKOFF = 60.0

_worker_lock = Lock()
#: Guards the reader threads' line buffers of a running download.
_download_lock = Lock()
_worker_retry_after = 0.0
# Handles of the workers we spawned, so an idle worker that exited can be
# reaped instead of lingering as a zombie for the rest of the session.
_worker_procs: list[subprocess.Popen[str]] = []


class TranscriptionCancelled(RuntimeError):
    pass


class TranscriptionTimeout(RuntimeError):
    pass


class WorkerUnavailable(RuntimeError):
    """The warm worker could not be reached or started.

    Only this failure is worth retrying through the one-shot runner. An error
    the worker *did* report (a bad model id, an out-of-memory CUDA context) is
    a real result: repeating it through a second, freshly spawned process would
    only double the latency of the failure.
    """


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def faster_runtime() -> Path:
    """Return the directory holding the Faster-Whisper runtime.

    Three locations, in order:

    1. ``WAYVOICE_RUNTIME`` -- an explicit override;
    2. a runtime shipped next to the application, i.e.
       ``<prefix>/lib/wayvoice/runtime``. Only used when it really holds a
       Python interpreter, so a leftover empty directory is ignored. This is
       how the Flatpak build finds the engine that was baked into the image:
       inside a sandbox ``XDG_DATA_HOME`` is a run-time directory, so the
       per-user location below cannot hold a prebuilt engine;
    3. the per-user ``$XDG_DATA_HOME/wayvoice/runtime``, which
       :mod:`wayvoice.engine_setup` creates on first use. This is what the
       Debian package uses.
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
    """Return the unix socket of the warm Faster-Whisper worker.

    The location itself is owned by :mod:`wayvoice.fw_worker` (the process
    that binds it), so daemon and worker can never disagree about it.
    """
    return fw_worker.default_socket_path()


# --------------------------------------------------------------------------
# Fetching a model
# --------------------------------------------------------------------------
#: Longest a single model download may take.  Generous, because ``large-v3`` is
#: about 3 GB and a slow line is not a failure - but not infinite either: the
#: process belongs to the user session, and a stalled connection used to leave
#: it there with nothing to show for it.
DOWNLOAD_TIMEOUT = 6 * 3600.0

#: Download lines are parsed from the helper's stdout, which speaks
#: ``WV-PROGRESS <done> <total>``; anything else it prints is not progress.
_PROGRESS_PREFIX = "WV-PROGRESS"
_READY_PREFIX = "WV-READY"
_ERROR_PREFIX = "WV-ERROR"


def model_is_present(model_id: str) -> bool:
    """Whether ``model_id`` can be used without touching the network.

    Weights alone are not enough: a snapshot whose ``config.json`` was deleted
    or never finished downloading passes a weight check and then fails at load
    time with an error about a file the user has never heard of.  Both are the
    same missing model, so both are checked here.
    """
    from . import model_store

    if not model_store.is_downloaded(model_id):
        return False
    snapshot = model_store.snapshot_dir(model_id)
    return bool(snapshot and (snapshot / "config.json").exists())


def _model_download_args(model_id: str) -> list[str] | None:
    """Command that fetches ``model_id``, or ``None`` when it cannot be fetched.

    ``None`` means "not a hub model": a local directory the user provides, or a
    model for an engine that keeps its weights somewhere else entirely.
    """
    from . import model_store

    repo = model_store.repo_id_for(model_id)
    runtime_python = faster_runtime() / "bin/python"
    if repo is None or not runtime_python.exists():
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
) -> dict[str, Any]:
    """Fetch a model into the hub cache, reporting progress as it arrives.

    Returns ``{"state": "ready" | "error" | "cancelled" | "unsupported",
    "error": str, "done": int, "total": int}``.  Never raises: this runs on the
    daemon's preparation path, where an exception would be a daemon that cannot
    start.

    A model that is already present returns immediately without a network
    request at all -- the daemon asks about this on every start, and a check
    that talked to huggingface.co would be both slow and rude.
    """
    result: dict[str, Any] = {"state": "ready", "error": "", "done": 0, "total": 0}
    if model_is_present(model_id):
        return result
    args = _model_download_args(model_id)
    if args is None:
        return {
            "state": "unsupported",
            "error": f"Cannot download this model: {model_id}",
            "done": 0,
            "total": 0,
        }
    env = os.environ.copy()
    # The helper speaks its own progress protocol on stdout; the hub's own bars
    # would interleave with it on stderr.
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return {"state": "error", "error": str(exc), "done": 0, "total": 0}

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
            state, detail = "cancelled", "Download cancelled"
            break
        if time.monotonic() - started >= DOWNLOAD_TIMEOUT:
            _terminate_process(proc)
            state = "error"
            detail = f"Download did not finish within {int(DOWNLOAD_TIMEOUT // 3600)} hours"
            break
        _apply_download_lines(lines, result, on_progress)
        time.sleep(0.2)
    for reader in readers:
        reader.join(timeout=5.0)
    _apply_download_lines(lines, result, on_progress)

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
        # The helper said it was done and the weights are not there. Reporting
        # success here would send the user to the hotkey for a model that is not
        # on disk.
        result["state"] = "error"
        result["error"] = "The download finished but the model is not on disk"
    return result


def download_configured_model(
    cfg: dict[str, Any],
    on_progress: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    """Fetch the model the *config* names; the registry hook.

    :func:`download_model` takes a model id because that is what most callers
    have, while a registered hook is always handed the whole config.  The
    adapter between the two lives here, in one named function, rather than as a
    lambda in the registry where a wrong argument order would only show up as a
    model that reports itself as "nothing to download".
    """
    return download_model(str(cfg.get("model", "")), on_progress, cancel_event)


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

    The list is the reader threads' buffer, so it is drained in one go and only
    while holding it: two threads append to it while this runs.
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
                        # A progress callback that raises must not end a
                        # download that is going fine.
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

    How that request reaches the machine is decided by
    :mod:`wayvoice.service` -- the user unit where one exists, a directly
    spawned ``wayvoice.engine_setup`` otherwise.
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
    # Accept previous WayVoice-ready stamps and migrate lazily. This avoids
    # a needless runtime rebuild after every application update.
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

    An id no engine claims is an error, never a silent fallback: a hand-edited
    config that quietly started using another recognizer would be worse than
    a visible failure.
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


def _terminate_process(proc: subprocess.Popen[str]) -> bool:
    """Stop a child and its group; return whether it is really gone.

    Every failure here is swallowed on purpose - the caller is already on an
    error path - but the outcome is reported, because a process that survived
    both signals would leave the caller waiting on its pipes forever.
    """
    if proc.poll() is not None:
        return True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return proc.poll() is not None
    try:
        proc.wait(timeout=1.5)
        return True
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
        proc.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        return proc.poll() is not None
    return True


def _start_readers(proc: subprocess.Popen[str]) -> tuple[list[threading.Thread], list[str], list[str]]:
    """Begin draining the child's pipes at once and return the buffers.

    ``communicate()`` alone is not enough here: the loop in
    :func:`_run_cancelable` polls the child instead of talking to it, so nothing
    drains the pipes while it runs.  An engine that writes more than one pipe
    buffer - ctranslate2 is not quiet about a model it does not like - blocks on
    the write and never exits, and the user is told the recognition timed out
    rather than what actually happened.
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
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
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
    for thread in readers:
        thread.join(timeout=5.0)
    return subprocess.CompletedProcess(
        args, proc.returncode, "".join(out), "".join(err)
    )


# --------------------------------------------------------------------------
# Warm Faster-Whisper worker
#
# Everything in this section is an optimisation.  Every entry point swallows its
# own failures so that a missing, broken or disabled worker falls back to the
# one-shot runner below, which stays the reference behaviour.
# --------------------------------------------------------------------------


def _worker_call(payload: dict[str, Any], timeout: float, path: Path | None = None) -> dict[str, Any]:
    """Send one request to the worker and return its reply.

    The wire format matches :mod:`wayvoice.protocol`: one JSON line in, one
    JSON line out.
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
        "beam_size": int(cfg.get("beam_size", 5)),
        "vad": bool(cfg.get("vad_filter", True)),
    }


def _worker_settings_match(reply: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """Return whether the running worker matches the current settings.

    An unknown worker (no ``config`` in its reply) is left alone: replacing it
    would be worse than trusting it.

    The version is part of the answer. The worker keeps running across daemon
    restarts - that is what makes it warm - so after an update the daemon would
    otherwise keep sending requests to a process that is running the previous
    version of the code, and neither of them would ever mention it.
    """
    remote_version = str(reply.get("version") or "")
    if remote_version and remote_version != __version__:
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
    """Return whether ``pid`` looks like a WayVoice worker process.

    The pid file can outlive a crash and pids get recycled, so the command line
    is verified before anything is signalled.  ``--serve`` is required as well:
    the one-shot runner is the same script, and stopping "the worker" must never
    kill a transcription that is running on its own.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv = raw.decode("utf-8", "replace")
    return "fw_runner.py" in argv and "--serve" in argv


def stop_worker() -> bool:
    """Stop the running worker, if any.

    Returns ``True`` when a worker was signalled.  Best effort by design: it is
    used to replace a worker that was started for stale settings.
    """
    stopped = False
    pid = 0
    try:
        pid = int(worker_pid_path().read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        pid = 0
    if pid > 1 and _is_worker_process(pid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            stopped = True
            deadline = time.monotonic() + 2.0
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not _pid_alive(pid):
                break
    try:
        worker_pid_path().unlink()
    except OSError:
        pass
    # The worker removes its socket on the way out; a leftover file belongs to
    # nobody and would make every later connection fail with ECONNREFUSED.  A
    # socket that still answers is left alone: it belongs to a live worker.
    if stopped or _worker_ping() is None:
        try:
            worker_socket_path().unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


def worker_info() -> dict[str, Any]:
    """What the warm worker is holding right now.

    ``{"running": bool, "model": str, "version": str}``.  The settings window
    asks before it offers to delete a model, and a model held in the worker's
    memory is one the user is about to need again: deleting it under the
    worker's feet would leave the recognizer claiming a model that is no longer
    there.  The model is the raw configured value, so the caller compares it
    with what the window shows rather than with a repository id.

    Never raises and never blocks for long: a worker that does not answer the
    ping within :data:`WORKER_PING_TIMEOUT` counts as "not running", which is
    the same answer :func:`stop_worker` acts on.
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
        "--beam-size", str(int(cfg.get("beam_size", 5))),
    ]
    if cfg.get("vad_filter", True):
        args.append("--vad")
    args += ["--idle-timeout", str(max(0.0, float(cfg.get("engine_worker_idle_sec", 900))))]
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


def ensure_worker(cfg: dict[str, Any]) -> bool:
    """Make sure a warm worker matching ``cfg`` is available.

    Returns ``False`` when no worker could be reached or started; the caller
    then falls back to the one-shot runner.  A worker that cannot be started is
    not retried for a while, so a broken runtime cannot add its start timeout
    to every single dictation.
    """
    global _worker_retry_after

    path = worker_socket_path()
    with _worker_lock:
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
        if not _start_worker(cfg):
            _worker_retry_after = time.monotonic() + WORKER_RETRY_BACKOFF
            return False
        deadline = time.monotonic() + WORKER_START_TIMEOUT
        while True:
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


def _worker_transcribe(payload: dict[str, Any], request_id: str, timeout: float, cancel_event: Event | None) -> dict[str, Any]:
    """Run one transcription on the worker while honouring cancel and timeout.

    The socket is polled instead of read in one blocking call, so a cancel is
    noticed within a fraction of a second even though the model may be busy for
    a minute.

    Raises :class:`WorkerUnavailable` when the worker cannot be talked to, and
    :class:`TranscriptionCancelled` as soon as the user asked to stop -- also
    when the answer arrived first, so a cancel is never silently ignored.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(WORKER_POLL_INTERVAL)
    try:
        sock.connect(str(worker_socket_path()))
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        started = time.monotonic()
        cancel_deadline: float | None = None
        data = bytearray()
        while True:
            if cancel_event is not None and cancel_event.is_set() and cancel_deadline is None:
                _worker_send_cancel(request_id)
                cancel_deadline = time.monotonic() + WORKER_CANCEL_GRACE
            if cancel_deadline is not None:
                # Give the worker a moment to unwind, then give up on it.
                if time.monotonic() >= cancel_deadline:
                    raise TranscriptionCancelled("Transcription cancelled")
            elif timeout > 0 and time.monotonic() - started >= timeout:
                _worker_send_cancel(request_id)
                raise TranscriptionTimeout(f"Transcription exceeded {int(timeout)} seconds")
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                continue
            except OSError as exc:
                raise WorkerUnavailable(f"worker connection failed: {exc}") from exc
            if not chunk:
                raise WorkerUnavailable("worker closed the connection")
            data.extend(chunk)
            if data.endswith(b"\n"):
                break
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
    if not isinstance(reply, dict):
        raise WorkerUnavailable("malformed worker reply")
    return reply


def _language(cfg: dict[str, Any]) -> str:
    """Effective recognition language for a config.

    A single-language model overrides the setting, and the result is always a
    code the engine understands: a stale value left in an old ``config.json``
    (``"RU_ru"``, ``"klingon"``, ``""``) normalizes to a real code or to
    :data:`~wayvoice.languages.AUTO`.  ``auto`` is passed on as ``auto``:
    faster-whisper turns it into ``None`` for the model, and whisper.cpp
    detects the language itself with ``-l auto``.
    """
    model = str(cfg.get("model", "small"))
    return languages.normalize(forced_language(model) or cfg.get("language"))


def _transcribe_via_worker(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    """Transcribe through the warm worker.

    Raises :class:`WorkerUnavailable` when the worker itself is unreachable, so
    the caller can retry through the one-shot runner, and a plain
    :class:`RuntimeError` when the worker reported a genuine engine error.
    """
    if not ensure_worker(cfg):
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
        float(cfg.get("transcription_timeout_sec", 90)),
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
            # actually in, which is the model's own language when it forces one.
            language=_language(cfg),
        )
    if text and cfg.get("append_space", True):
        text += " "
    return text


def _transcribe_faster(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    runtime_python = faster_runtime() / "bin/python"
    if not runtime_python.exists():
        raise RuntimeError("Faster-Whisper is not ready")
    if bool(cfg.get("engine_worker", True)):
        # The warm worker only removes the model load from the critical path.
        # When the worker itself is unusable we quietly use the one-shot runner
        # below. An error the worker *did* report is not retried: that would
        # run the same failing job twice and double the latency of the failure.
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
        "--beam-size", str(int(cfg.get("beam_size", 5))),
    ]
    if cfg.get("vad_filter", True):
        args.append("--vad")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    cp = _run_cancelable(
        args,
        timeout=float(cfg.get("transcription_timeout_sec", 90)),
        cancel_event=cancel_event,
        env=env,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "Faster-Whisper failed")
    return (cp.stdout or "").strip()


def _transcribe_whisper_cpp(audio: Path, cfg: dict[str, Any], cancel_event: Event | None) -> str:
    binary = _find_whisper_cpp(cfg)
    model = Path(str(cfg.get("whisper_cpp_model") or "")).expanduser()
    if not binary:
        raise RuntimeError("whisper-cli was not found")
    if not model.is_file():
        raise RuntimeError("whisper.cpp model was not found")
    language = _language(cfg)
    args = [binary, "-m", str(model), "-f", str(audio), "-l", language, "-nt", "-np"]
    if not bool(cfg.get("whisper_cpp_gpu", True)):
        args.append("-ng")
    cp = _run_cancelable(
        args,
        timeout=float(cfg.get("transcription_timeout_sec", 90)),
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
        timeout=float(cfg.get("transcription_timeout_sec", 90)),
        cancel_event=cancel_event,
    )
    if cp.returncode != 0:
        detail = (cp.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "External recognition command failed")
    return (cp.stdout or "").strip()


def transcribe(audio: Path, cfg: dict[str, Any], cancel_event: Event | None = None) -> str:
    """Recognize ``audio`` with the configured engine and postprocess the text.

    The engine itself returns the raw transcript; punctuation and the trailing
    space are shared by all of them and stay here.
    """
    engine_id = str(cfg.get("engine", DEFAULT_ENGINE))
    engine = get_engine(engine_id)
    if engine is None:
        raise RuntimeError(f"Unknown engine: {engine_id}")
    return _postprocess(engine.transcribe(audio, cfg, cancel_event), cfg)


# --------------------------------------------------------------------------
# Engine registry
#
# This is the single place that knows which engines exist.  Everything else --
# the daemon, the CLI, the settings window -- asks the registry instead of
# comparing engine ids, so adding an engine is one entry here plus its
# ``_transcribe_*``/``_status_*`` functions above, and nothing is left behind
# that still believes there are only two or three engines.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Engine:
    """One recognition engine: how to run it and what its settings look like.

    ``transcribe`` returns the raw text and ``status`` a ``state``/``message``
    pair; the identity of the engine (``id``/``label``) is added by
    :func:`engine_status`, which keeps every engine's report uniform.

    ``settings`` lists the config keys that belong to this engine and to no
    other -- the settings window shows a row exactly when its key is in there.
    Keys every engine shares (language, timeouts) are in none of them.

    ``setup`` is the optional hook that prepares the engine's runtime in the
    background.  It is what ``needs_setup`` advertises to the UI, and the two
    must agree: a button that prepares nothing must not be shown.

    ``model_present``/``model_download`` are the same agreement for weights: an
    engine that reports a model as missing and has no way to fetch it would show
    a download that cannot start.
    """

    id: str
    label: str
    transcribe: Callable[[Path, dict[str, Any], Event | None], str]
    status: Callable[[dict[str, Any]], dict[str, Any]]
    uses_models: bool
    needs_setup: bool
    settings: tuple[str, ...]
    setup: Callable[[], None] | None = None
    #: Whether the engine's weights are already usable, when that is a question
    #: this daemon can answer.  ``None`` for an engine whose weights live
    #: outside the hub cache - a local folder, or an external command.
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
    # settings only exist for this engine.  ``compute_type_*`` are still read
    # by nothing (the quantization is derived from the device), but they name a
    # faster-whisper setting, so they belong here rather than to nobody.
    settings=(
        "model",
        "custom_model",
        "compute_type_cpu",
        "compute_type_cuda",
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

    ``None`` is handed back instead of a default engine on purpose: a config
    that names a nonexistent engine is broken, and only the caller knows how
    much to say about it.
    """
    return get_engine(cfg.get("engine", DEFAULT_ENGINE))


def model_state(engine: Engine | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """What is known about the weights the config names.

    ``{"supported": bool, "present": bool, "model": str}``.  ``supported`` asks
    whether *this* value is a model WayVoice manages, and that is two questions
    answered in turn: does the engine have hub models at all, and is the value
    one of them.

    Both matter, and confusing them breaks dictation for people who did nothing
    wrong.  A local directory is a perfectly good model - the settings window
    asks for exactly that, and the engine hands the path straight to
    Faster-Whisper, which loads it from disk - but nothing about it can be
    fetched, counted or deleted.  Answering "missing" for it makes the daemon
    refuse every hot-key press, and answering with an error when asked to
    prepare it makes a working setup look broken.  So for a value that is not a
    hub repository the answer is "not ours": not missing, not downloadable.
    """
    model_id = str(cfg.get("model", ""))
    foreign = {"supported": False, "present": True, "model": model_id}
    if engine is None or engine.model_present is None:
        return foreign
    from . import model_store

    if model_store.repo_id_for(model_id) is None:
        # Not a hub repository: a local path, a nested path, or a free-form
        # value.  ``repo_id_for`` is the same answer the cache layout is built
        # from, so "not downloadable" and "not in our cache" cannot disagree.
        return foreign
    try:
        present = bool(engine.model_present(cfg))
    except Exception:
        # A cache that cannot even be walked is not a reason to claim the model
        # is missing: that would start a download of something already there.
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
    """Fetch the engine's weights, and load them into the warm worker.

    Runs in the background: this is the path a user waits out while a 3 GB model
    comes down, and the daemon has to keep answering the hot key throughout.
    The reply carries what happened, the same shape :func:`download_model`
    returns; the worker warm-up is reported separately in ``warming`` because it
    is the second half of the job and the second half can fail on its own.

    ``on_warming`` is called once the weights are down and the model is going
    into memory.  It exists because that second phase can take as long as the
    first one on a slow disk, and a window that is told only that "the download"
    is still running would sit at 100% with nothing happening.

    ``download=False`` warms the worker without fetching anything.  That is the
    daemon's startup path on purpose: reading a model the user already has is
    free, while fetching three gigabytes the moment WayVoice starts is a decision
    nobody asked for - on a tethered laptop that is somebody else's bandwidth.

    Engines with nothing to fetch are reported as ``ready`` without doing
    anything, which is what lets the daemon call this unconditionally.
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
        # protocol and would be asked to warm a model that it cannot hold.
        return reply
    if on_warming is not None:
        try:
            on_warming()
        except Exception:
            # Telling the caller about a phase must never break that phase.
            pass
    reply["warming"] = warm_worker(cfg)
    return reply


def warm_worker(cfg: dict[str, Any], timeout: float = WORKER_START_TIMEOUT) -> bool:
    """Make the warm worker hold the model, starting it if needed.

    Returns whether the model is in memory afterwards.  A worker that cannot be
    started is not an error here: recognition falls back to the one-shot runner,
    which loads the model itself and still works.
    """
    if not ensure_worker(cfg):
        return False
    try:
        reply = _worker_call({"cmd": "warm"}, timeout=max(timeout, 30.0))
    except (OSError, ValueError, TimeoutError):
        return False
    return bool(reply.get("ok") and reply.get("warm"))


def request_engine_setup(engine: Engine | None) -> bool:
    """Ask for the runtime of ``engine`` to be prepared in the background.

    Returns ``False`` when that engine needs no preparation at all, so a
    caller can tell the user instead of doing nothing quietly.
    """
    if engine is None or not engine.needs_setup or engine.setup is None:
        return False
    engine.setup()
    return True
