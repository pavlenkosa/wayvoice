from __future__ import annotations
import os
from pathlib import Path

def runtime_dir() -> Path:
    """Per-user runtime directory, the home of everything session-scoped."""
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def socket_path() -> Path:
    return runtime_dir() / "wayvoice.sock"


def owner_lock_path() -> Path:
    """Lock file that says which process owns the daemon socket.

    The socket itself cannot answer that question reliably: a daemon that is
    alive but wedged does not reply, and a socket file exists from the moment
    ``bind()`` returns - half a second before the daemon can serve anything.  A
    lock the running daemon holds for its whole life has neither problem, so it
    is what a second instance checks first.
    """
    return runtime_dir() / "wayvoice.owner.lock"
