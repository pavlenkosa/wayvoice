"""Bounded log collection for native service and direct-process installations."""
import subprocess

from .. import service
from ..i18n import tr

MAX_LOG_BYTES = 65536


def read_logs(lines=200, timeout=5.0, language=None):
    """Read the selected service's tail; never start or alter a service."""
    if service.systemd_available():
        result = subprocess.run(
            ["journalctl", "--user", "-u", service.DAEMON_UNIT, "-n", str(lines),
             "--no-pager", "--output=short-iso"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode:
            raise subprocess.SubprocessError((result.stderr or "").strip() or f"exit {result.returncode}")
        return (result.stdout or "")[-MAX_LOG_BYTES:]
    path = service.service_log_path()
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - MAX_LOG_BYTES))
            data = handle.read(MAX_LOG_BYTES)
    except FileNotFoundError as exc:
        raise OSError(tr("diagnostics.log_missing", language, path=path)) from exc
    tail = data.decode("utf-8", "replace").splitlines()
    if size > MAX_LOG_BYTES and len(tail) > 1:
        tail = tail[1:]  # The first line may begin in the middle of a UTF-8 message.
    return "\n".join(tail[-lines:])
