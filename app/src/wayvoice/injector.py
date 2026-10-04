from __future__ import annotations
import atexit
import os
import shutil
import socket
import subprocess
import sys
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


class InjectionError(RuntimeError):
    pass


@dataclass
class InjectionResult:
    pasted: bool
    warning: str = ""


_clipboard_lock = threading.Lock()
_clipboard_proc: subprocess.Popen | None = None

#: How long wl-copy gets to claim the Wayland selection before we assume it made
#: it. A deadline rather than a sleep, so a wedged clipboard tool cannot exceed it.
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
    """Own the Wayland clipboard without blocking on wl-copy's background server.

    wl-copy forks after acquiring the selection, and waiting on it with a captured
    stderr pipe can hang because the background child keeps that pipe open. Running it
    in the foreground and keeping the process alive avoids that, and leaves the
    clipboard usable for repeated pastes until another owner replaces it.

    ``language`` is the interface language of the error messages, taken from the
    configuration so that a notification matches the UI.
    """
    global _clipboard_proc

    binary = shutil.which("wl-copy")
    if not binary:
        raise InjectionError(describe_missing("wl-clipboard"))

    with _clipboard_lock:
        _terminate_clipboard()

        proc = subprocess.Popen(
            [binary, "--foreground", "--type", "text/plain;charset=utf-8"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=False,
        )
        # Remember the process before doing anything that can fail. wl-copy only
        # forks into its clipboard server after the selection is claimed, so a
        # process we lose track of keeps owning the Wayland clipboard.
        _clipboard_proc = proc
        try:
            assert proc.stdin is not None
            proc.stdin.write(text)
            proc.stdin.close()
        except Exception as exc:
            _terminate_clipboard()
            raise InjectionError(tr("injector.clipboard_error", language, error=exc)) from exc

        # Wait for the selection with a deadline. The daemon serves one client at
        # a time, so this wait is part of the dictation's latency, and an unbounded
        # poll() hands the daemon to a clipboard tool that stopped making progress.
        deadline = time.monotonic() + CLIPBOARD_SETTLE_TIMEOUT
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)

        rc = proc.poll()
        if rc is None:
            # Still running, which is the good case: it owns the selection.
            return
        _clipboard_proc = None
        detail = ""
        try:
            if proc.stderr is not None:
                detail = proc.stderr.read().strip()
        except Exception:
            pass
        raise InjectionError(detail or tr("injector.clipboard_write", language))


def _terminate_clipboard() -> None:
    """Stop the process that currently owns our clipboard entry, if any."""
    global _clipboard_proc

    proc = _clipboard_proc
    _clipboard_proc = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=0.25)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=0.5)
        except Exception:
            pass


def ydotool_command() -> str | None:
    """The ydotool client to run, or ``None`` when there is none anywhere.

    A distribution's own copy wins over the bundled one: it is the copy that gets
    security updates.
    """
    return find_command("ydotool")


def ydotool_socket() -> Path | None:
    """Where the helper's socket is, or ``None`` when there is none anywhere.

    A missing socket and a socket nobody answers on need different things - a start
    and a restart - and look the same until you connect.
    """
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    custom = runtime / "wayvoice-ydotool.sock"
    if custom.exists():
        return custom
    # ydotool 0.1.8 hardcodes this path instead of honouring XDG_RUNTIME_DIR, so
    # the literal is a fallback for that version and not an installation prefix.
    legacy = Path(LEGACY_SOCKET)
    if legacy.exists():
        return legacy
    return None


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

    env = _ydotool_env()
    if mode == "terminal":
        seq = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
    else:
        seq = ["29:1", "47:1", "47:0", "29:0"]

    # Raise the helper before typing into somebody's document. Typing into the void
    # fails with a message about a socket, which says nothing about the one command
    # that would have fixed it.
    started = ensure_helper_running()

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
            # Naming the difference between "ydotool is broken" and "ydotool is
            # not running" is the difference between a bug report and a fix.
            message = tr(
                "injector.ydotool_helper_down", language, reason=short,
            )
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
