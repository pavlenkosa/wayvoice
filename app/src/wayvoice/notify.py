from __future__ import annotations

import shutil
import subprocess


def notify(title: str, body: str = "", *, enabled: bool = True) -> None:
    """Show a desktop notification, and never raise.

    Every caller of this is a code path that has already done the real work or
    is in the middle of it: the recorder has started, the text has been
    inserted, the recognition has finished.  A notification that fails - the
    binary was removed between the lookup and the call, the session bus is
    gone - must not turn any of that into an error the user sees, and must not
    abort the transcription whose result is already in hand.

    That is not hypothetical: ``which`` and ``run`` are two separate steps, and
    a package upgrade or a removed dependency can land between them.
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
        subprocess.run(
            [binary, "-a", "WayVoice", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
    except Exception:
        # A notification is decoration; failing to show one is not an error.
        return