from __future__ import annotations
import shutil
import subprocess

def notify(title: str, body: str = "", *, enabled: bool = True) -> None:
    if not enabled or not shutil.which("notify-send"):
        return
    subprocess.run(
        ["notify-send", "-a", "WayVoice", title, body],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
