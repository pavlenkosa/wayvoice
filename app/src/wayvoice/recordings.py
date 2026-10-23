"""Private recording sessions; only unlocked, abandoned sessions may be swept.

The process retains its session lock through recorder-to-transcription handoff.
No exit hook releases it early: kernel process teardown releases the descriptor.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time


def recording_root() -> Path:
    return Path(tempfile.gettempdir()) / f"wayvoice-recordings-{os.getuid()}"


def _private_directory(fd: int) -> bool:
    info = os.fstat(fd)
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700)


def _open_directory(path, *, dir_fd=None) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                 dir_fd=dir_fd)
    if not _private_directory(fd):
        os.close(fd)
        raise PermissionError("Recording directory must be owned by this user with mode 0700")
    return fd


class _RecordingSession:
    def __init__(self, root: Path):
        root.mkdir(mode=0o700, exist_ok=True)
        root_fd = _open_directory(root)
        self.owner_fd = None
        self.directory_fd = None
        try:
            # A sweep must not mistake the interval between marker creation
            # and locking it for an abandoned session.
            fcntl.flock(root_fd, fcntl.LOCK_SH)
            name = "session-" + secrets.token_hex(16)
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
            self.path = root / name
            self.directory_fd = _open_directory(name, dir_fd=root_fd)
            self.owner_fd = os.open(".owner", os.O_CREAT | os.O_EXCL | os.O_RDWR
                                    | os.O_NOFOLLOW, 0o600,
                                    dir_fd=self.directory_fd)
            fcntl.flock(self.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(root_fd)

    def create(self) -> Path:
        # Anchoring creation to the open session also avoids following a renamed
        # or substituted directory when allocating a recording.
        name = "take-" + secrets.token_hex(16) + ".wav"
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                     0o600, dir_fd=self.directory_fd)
        os.close(fd)
        return self.path / name

    def close(self) -> None:
        """Release a session only after all its users have stopped (tests)."""
        for name in ("owner_fd", "directory_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


_sessions = {}
_session_lock = threading.Lock()


def create_recording() -> Path:
    """Allocate a 0600 take in this process's lifetime-locked private session."""
    root = recording_root()
    with _session_lock:
        key = (os.getpid(), root)
        if key not in _sessions:
            _sessions[key] = _RecordingSession(root)
        return _sessions[key].create()


def _sweep_session(root_fd: int, name: str, cutoff: float) -> int:
    session_fd = owner_fd = None
    removed = 0
    try:
        session_fd = _open_directory(name, dir_fd=root_fd)
        # Never create a missing marker: a private directory alone does not prove
        # it belongs to our recording-session protocol.
        owner_fd = os.open(".owner", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                           dir_fd=session_fd)
        owner = os.fstat(owner_fd)
        if (not stat.S_ISREG(owner.st_mode) or owner.st_uid != os.getuid()
                or stat.S_IMODE(owner.st_mode) != 0o600 or owner.st_nlink != 1):
            return 0
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for take in os.listdir(session_fd):
            if not take.startswith("take-") or not take.endswith(".wav"):
                continue
            try:
                info = os.stat(take, dir_fd=session_fd, follow_symlinks=False)
                if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and info.st_nlink == 1 and info.st_mtime < cutoff):
                    os.unlink(take, dir_fd=session_fd)
                    removed += 1
            except OSError:
                continue
        if os.listdir(session_fd) == [".owner"]:
            os.unlink(".owner", dir_fd=session_fd)
            os.rmdir(name, dir_fd=root_fd)
    except OSError:
        # Locked live owner, symlink, foreign ownership or concurrent cleanup:
        # leave everything untouched when ownership cannot be established.
        pass
    finally:
        if owner_fd is not None:
            os.close(owner_fd)
        if session_fd is not None:
            os.close(session_fd)
    return removed


def sweep_stale_recordings(max_age: float = 3600.0) -> int:
    """Remove old WAVs only from verified sessions whose owner lock is free.

    Legacy files in the shared temporary directory have no ownership marker and
    are intentionally excluded, regardless of filename or age.
    """
    root_fd = None
    try:
        root_fd = _open_directory(recording_root())
        fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cutoff = time.time() - max_age
        return sum(_sweep_session(root_fd, name, cutoff)
                   for name in os.listdir(root_fd) if name.startswith("session-"))
    except OSError:
        return 0
    finally:
        if root_fd is not None:
            os.close(root_fd)
