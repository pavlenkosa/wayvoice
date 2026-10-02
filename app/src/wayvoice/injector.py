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

from .deps import describe_missing
from .i18n import tr


class InjectionError(RuntimeError):
    pass


@dataclass
class InjectionResult:
    pasted: bool
    warning: str = ""


_clipboard_lock = threading.Lock()
_clipboard_proc: subprocess.Popen | None = None


def _cleanup_clipboard() -> None:
    global _clipboard_proc
    with _clipboard_lock:
        proc = _clipboard_proc
        _clipboard_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


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
        old = _clipboard_proc
        _clipboard_proc = None
        if old is not None and old.poll() is None:
            try:
                old.terminate()
                old.wait(timeout=0.25)
            except Exception:
                try:
                    old.kill()
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(
                [binary, "--foreground", "--type", "text/plain;charset=utf-8"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=False,
            )
            assert proc.stdin is not None
            proc.stdin.write(text)
            proc.stdin.close()
        except Exception as exc:
            try:
                proc.kill()  # type: ignore[name-defined]
            except Exception:
                pass
            raise InjectionError(tr("injector.clipboard_error", language, error=exc)) from exc

        # Give wl-copy a moment to connect and claim the selection. If it has
        # already exited, surface its real error; otherwise it is serving data.
        time.sleep(0.06)
        rc = proc.poll()
        if rc is not None:
            detail = ""
            try:
                if proc.stderr is not None:
                    detail = proc.stderr.read().strip()
            except Exception:
                pass
            raise InjectionError(detail or tr("injector.clipboard_write", language))

        _clipboard_proc = proc


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
    if not shutil.which("ydotool"):
        detail = describe_missing("ydotool", language)
        return False, tr("injector.ydotool_missing", language, detail=detail)

    env = _ydotool_env()
    if mode == "terminal":
        seq = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]
    else:
        seq = ["29:1", "47:1", "47:0", "29:0"]

    time.sleep(0.08)
    try:
        cp = _run(["ydotool", "key", *seq], env=env, timeout=1.2)
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
