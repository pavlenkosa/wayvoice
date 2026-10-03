"""Single place that decides how this system starts the WayVoice parts.

The packaged (Debian) installation drives everything through ``systemctl
--user``: three user units plus ``setup-user``.  A Flatpak build has no
``systemctl`` at all, and the host user manager cannot see ``/app`` either, so
every one of those calls fails silently and the daemon, the engine preparation
and the global shortcut are never applied.

This module hides that difference behind one API:

* a user manager is available -> keep using the units, exactly as before;
* it is not -> do the same work directly (spawn the daemon, run the engine
  setup, apply the shortcut through GSettings).

Only the standard library is used, nothing is ever run through a shell, and
every entry point is best effort: the callers (the GTK window, the daemon, the
CLI) all swallow failures, so a failure here must never be worse than one
there.

Import graph: this module imports only :mod:`wayvoice.config`,
:mod:`wayvoice.paths`, :mod:`wayvoice.protocol` and :mod:`wayvoice.shortcut`.
The daemon request (:func:`wayvoice.cli.request`) is imported lazily inside the
functions that need it, because :mod:`wayvoice.cli` imports
:mod:`wayvoice.engine`, which imports *this* module -- a top-level import would
close the cycle.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import RLock

from .config import load_config
from .paths import app_src_dir, python_executable
from .shortcut import apply_shortcut

# Sign of a running user manager.  ``/run/systemd/user`` is the historical
# marker and is still created by older systemd releases; current ones (257 on
# Debian 13, for one) only expose the user manager's control socket under the
# runtime directory.  Either one means "systemctl --user works", both are pure
# filesystem probes.
SYSTEMD_USER_RUNTIME = "/run/systemd/user"

DAEMON_UNIT = "wayvoice.service"
ENGINE_SETUP_UNIT = "wayvoice-engine-setup.service"

DAEMON_MODULE = "wayvoice.daemon"
ENGINE_SETUP_MODULE = "wayvoice.engine_setup"

# How often the daemon socket is polled while waiting for the daemon to come up.
POLL_INTERVAL = 0.1
# Grace period for a "quit" to actually make the daemon leave.
QUIT_TIMEOUT = 3.0

# Serialises start/restart. The window asks for the daemon from its startup
# path and again after installing a dependency, and both run in their own
# thread; without this both could see "no daemon answering" and each spawn one.
# The daemon itself refuses to steal a live socket, this lock keeps two
# processes from being spawned in the first place.
#
# Reentrant because restart_daemon() holds it across "ask the old daemon to
# leave, wait for it, spawn the new one" and then calls start_daemon() - the
# public entry point, so the tests and any caller keep working - which would
# deadlock on a plain lock.
_start_lock = RLock()


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def service_log_path() -> Path:
    """Return the log file the directly spawned processes write into.

    Lives next to ``engine-setup.log`` in the state directory, which is the
    place the rest of the project already uses for its run-time state.
    """
    return _state_home() / "wayvoice" / "daemon.log"


def _user_manager_socket() -> Path:
    """Return the control socket of the user manager, which may not exist."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "systemd" / "private"


def systemd_available() -> bool:
    """Return whether the user units can be used on this system.

    Both conditions are required and both are pure filesystem probes -- nothing
    is started for the check:

    * ``systemctl`` on ``PATH``.  Inside the Flatpak sandbox (org.gnome.Platform)
      the binary does not exist at all;
    * a user manager that is actually running, i.e. either the historical
      ``/run/systemd/user`` directory or its control socket in
      ``$XDG_RUNTIME_DIR/systemd/private``.  A container image that ships
      ``systemctl`` without systemd has neither, and every ``systemctl --user``
      call in it would fail with "Failed to connect to bus".

    This matters because the wrong answer is not a cosmetic one: on a desktop
    that does have a user manager the units own the daemon's lifetime (and the
    ``graphical-session.target`` pairing), so falling back to spawning a daemon
    by hand there would only create a second, unmanaged copy.
    """
    if shutil.which("systemctl") is None:
        return False
    return os.path.isdir(SYSTEMD_USER_RUNTIME) or _user_manager_socket().exists()


def _request(command: str, timeout: float = 1.0) -> dict:
    """Send one command to the daemon through the regular CLI transport.

    Imported lazily on purpose, see the module docstring.
    """
    from .cli import request

    return request(command, timeout=timeout)


def daemon_socket_alive(timeout: float = 0.3) -> bool:
    """Return whether the daemon answers on its socket right now."""
    try:
        return bool(_request("ping", timeout=timeout).get("ok"))
    except Exception:
        return False


def _child_env() -> dict[str, str]:
    """Return the environment for a directly spawned WayVoice process.

    The import root of the package is put on ``PYTHONPATH`` so that
    ``python -m wayvoice.daemon`` resolves the same sources this process was
    started from, in a source checkout as well as in the ``<prefix>/lib/...``
    layout, where the package is not installed into ``site-packages``.
    """
    env = os.environ.copy()
    root = str(app_src_dir())
    existing = env.get("PYTHONPATH", "")
    if root not in existing.split(os.pathsep):
        env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    return env


def _spawn(module: str) -> None:
    """Run ``python -m <module>`` detached, with output in the service log."""
    args = [python_executable(), "-m", module]
    log = None
    try:
        path = service_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        log = path.open("a", encoding="utf-8")
    except OSError:
        log = None
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log if log is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env=_child_env(),
            start_new_session=True,
        )
    finally:
        if log is not None:
            log.close()


def _systemctl(*args: str) -> bool:
    """Start a user unit without blocking; ``True`` when it was launched."""
    cmd = ["systemctl", "--user", *args]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print(f"WayVoice: failed to start {' '.join(cmd)}: {exc}", file=sys.stderr)
        return False
    return True


def _wait_for_daemon(wait: float) -> bool:
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if daemon_socket_alive():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)


def start_daemon(wait: float = 5.0) -> bool:
    """Make sure a daemon is running, and report whether one really is.

    With a user manager this is the old ``systemctl --user start
    wayvoice.service``; without one the daemon is spawned directly and its
    socket is polled.  A daemon that is already answering is never killed and
    never duplicated -- the second process would fight over the same socket.
    """
    try:
        if systemd_available():
            return _systemctl("start", DAEMON_UNIT)
        with _start_lock:
            return _start_daemon_locked(wait=wait)
    except Exception as exc:
        print(f"WayVoice: could not start the daemon: {exc}", file=sys.stderr)
        return False


def _start_daemon_locked(wait: float = 5.0) -> bool:
    """Spawn and wait for the daemon; the caller already holds the lock."""
    if daemon_socket_alive():
        return True
    try:
        _spawn(DAEMON_MODULE)
    except Exception as exc:
        print(f"WayVoice: failed to start the daemon: {exc}", file=sys.stderr)
        return False
    return _wait_for_daemon(wait)


def _is_daemon_process(pid: int) -> bool:
    """Return whether ``pid`` is started as ``python -m wayvoice.daemon``.

    Same trick as ``engine._is_worker_process``: pids get recycled, so the
    command line is verified before anything is signalled.  Only the exact
    ``-m wayvoice.daemon`` form counts -- the module name alone would also match
    an interactive ``python -m wayvoice.daemon`` of the current user.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
    return len(argv) == 3 and argv[1] == "-m" and argv[2] == DAEMON_MODULE


def _daemon_pids() -> list[int]:
    """Return the pids of the running WayVoice daemons."""
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and _is_daemon_process(int(entry.name)):
            pids.append(int(entry.name))
    return pids


def _force_stop_daemon() -> bool:
    """Terminate a daemon that ignored ``quit``; best effort.

    The worker-stopping code in :mod:`wayvoice.engine` established the pattern
    (verify the cmdline in ``/proc``, then SIGTERM, wait, SIGKILL) and its
    liveness helper is reused here.
    """
    from .engine import _pid_alive

    stopped = False
    for pid in _daemon_pids():
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            stopped = True
            deadline = time.monotonic() + 2.0
            while _pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL)
            if not _pid_alive(pid):
                break
    return stopped


def restart_daemon(wait: float = 5.0) -> bool:
    """Replace the running daemon with a fresh one.

    Used when the daemon reports a version that does not match the window.  A
    directly spawned daemon is asked to leave through its own ``quit``
    command, so it can close the socket and the recording timer properly; only
    if it does not go away is it signalled by pid.

    The whole sequence holds ``_start_lock``.  The window also starts a daemon
    from a background thread, and without the lock the two could interleave:
    this one asks the daemon to quit, the other spawns a replacement that binds
    the socket, and then the quitting daemon leaves - after which there is no
    daemon at all, and this function reports a failure that did not happen.
    """
    with _start_lock:
        try:
            if systemd_available():
                return _systemctl("restart", DAEMON_UNIT)
            if daemon_socket_alive():
                try:
                    _request("quit", timeout=1.0)
                except Exception:
                    pass
                deadline = time.monotonic() + QUIT_TIMEOUT
                while time.monotonic() < deadline:
                    if not daemon_socket_alive(timeout=0.2):
                        break
                    time.sleep(POLL_INTERVAL)
                if daemon_socket_alive(timeout=0.2):
                    print(
                        "WayVoice: the daemon ignored the quit request; terminating it.",
                        file=sys.stderr,
                    )
                    _force_stop_daemon()
            return start_daemon(wait=wait)
        except Exception as exc:
            print(f"WayVoice: could not restart the daemon: {exc}", file=sys.stderr)
            return False


def request_engine_setup() -> None:
    """Ask for the Faster-Whisper runtime to be prepared, one way or another.

    Replaces the direct ``systemctl`` call the callers used before.  Soft by
    design: the engine setup is a long background job, and every caller treats
    "not started" as "the engine stays unprepared" and says so in the UI.
    """
    try:
        if systemd_available():
            _systemctl("--no-block", "start", ENGINE_SETUP_UNIT)
        else:
            _spawn(ENGINE_SETUP_MODULE)
    except Exception as exc:
        print(f"WayVoice: could not request the engine setup: {exc}", file=sys.stderr)


def apply_shortcut_now() -> tuple[bool, str]:
    """Apply the configured global shortcut directly.

    ``setup-user`` does this on a packaged system, together with enabling the
    ydotoold unit, which needs systemd.  Without a user manager the shortcut is
    all that can be done here, so it is applied directly through GSettings --
    exactly what ``wayvoice apply-shortcut`` does.

    Returns ``(True, "")`` when a user manager is present: there, the packaged
    integration owns the shortcut and this must not touch it.
    """
    try:
        if systemd_available():
            return True, ""
        cfg = load_config()
        return apply_shortcut(str(cfg.get("shortcut", "F8")))
    except Exception as exc:
        print(f"WayVoice: could not apply the global shortcut: {exc}", file=sys.stderr)
        return False, str(exc)
