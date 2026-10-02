from __future__ import annotations
import os
from pathlib import Path

def socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / "wayvoice.sock"
