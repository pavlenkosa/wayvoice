"""Ownership, cancellation and cleanup of one-shot subprocess groups."""

from __future__ import annotations

import codecs
import io
import locale
import os
import selectors
import signal
import subprocess
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


class _PipeReaders:
    """Drain owned descriptors without a blocking buffered read or helper thread."""

    def __init__(self, proc, overflow):
        self.selector = selectors.DefaultSelector()
        self.out: list[str] = []
        self.err: list[str] = []
        self.overflow = overflow
        self.retained = 0
        try:
            for stream, sink in ((proc.stdout, self.out), (proc.stderr, self.err)):
                if stream is None:
                    continue
                try:
                    descriptor = stream.fileno()
                    os.set_blocking(descriptor, False)
                except (AttributeError, OSError, TypeError, ValueError):
                    # In-memory streams have no OS descriptor to drain.
                    continue
                # Diagnostics may contain arbitrary tool output; preserve readable
                # warnings without discarding a valid transcript. Transcript bytes
                # remain strict so malformed recognition text is reported explicitly.
                errors = "replace" if sink is self.err else "strict"
                decoder = io.IncrementalNewlineDecoder(
                    codecs.getincrementaldecoder(stream.encoding or locale.getencoding())(errors=errors),
                    translate=True,
                )
                self.selector.register(descriptor, selectors.EVENT_READ, (sink, decoder))
        except BaseException:
            self.close()
            raise

    def _retain(self, sink, chunk):
        if sink is self.err:
            sink[:] = [("".join(sink) + chunk)[-PROCESS_ERROR_TAIL:]]
        else:
            available = TRANSCRIPT_OUTPUT_LIMIT - self.retained
            if available > 0:
                sink.append(chunk[:available])
                self.retained += min(len(chunk), available)
            if len(chunk) > available and self.overflow is not None:
                self.overflow.set()

    def drain(self, timeout=0.0):
        if not self.selector.get_map():
            if timeout:
                time.sleep(timeout)
            return False
        ready = self.selector.select(timeout)
        for key, _ in ready:
            sink, decoder = key.data
            try:
                chunk = os.read(key.fd, 8192)
            except BlockingIOError:
                continue
            if not chunk:
                self.selector.unregister(key.fd)
            self._retain(sink, decoder.decode(chunk, final=not chunk))
        return bool(ready)

    def finish(self):
        # A detached descendant can retain or continuously write these pipes.
        # The leader has exited; consume its buffered output without waiting for
        # another process to close its copies or extending cleanup indefinitely.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and self.drain():
            pass

    def close(self):
        self.selector.close()


def _start_readers(proc: subprocess.Popen[str], overflow: Event | None = None):
    """Create nonblocking pipe ownership and return the transcript buffers."""
    readers = _PipeReaders(proc, overflow)
    return readers, readers.out, readers.err


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


def _finish(proc: subprocess.Popen[str], readers: _PipeReaders, error: Exception) -> Exception:
    """Stop an owned group without waiting for inherited pipe descriptors."""
    _terminate_process(proc)
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
    readers = None
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
            readers.drain(timeout=0.12)
        _terminate_process(proc)
        readers.finish()
        if overflow.is_set():
            raise RuntimeError("Recognition output exceeds text limit")
        return subprocess.CompletedProcess(
            args, proc.returncode, "".join(out), "".join(err)
        )
    finally:
        try:
            if readers is not None:
                readers.close()
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
