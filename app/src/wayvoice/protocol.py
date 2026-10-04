from __future__ import annotations
import os
from pathlib import Path

def runtime_dir() -> Path:
    """Per-user runtime directory, the home of everything session-scoped."""
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def socket_path() -> Path:
    return runtime_dir() / "wayvoice.sock"


def owner_lock_path() -> Path:
    """Lock file naming the process that owns the daemon socket.

    The socket cannot answer that: a daemon that is wedged does not reply, and the
    socket file exists from the moment ``bind()`` returns. A lock held for the daemon's
    whole life has neither problem, so a second instance checks it first.
    """
    return runtime_dir() / "wayvoice.owner.lock"
