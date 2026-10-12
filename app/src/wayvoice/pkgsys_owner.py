"""Independent, unprivileged owner of one explicit package transaction.

The GUI/CLI can disappear without releasing the install lock or killing dpkg.
This script is bundled with the Python package; no privileged helper is installed.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
from pathlib import Path

# Direct script execution also works in installed prefix and source layouts.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wayvoice.pkgsys import _install_lock, _manager_env
from wayvoice.i18n import tr


def run(command: list[str], language: str | None = None) -> tuple[int, str, str]:
    # Main-thread owner, detached from the caller's session. No ordinary signal
    # may release ownership while a privileged child still holds system locks.
    watched = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in watched}
    try:
        with _install_lock() as ours:
            if not ours:
                return 1, "", tr("pkgsys.install_busy", language)
            # File-backed output cannot fill a pipe after the caller disappears,
            # and avoids decoding/pipe errors releasing a live transaction's lock.
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                try:
                    proc = subprocess.Popen(
                        command, stdin=subprocess.DEVNULL, stdout=out,
                        stderr=err, env=_manager_env(), start_new_session=True,
                    )
                except OSError as exc:
                    return 1, "", str(exc)
                while True:
                    try:
                        code = proc.wait()
                        break
                    except (KeyboardInterrupt, InterruptedError):
                        continue
                def tail(stream):
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 8192))
                    return stream.read().decode("utf-8", errors="replace")
                return code, tail(out), tail(err)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    if len(sys.argv) < 3:
        return 2
    code, stdout, stderr = run(sys.argv[2:], sys.argv[1] or None)
    # The parent can close its pipes while the manager runs. Output delivery is
    # attempted only after completion and never controls transaction lifetime.
    for stream, value in ((sys.stdout, stdout), (sys.stderr, stderr)):
        try:
            stream.write(value)
            stream.flush()
        except BrokenPipeError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
