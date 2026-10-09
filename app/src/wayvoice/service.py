"""Single place that decides how this system starts the WayVoice parts.

The Debian package drives everything through ``systemctl --user``. A Flatpak build
has no ``systemctl`` and the host user manager cannot see ``/app``, so every one of
those calls fails and the daemon, the engine preparation and the shortcut are never
applied. Both shapes end up behind one API: with a user manager, keep using the
units; without one, do the same work directly.

Only the standard library is used, nothing runs through a shell, and every entry
point is best effort - the callers (the GTK window, the daemon, the CLI) all swallow
failures.

:mod:`wayvoice.cli` is imported lazily inside the functions that need it: it imports
:mod:`wayvoice.engine`, which imports this module.
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

# Sign of a running user manager: ``/run/systemd/user`` on older systemd, the
# manager's control socket under the runtime directory on current ones (257 on
# Debian 13). Either means "systemctl --user works"; both are filesystem probes.
SYSTEMD_USER_RUNTIME = "/run/systemd/user"

DAEMON_UNIT = "wayvoice.service"
ENGINE_SETUP_UNIT = "wayvoice-engine-setup.service"

DAEMON_MODULE = "wayvoice.daemon"
ENGINE_SETUP_MODULE = "wayvoice.engine_setup"

# How often the daemon socket is polled while waiting for the daemon to come up.
POLL_INTERVAL = 0.1
# Grace period for a "quit" to actually make the daemon leave.
QUIT_TIMEOUT = 3.0

# Serialises start/restart. The window asks for the daemon from its startup path
# and again after installing a dependency, each in its own thread, and without this
# both could see "no daemon answering" and each spawn one. The daemon refuses to
# steal a live socket; this keeps two processes from being spawned at all.
#
# Reentrant because restart_daemon() holds it across "ask the old daemon to leave,
# wait, spawn the new one" and then calls start_daemon().
_start_lock = RLock()


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))


def service_log_path() -> Path:
    """Log file the directly spawned processes write into, next to
    ``engine-setup.log`` in the state directory."""
    return _state_home() / "wayvoice" / "daemon.log"


def _user_manager_socket() -> Path:
    """Return the control socket of the user manager, which may not exist."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "systemd" / "private"


def systemd_available() -> bool:
    """Return whether the user units can be used on this system.

    Two filesystem probes, nothing started: ``systemctl`` on ``PATH`` (absent in the
    Flatpak sandbox) and a user manager that is actually running. A container image that
    ships ``systemctl`` without systemd has neither, and every call in it would fail
    with "Failed to connect to bus".

    The wrong answer is not cosmetic either: where a user manager exists the units own
    the daemon's lifetime, and a second, unmanaged daemon would be worse than the
    failure.
    """
    if shutil.which("systemctl") is None:
        return False
    return os.path.isdir(SYSTEMD_USER_RUNTIME) or _user_manager_socket().exists()


def _request(command: str, timeout: float = 1.0) -> dict:
    """Send one command to the daemon through the regular CLI transport."""
    from .cli import request

    return request(command, timeout=timeout)


def daemon_socket_alive(timeout: float = 0.3) -> bool:
    """Return whether the daemon answers on its socket right now."""
    try:
        return bool(_request("ping", timeout=timeout).get("ok"))
    except Exception:
        return False


def _child_env() -> dict[str, str]:
    """Environment for a directly spawned WayVoice process.

    The import root goes on ``PYTHONPATH`` so ``python -m wayvoice.daemon`` resolves
    the same sources this process was started from, in a checkout and in the
    ``<prefix>/lib/...`` layout alike, where the package is not in ``site-packages``.
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


def start_user_unit(unit: str) -> bool:
    """Start a packaged user unit without blocking; ``True`` when launched.

    Used for the ydotoold unit: enabling it at installation time does nothing for a
    session that was already open when the package arrived.
    """
    if not systemd_available():
        return False
    return _systemctl("start", unit)


def _systemd_daemon_ready(action: str, wait: float) -> bool:
    """Command completion precedes ping, so restart cannot accept the old daemon."""
    deadline = time.monotonic() + max(0.0, wait)
    cmd = ["systemctl", "--user", action, DAEMON_UNIT]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=max(0.001, deadline - time.monotonic()),
                                check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"WayVoice: daemon {action} failed: {exc}", file=sys.stderr)
        return False
    if result.returncode:
        print(f"WayVoice: daemon {action} failed ({result.returncode}): "
              f"{(result.stderr or result.stdout or '').strip()}", file=sys.stderr)
        return False
    return _wait_for_daemon(max(0.0, deadline - time.monotonic()))


def _wait_for_daemon(wait: float) -> bool:
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if daemon_socket_alive(timeout=min(0.3, max(0.001, deadline - time.monotonic()))):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.monotonic())))


def start_daemon(wait: float = 5.0) -> bool:
    """Make sure a daemon is running, and report whether one really is.

    With a user manager, ``systemctl --user start wayvoice.service``; without one, a
    direct spawn and a poll of its socket. A daemon that already answers is never
    killed and never duplicated.
    """
    try:
        if systemd_available():
            return _systemd_daemon_ready("start", wait)
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

    pids get recycled, so the command line is verified before anything is signalled,
    and only the exact ``-m wayvoice.daemon`` form counts - the module name alone would
    also match an interactive run of the same module.
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

    SIGTERM is the expected path: the daemon handles it by asking its own loop to stop,
    which is what stops the recorder and removes the socket. SIGKILL is the escalation
    for a daemon wedged past its handler, and a recorder that outlives it is the cost of
    running it in a session of its own.
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

    A directly spawned daemon is asked to leave through its own ``quit`` so it can close
    the socket and the recording timer; only if it stays is it signalled by pid.

    The sequence holds ``_start_lock``, because the window also starts a daemon from a
    background thread: without it that spawn could bind the socket after this one has
    asked the old daemon to quit, leaving no daemon at all and a failure that did not
    happen.
    """
    with _start_lock:
        try:
            if systemd_available():
                return _systemd_daemon_ready("restart", wait)
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

    Soft by design: the engine setup is a long background job, and every caller treats
    "not started" as "the engine stays unprepared" and says so.
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

    ``setup-user`` does this on a packaged system; without a user manager it is the only
    thing that can be done here. Returns ``(True, "")`` when a user manager is present,
    since there the packaged integration owns the shortcut.
    """
    try:
        if systemd_available():
            return True, ""
        cfg = load_config()
        return apply_shortcut(str(cfg.get("shortcut", "F8")), cfg.get("ui_language"))
    except Exception as exc:
        print(f"WayVoice: could not apply the global shortcut: {exc}", file=sys.stderr)
        return False, str(exc)
