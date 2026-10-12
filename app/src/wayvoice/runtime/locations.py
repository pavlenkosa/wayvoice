"""Filesystem locations shared by runtime owners and the public engine facade."""

from __future__ import annotations

import os
from pathlib import Path

from .. import fw_worker
from ..paths import app_dir

RUNTIME_STAMP = ".engine-v1-ready"


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


def worker_pid_path() -> Path:
    """Return the file holding the pid of the running worker."""
    return _state_home() / "wayvoice" / "engine-worker.pid"


def worker_log_path() -> Path:
    """Return the log file the worker writes stdout/stderr into."""
    return _state_home() / "wayvoice" / "engine-worker.log"
