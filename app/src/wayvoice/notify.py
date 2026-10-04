from __future__ import annotations

import shutil
import subprocess
import threading

#: How long a notification program is given before it is abandoned. Only a bound
#: on a background thread; nothing in the application waits for it.
NOTIFY_TIMEOUT = 5.0

#: Id of the last notification this process showed, so the next one can replace
#: it. ``None`` until the server hands the first one back.
_lock = threading.Lock()
_notification_id: int | None = None


def notification_id() -> int | None:
    """The id of the last notification, for tests and for diagnostics."""
    with _lock:
        return _notification_id


def reset_notification_id() -> None:
    """Forget the last notification id, so the next one starts a new entry."""
    global _notification_id
    with _lock:
        _notification_id = None


def notify(title: str, body: str = "", *, enabled: bool = True,
           replace: bool = True) -> None:
    """Show a desktop notification, and never raise or wait for one.

    With ``replace`` (the default) each call updates the entry this process already
    owns, so one dictation leaves one line in the history instead of three.

    A failure here must not become an error the user sees: ``which`` and starting the
    program are separate steps, and a package upgrade can land between them. The
    program is started and then forgotten, and the id it prints is read on that same
    background thread - waiting for it on the thread that answers the hot key would
    turn a stalled session bus into a pause on every dictation.
    """
    if not enabled:
        return
    try:
        binary = shutil.which("notify-send")
    except Exception:
        return
    if not binary:
        return
    with _lock:
        current = _notification_id
    args = [binary, "-a", "WayVoice", "-p"]
    if replace and current is not None:
        args += ["-r", str(current)]
    args += [title, body]
    try:
        program = subprocess.Popen(
            args,
            # A notification program must not read the daemon's terminal, and
            # must not be able to write into the protocol it does not speak.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return
    # A daemon's own thread: it must not keep the interpreter from exiting, and
    # the program is reaped rather than left as a zombie for every dictation.
    threading.Thread(target=_deliver, args=(program,), daemon=True).start()


def _deliver(program: subprocess.Popen) -> None:
    """Wait for a notification program, remember the id it printed, let it go."""
    global _notification_id
    printed = ""
    try:
        result = program.communicate(timeout=NOTIFY_TIMEOUT)
        # ``communicate`` returns (stdout, stderr); a test's stand-in may return
        # something else entirely, and a notification is not worth an exception.
        if isinstance(result, tuple) and result:
            printed = result[0] or ""
    except subprocess.TimeoutExpired:
        try:
            program.kill()
            program.wait(timeout=1.0)
        except Exception:
            pass
    except Exception:
        pass

    number = _parse_id(printed)
    if number is not None:
        with _lock:
            _notification_id = number


def _parse_id(text: str) -> int | None:
    """The notification id in ``text``, or ``None`` when there is none.

    ``notify-send --print-id`` writes one number and exits; anything else on stdout is
    not an id, and a wrong one would replace another program's notification.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            return int(line)
        return None
    return None
