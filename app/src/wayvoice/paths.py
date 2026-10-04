"""Installation-layout helpers.

The project ships as a Debian package, with the sources in
``<prefix>/lib/wayvoice/app/src/wayvoice`` and the helpers in ``<prefix>/bin``,
and as a plain checkout, with the sources in ``<repo>/app/src/wayvoice`` and the
helpers in ``<repo>/scripts``. Everything here is derived from the location of this
module, so any prefix works and no absolute path is hardcoded.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_dir() -> Path:
    """Root of the ``app`` directory holding the sources: ``<root>/app`` in a
    checkout, ``<prefix>/lib/wayvoice/app`` in the Debian package."""
    return Path(__file__).resolve().parents[2]


def app_src_dir() -> Path:
    """Return the import root for the ``wayvoice`` package (``.../src``)."""
    return app_dir() / "src"


def bundled_dir(name: str) -> Path:
    """Where a program bundled with the application is installed.

    ``<root>/<name>`` in a checkout, ``<prefix>/lib/wayvoice/<name>`` in the Debian
    package. A missing directory is the normal case - a checkout has no bundled
    programs unless it was built, and a package built without a compiler has none - so
    callers have to look before they assume.
    """
    return app_dir().parent / name


def package_dir() -> Path:
    """Return the directory containing the ``wayvoice`` package modules."""
    return app_src_dir() / "wayvoice"


def script_path(name: str) -> Path:
    """Absolute path of a helper script shipped inside the package, such as
    ``fw_runner.py``, which runs on the engine's interpreter rather than on the system
    Python."""
    return package_dir() / name


def setup_user_script() -> Path | None:
    """The ``setup-user`` helper script, or ``None`` when not found.

    Probed in order: next to the ``app`` directory, ``<repo>/scripts/setup-user`` in a
    checkout, and ``PATH``.
    """
    root = app_dir().parent
    for candidate in (root / "setup-user", root / "scripts" / "setup-user"):
        if candidate.is_file():
            return candidate
    found = shutil.which("setup-user")
    return Path(found) if found else None


def _is_executable_file(path: Path) -> bool:
    """Return ``True`` when ``path`` is an existing regular executable file."""
    return path.is_file() and os.access(path, os.X_OK)


def command_path(name: str) -> str:
    """Resolve a WayVoice helper command to an absolute path.

    For commands launched without a shell and without a controlled ``PATH`` (GSettings
    keybindings, desktop entries), where a bare name is not good enough. Order:
    ``WAYVOICE_BINDIR``, ``PATH``, the directory of the running script, then the bare
    name.
    """
    bindir = os.environ.get("WAYVOICE_BINDIR")
    if bindir:
        candidate = Path(bindir) / name
        if _is_executable_file(candidate):
            return str(candidate)

    found = shutil.which(name)
    if found:
        return found

    try:
        sibling = Path(sys.argv[0]).resolve().parent / name
    except (OSError, ValueError):
        sibling = None
    if sibling is not None and _is_executable_file(sibling):
        return str(sibling)

    return name


def python_executable() -> str:
    """The interpreter to use for bundled Python code: ``WAYVOICE_PYTHON``, then
    ``python3`` from ``PATH``, then the interpreter running right now."""
    override = os.environ.get("WAYVOICE_PYTHON")
    if override:
        return override
    found = shutil.which("python3")
    if found:
        return found
    return sys.executable
