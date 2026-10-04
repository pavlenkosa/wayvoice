from __future__ import annotations

import shutil
import subprocess
import threading

#: How long a notification program is given before it is abandoned.  It is only
#: a bound on a background thread: nothing in the application waits for it.
NOTIFY_TIMEOUT = 5.0


def notify(title: str, body: str = "", *, enabled: bool = True) -> None:
    """Show a desktop notification, and never raise or wait for one.

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
    """
    if not enabled:
        return
    try:
        binary = shutil.which("notify-send")
    except Exception:
        return
    if not binary:
        return
    try:
        program = subprocess.Popen(
            [binary, "-a", "WayVoice", title, body],
            # A notification program must not read the daemon's terminal, and
            # must not be able to write into the protocol it does not speak.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return
    # A daemon's own thread: it must not keep the interpreter from exiting, and
    # the program is reaped rather than left as a zombie for every dictation.
    threading.Thread(target=_abandon, args=(program,), daemon=True).start()


def _abandon(program: subprocess.Popen) -> None:
    """Wait for a notification program, and stop it if it overstays."""
    try:
        program.wait(timeout=NOTIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            program.kill()
            program.wait(timeout=1.0)
        except Exception:
            pass
    except Exception:
        pass
