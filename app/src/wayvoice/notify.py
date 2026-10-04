from __future__ import annotations

import shutil
import subprocess
import threading

#: How long a notification program is given before it is abandoned.  It is only
#: a bound on a background thread: nothing in the application waits for it.
NOTIFY_TIMEOUT = 5.0

#: The id of the notification this process last showed, so the next one can
#: replace it instead of arriving next to it.  ``None`` until the first id comes
#: back from the server.
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

    One dictation is three states - recording, transcribing, done - and three
    popups per dictation is how a notification center stops being read at all.
    With ``replace`` (the default) each of them updates the one entry this
    process already owns, so what is on screen is the current state and the
    history holds one line per dictation instead of three.

    Every caller of this is a code path that has already done the real work or
    is in the middle of it: the recorder has started, the text has been
    inserted, the recognition has finished.  A notification that fails - the
    binary was removed between the lookup and the call, the session bus is
    gone - must not turn any of that into an error the user sees, and must not
    abort the transcription whose result is already in hand.

    That is not hypothetical: ``which`` and starting the program are two separate
    steps, and a package upgrade or a removed dependency can land between them.

    The program is started and then forgotten.  It used to be waited for, with a
    five second deadline, on the thread that was answering the hot key - so a
    session bus that had stopped answering turned every single dictation into a
    five second pause, and the settings window, which asks the daemon for its
    status every 650 ms with a 0.12 s deadline, decided the daemon had died.
    Reading the id the server prints happens on the same background thread, for
    the same reason.
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

    ``notify-send --print-id`` writes one number and exits. Anything else on
    stdout - a warning from the program, a line from a wrapper - is not an id,
    and a wrong one would replace somebody else's notification.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.isdigit():
            return int(line)
        return None
    return None
