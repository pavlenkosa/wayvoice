from __future__ import annotations
import atexit
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .deps import describe_missing, find_command
from .i18n import tr

#: The unit that owns /dev/uinput for us. Without it the ydotool client has
#: nothing to talk to, and the paste fails while looking like a WayVoice bug.
YDOTOOLD_UNIT = "wayvoice-ydotool.service"

#: The packaged helper is enabled at installation time, but a session that was
#: already running when the package arrived does not start a newly enabled unit
#: until the next login. One retry a minute covers that, without turning every
#: dictation into a systemctl call on a machine where the helper cannot run.
HELPER_RETRY_INTERVAL = 60.0

#: Where ydotool 0.1.8 puts its socket, having no honour for XDG_RUNTIME_DIR.
LEGACY_SOCKET = "/tmp/.ydotool_socket"

#: The name ydotoold itself uses inside XDG_RUNTIME_DIR - both the packaged unit
#: and a distribution's, which is why this has to be looked for as well. Checked
#: against the vendored sources: Daemon/ydotoold.c falls back to /tmp only when
#: XDG_RUNTIME_DIR is unset, and Client/ydotool.c reads YDG_RUNTIME_DIR.
DEFAULT_SOCKET_NAME = ".ydotool_socket"


class InjectionError(RuntimeError):
    pass


@dataclass
class InjectionResult:
    pasted: bool
    warning: str = ""


_clipboard_lock = threading.Lock()
_clipboard_proc: subprocess.Popen | None = None

#: Deadline for wl-copy to acknowledge selection ownership (not a fixed delay).
CLIPBOARD_SETTLE_TIMEOUT = 0.5


def _cleanup_clipboard() -> None:
    with _clipboard_lock:
        _terminate_clipboard()


atexit.register(_cleanup_clipboard)


def _run(cmd, *, env=None, timeout: float = 2.0,
         capture_stdout: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env=env,
        check=False,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def copy_to_clipboard(text: str, language: str | None = None) -> None:
    """Wait for wl-copy's selection handshake, retaining its background owner.

    Default-mode wl-copy exits its parent after acquiring the selection. Its child
    serves the text until the compositor replaces it, including after UI exit.
    Inherited stderr must not be a PIPE: the child would keep communicate waiting.
    """
    global _clipboard_proc

    binary = shutil.which("wl-copy")
    if not binary:
        raise InjectionError(describe_missing("wl-clipboard"))

    with _clipboard_lock, tempfile.TemporaryFile() as errors:
        _terminate_clipboard()
        proc = subprocess.Popen(
            [binary, "--type", "text/plain;charset=utf-8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            text=True,
            encoding="utf-8",
            start_new_session=True,
        )
        _clipboard_proc = proc
        try:
            proc.communicate(input=text, timeout=CLIPBOARD_SETTLE_TIMEOUT)
            if proc.returncode != 0:
                errors.seek(0)
                detail = errors.read(8192).decode("utf-8", errors="replace").strip()
                raise InjectionError(detail or tr("injector.clipboard_write", language))
        except Exception as exc:
            # A failed parent can already have forked. Retire the entire attempt,
            # even when that parent exited, before permitting another copy/paste.
            _terminate_clipboard()
            if isinstance(exc, InjectionError):
                raise
            raise InjectionError(tr("injector.clipboard_error", language, error=exc)) from exc
        # The successful child intentionally outlives this application. A later
        # selection cancels it through the compositor; atexit owns only attempts
        # that have not completed their handshake.
        _clipboard_proc = None


def _terminate_clipboard() -> None:
    """Retire every process belonging to an unsuccessful clipboard attempt."""
    global _clipboard_proc
    proc = _clipboard_proc
    _clipboard_proc = None
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        # The group may have disappeared already; still reap the parent below.
        pass
    try:
        proc.wait(timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass


def ydotool_command() -> str | None:
    """The ydotool client to run, or ``None`` when there is none anywhere.

    A distribution's own copy wins over the bundled one: it is the copy that gets
    security updates.
    """
    return find_command("ydotool")


def ydotool_socket() -> Path | None:
    """Prefer a connectable helper; retain a stale path only for diagnostics.

    Three places, in priority order among live endpoints:

    1. the socket the packaged unit creates, ``$XDG_RUNTIME_DIR/wayvoice-ydotool.sock``;
    2. ``$XDG_RUNTIME_DIR/.ydotool_socket`` - where a distribution's own ydotoold
       listens. Skipping this one is how a system that already runs ydotoold ended
       up with a second one started here, competing for the same ``/dev/uinput``;
    3. ``/tmp/.ydotool_socket``, which is the same path the daemon falls back to
       when ``XDG_RUNTIME_DIR`` is not set at all.

    A missing socket and a socket nobody answers on need different things - a start
    and a restart - and look the same until you connect.
    """
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    fallback = None
    for candidate in (runtime / "wayvoice-ydotool.sock", runtime / DEFAULT_SOCKET_NAME,
                      Path(LEGACY_SOCKET)):
        if candidate.exists():
            if fallback is None:
                fallback = candidate
            # Explicit paths avoid recursively invoking discovery from the probe.
            # Connecting sends no key events or datagrams.
            if helper_answering(candidate):
                return candidate
    return fallback


def helper_answering(path: Path | None = None, timeout: float = 0.2) -> bool:
    """Whether something is listening on the helper's socket right now.

    Existence cannot answer it: ydotoold is killed at logout and on restart without
    removing its socket, so a file that is there may be a name nobody answers to.
    """
    target = path if path is not None else ydotool_socket()
    if target is None:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.connect(str(target))
        return True
    except OSError:
        return False
    finally:
        sock.close()


#: When the helper was last asked to start, or ``None`` if it never was.
_helper_start_lock = threading.Lock()
_helper_started_at: float | None = None


def ensure_helper_running(wait: float = 2.0) -> bool:
    """Start the packaged ydotoold if it is not answering, and report the result.

    The unit is enabled at installation time, but a session that was already running
    when the package arrived never starts it, and until the next login every dictation
    is recognized and then not pasted.

    Being unable to start it is not an error by itself: a Flatpak sandbox has no user
    manager, a distribution that ships its own ydotoold already runs it, and the caller
    reports the paste failure either way.
    """
    global _helper_started_at
    if helper_answering():
        return True

    now = time.monotonic()
    with _helper_start_lock:
        last = _helper_started_at
        if last is not None and now - last < HELPER_RETRY_INTERVAL:
            # Somebody asked recently and it did not help. Asking again on every
            # dictation would only add a process spawn to a working dictation.
            return helper_answering()
        _helper_started_at = now

    from . import service

    if not service.start_user_unit(YDOTOOLD_UNIT):
        return False

    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if helper_answering():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _ydotool_env() -> dict[str, str]:
    env = os.environ.copy()
    target = ydotool_socket()
    if target is not None:
        env["YDOTOOL_SOCKET"] = str(target)
    return env


def paste_with_ydotool(mode: str, language: str | None = None) -> tuple[bool, str]:
    command = ydotool_command()
    if not command:
        detail = describe_missing("ydotool", language)
        return False, tr("injector.ydotool_missing", language, detail=detail)

    if mode == "terminal":
        seq = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
    else:
        seq = ["29:1", "47:1", "47:0", "29:0"]

    # Raise the helper before typing into somebody's document. Typing into the void
    # fails with a message about a socket, which says nothing about the one command
    # that would have fixed it.
    #
    # The environment is read *after* this, not before. YDOTOOL_SOCKET is only set
    # when a socket already exists, so building the environment first meant the very
    # first dictation after an install had no YDOTOOL_SOCKET at all - and the client
    # then went looking in $XDG_RUNTIME_DIR/.ydotool_socket, which is not where the
    # packaged unit listens. The symptom was "works from the second dictation on".
    started = ensure_helper_running()
    env = _ydotool_env()

    time.sleep(0.08)
    try:
        cp = _run([command, "key", *seq], env=env, timeout=1.2,
                  capture_stdout=True)
    except subprocess.TimeoutExpired:
        return False, tr("injector.ydotool_timeout", language)
    if cp.returncode != 0:
        # ydotool reports its failures on stdout, which used to go to /dev/null:
        # the user was told "ydotool exited with an error" and the one line naming
        # the cause was discarded before anyone could read it.
        detail = cp.stderr.strip() if cp.stderr else ""
        if not detail:
            detail = (cp.stdout or "").strip()
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        short = lines[-1] if lines else tr("injector.ydotool_failed_generic", language)
        message = tr("injector.ydotool_failed", language, reason=short)
        if not started:
            # The helper could not be raised, so the message names the one command
            # that fixes it. The reason stays in the log: it is ydotool's own English
            # and does not belong inside a translated sentence.
            message = tr("injector.ydotool_helper_down", language)
        # And to the service log, so that "it does not paste" is a line in
        # journalctl rather than something the user has to describe.
        print(f"WayVoice: auto-paste failed: {short}", file=sys.stderr)
        return False, message
    return True, ""


def inject(text: str, cfg: dict[str, Any]) -> InjectionResult:
    if not text:
        return InjectionResult(pasted=False)
    language = cfg.get("ui_language")
    copy_to_clipboard(text, language)
    mode = str(cfg.get("paste_mode", "standard"))
    if mode == "copy":
        return InjectionResult(pasted=False)
    pasted, warning = paste_with_ydotool(mode, language)
    return InjectionResult(pasted=pasted, warning=warning)
