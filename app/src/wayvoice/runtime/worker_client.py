"""Persistent worker lifetime, addressed transport and cancellation ownership."""

from __future__ import annotations

from . import locations

import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import time
import uuid
from pathlib import Path
from threading import Event, Lock
from typing import Any

from .. import __version__
from ..config import number
from ..paths import script_path
from .errors import TranscriptionCancelled, TranscriptionTimeout, WorkerUnavailable, WorkerBusyError

WORKER_START_TIMEOUT = 20.0
WORKER_POLL_INTERVAL = 0.15
WORKER_PING_TIMEOUT = 1.5
WARM_TIMEOUT = 900.0
WORKER_CANCEL_GRACE = 3.0
WORKER_RETRY_BACKOFF = 60.0
_job_lock = Lock()
_worker_jobs: dict[Event, int] = {}
_unresolved_worker_jobs: dict[Event | None, set[tuple[int, str]]] = {}
_worker_lock = Lock()
_worker_retry_after = 0.0
_worker_procs: list[subprocess.Popen[str]] = []


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


def _worker_call(payload: dict[str, Any], timeout: float, path: Path | None = None) -> dict[str, Any]:
    """Send one request to the worker and return its reply.

    Wire format as in :mod:`wayvoice.protocol`: one JSON line in, one out.
    """
    target = path or locations.worker_socket_path()
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
    target = path or locations.worker_socket_path()
    if not target.exists():
        return None
    try:
        reply = _worker_call({"cmd": "ping"}, WORKER_PING_TIMEOUT, target)
    except (OSError, ValueError, TimeoutError):
        return None
    _raise_if_busy(reply)
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
        pid = int(locations.worker_pid_path().read_text(encoding="utf-8").split()[0])
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
            locations.worker_pid_path().unlink()
        elif not _pid_alive(pid):
            # Another operation may already have published a replacement.
            current_pid = int(locations.worker_pid_path().read_text().split()[0])
            if current_pid == pid:
                locations.worker_pid_path().unlink()
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
            locations.worker_socket_path().unlink(missing_ok=True)
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
    runtime_python = locations.faster_runtime() / "bin/python"
    if not runtime_python.exists():
        return False
    args = [
        str(runtime_python), str(script_path("fw_runner.py")),
        "--serve",
        "--socket", str(locations.worker_socket_path()),
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
        log_path = locations.worker_log_path()
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
        locations.worker_pid_path().write_text(f"{proc.pid}\n", encoding="utf-8")
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
    path = locations.worker_socket_path()
    with _worker_guard(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise TranscriptionCancelled("Transcription cancelled")
        _retry_unresolved_workers()
        _reap_workers()
        reply = _worker_ping(path)
        if reply is not None:
            _raise_if_busy(reply)
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
                _raise_if_busy(reply)
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
                sock.connect(str(locations.worker_socket_path()))
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
            _raise_if_busy(health)
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
        _raise_if_busy(reply)
        if not isinstance(reply, dict) or reply.get("request_id") != request_id:
            raise WorkerUnavailable("malformed worker reply or mismatched request id")
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
        current_worker = any(owner in _worker_jobs for owner in events)
        unresolved = {identity for owner, identities in _unresolved_worker_jobs.items()
                      if any(owner is event for event in events) for identity in identities}
        active_worker = current_worker or (active_worker and not unresolved)
    for identity in unresolved:
        grace = 0.5 if deadline is None else max(0.0, (deadline - time.monotonic()) / 2)
        stop_worker(timeout=grace, expected_identity=identity)
        if _worker_identity(identity[0]) != identity or not _pid_alive(identity[0]):
            _forget_unresolved_worker(identity)
    if active_worker:
        grace = 2.0 if deadline is None else max(0.0, (deadline - time.monotonic()) / 2)
        stop_worker(timeout=grace)


def retry_pending(cancel_event):
    """Refuse duplicate inference while an abandoned native job still owns work."""
    with _job_lock:
        unresolved = bool(_unresolved_worker_jobs)
    if unresolved:
        with _worker_guard(cancel_event):
            if cancel_event is not None and cancel_event.is_set():
                raise TranscriptionCancelled("Transcription cancelled")
            _retry_unresolved_workers()


def _raise_if_busy(reply):
    if isinstance(reply, dict) and reply.get("busy") is True:
        raise WorkerBusyError(str(reply.get("error") or "Faster-Whisper worker is busy"))
