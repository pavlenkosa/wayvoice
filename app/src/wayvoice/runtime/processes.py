"""Ownership, cancellation and cleanup of one-shot subprocess groups."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from threading import Event, Lock

from .errors import TranscriptionCancelled, TranscriptionTimeout

TRANSCRIPT_OUTPUT_LIMIT = 1024 * 1024
PROCESS_ERROR_TAIL = 64 * 1024
_job_lock = Lock()
_job_procs: dict[subprocess.Popen, Event | None] = {}


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


def _start_readers(
    proc: subprocess.Popen[str], overflow: Event | None = None,
) -> tuple[list[threading.Thread], list[str], list[str]]:
    """Begin draining the child's pipes at once and return the buffers.

    :func:`_run_cancelable` polls the child rather than talking to it, so nothing
    else drains the pipes; an engine that writes more than one buffer blocks on the
    write and never exits.
    """
    out: list[str] = []
    err: list[str] = []

    def read(stream, sink):
        retained = 0
        try:
            while chunk := stream.read(8192):
                if sink is err:
                    sink[:] = [("".join(sink) + chunk)[-PROCESS_ERROR_TAIL:]]
                else:
                    available = TRANSCRIPT_OUTPUT_LIMIT - retained
                    if available > 0:
                        sink.append(chunk[:available])
                        retained += min(len(chunk), available)
                    if len(chunk) > available and overflow is not None:
                        overflow.set()
        except Exception:
            pass

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
        overflow = Event()
        readers, out, err = _start_readers(proc, overflow)
        started = time.monotonic()
        while proc.poll() is None:
            if overflow.is_set():
                raise _finish(proc, readers, RuntimeError("Recognition output exceeds text limit"))
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
        if overflow.is_set():
            raise RuntimeError("Recognition output exceeds text limit")
        return subprocess.CompletedProcess(
            args, proc.returncode, "".join(out), "".join(err)
        )
    finally:
        _release_job(proc)


def stop_jobs(*events, deadline=None):
    """Stop process groups registered to the given cancellation owners."""
    with _job_lock:
        procs = [proc for proc, owner in _job_procs.items()
                 if any(owner is event for event in events)]
    for proc in procs:
        grace = 1.5 if deadline is None else min(1.5, max(0.0, (deadline - time.monotonic()) / 2))
        _terminate_process(proc, timeout=grace)
