"""PipeWire input discovery and an explicit, short microphone level check."""
from __future__ import annotations

import array
import json
import signal
import subprocess
import sys
import time


def list_inputs() -> list[dict[str, str]]:
    """Return stable input names; transient PipeWire object IDs are not persisted."""
    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, timeout=5, check=True,
    )
    nodes = json.loads(result.stdout)
    if not isinstance(nodes, list):
        raise ValueError("PipeWire returned an invalid device list.")
    inputs = {}
    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "PipeWire:Interface:Node":
            continue
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Audio/Source":
            continue
        name = props.get("node.name")
        if not isinstance(name, str) or not name or name.endswith(".monitor"):
            continue
        label = props.get("node.description") or props.get("node.nick") or name
        inputs[name] = {"name": name, "label": str(label)}
    return sorted(inputs.values(), key=lambda item: (item["label"].casefold(), item["name"]))


def validate_target(target: str) -> str:
    """An explicitly selected missing input must never turn into another microphone."""
    if not isinstance(target, str) or "\x00" in target:
        raise ValueError("Invalid microphone selection.")
    if target and not any(item["name"] == target for item in list_inputs()):
        raise ValueError("The selected microphone is unavailable. Reconnect it or select another input.")
    return target


def probe_level(target: str = "", cancel_event=None) -> float:
    """Measure peak amplitude for one second without writing or retaining audio."""
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Microphone check cancelled.")
    target = validate_target(target)
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("Microphone check cancelled.")
    command = ["pw-record", "--raw", "--format=s16", "--rate=16000", "--channels=1"]
    if target:
        command.extend(["--target=" + target, "--properties=node.dont-reconnect=true node.dont-fallback=true"])
    command.append("-")
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("Microphone check cancelled.")
            try:
                data, error = proc.communicate(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
            raise RuntimeError(error.decode("utf-8", errors="replace").strip() or "Microphone check stopped unexpectedly.")
        proc.send_signal(signal.SIGINT)
        data, error = proc.communicate(timeout=2)
        if proc.returncode not in (0, 1, -signal.SIGINT) or error.strip():
            raise RuntimeError(error.decode("utf-8", errors="replace").strip() or "Microphone check failed.")
        if len(data) < 2:
            raise RuntimeError("No audio was received from the microphone.")
        samples = array.array("h")
        samples.frombytes(data[:len(data) // 2 * 2])
        if sys.byteorder != "little":
            samples.byteswap()
        return min(1.0, max(abs(sample) for sample in samples) / 32768.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
