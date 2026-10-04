from __future__ import annotations
import atexit
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .deps import describe_missing, find_command
from .i18n import tr


class InjectionError(RuntimeError):
    pass


@dataclass
class InjectionResult:
    pasted: bool
    warning: str = ""


_clipboard_lock = threading.Lock()
_clipboard_proc: subprocess.Popen | None = None

#: How long wl-copy gets to claim the Wayland selection before we assume it
#: made it.  60 ms was the old guess; this is the same ballpark, but as a
#: deadline that cannot be exceeded by a wedged clipboard tool.
CLIPBOARD_SETTLE_TIMEOUT = 0.5


def _cleanup_clipboard() -> None:
    with _clipboard_lock:
        _terminate_clipboard()


atexit.register(_cleanup_clipboard)


def _run(cmd, *, env=None, timeout: float = 2.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        env=env,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def copy_to_clipboard(text: str, language: str | None = None) -> None:
    """Own the Wayland clipboard without blocking on wl-copy's background server.

    wl-copy normally forks after acquiring the selection. Waiting on it with a
    captured stderr pipe can hang because the background child keeps that pipe
    open. Running it explicitly in foreground and keeping the process alive in
    the daemon avoids that race and keeps the clipboard available for repeated
    pastes until the next dictation/clipboard owner replaces it.

    ``language`` selects the interface language of the error messages; it comes
    from the user configuration so that the notification matches the UI.
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
        # Remember the process before doing anything that can fail.  wl-copy
        # only forks into its clipboard server after the selection is claimed,
        # so a process we lose track of keeps owning the Wayland clipboard
        # until the next dictation - or forever, if the next one never comes.
        _clipboard_proc = proc
        try:
            assert proc.stdin is not None
            proc.stdin.write(text)
            proc.stdin.close()
        except Exception as exc:
            _terminate_clipboard()
            raise InjectionError(tr("injector.clipboard_error", language, error=exc)) from exc

        # Wait for the selection with a deadline instead of guessing a sleep.
        # The daemon serves one client at a time, so this wait is added to the
        # latency of the dictation that triggered it, and an unbounded poll()
        # would hand the daemon over to a clipboard tool that stopped making
        # progress.
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

    A distribution's own copy wins over the one bundled with WayVoice, for the
    same reason the daemon does: it is the one that gets security updates, and a
    bundled fallback that took precedence would be a second, unmaintained copy
    of a program that talks to the kernel's input layer.
    """
    return find_command("ydotool")


def _ydotool_env() -> dict[str, str]:
    env = os.environ.copy()
    runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    custom = Path(runtime) / "wayvoice-ydotool.sock"
    if custom.exists():
        env["YDOTOOL_SOCKET"] = str(custom)
    elif "YDOTOOL_SOCKET" not in env and Path("/tmp/.ydotool_socket").exists():
        # ydotool 0.1.8 hardcodes its default socket path instead of honouring
        # XDG_RUNTIME_DIR, so this literal is an intentional fallback for that
        # version and not a hardcoded installation prefix.
        env["YDOTOOL_SOCKET"] = "/tmp/.ydotool_socket"
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

    time.sleep(0.08)
    try:
        cp = _run([command, "key", *seq], env=env, timeout=1.2)
    except subprocess.TimeoutExpired:
        return False, tr("injector.ydotool_timeout", language)
    if cp.returncode != 0:
        detail = cp.stderr.strip().splitlines()
        short = detail[-1] if detail else tr("injector.ydotool_failed_generic", language)
        return False, tr("injector.ydotool_failed", language, reason=short)
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
