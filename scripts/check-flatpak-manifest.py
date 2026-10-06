#!/usr/bin/env python3
"""Assertions about the Flatpak manifest that a JSON parse alone cannot make.

The release gate used to check only that the manifest was valid JSON with the
expected id and runtime. That is near-zero assurance: a manifest that lost its
wayvoice module, its pinned wl-clipboard commit, the runtime version or the
launch command still parsed and still passed. Every property asserted here is
one whose loss has actually shipped or would change what the Flatpak does:

- id / runtime / sdk / runtime-version / command: what the app is, what it is
  built against, and which entry point a launcher opens;
- the wayvoice module with a `dir` source: the application itself, built from
  this tree;
- the wl-clipboard module with BOTH tag and commit pinned: the pinned commit is
  what makes the build reproducible - a tag alone can be re-pointed upstream,
  and an unpinned checkout cannot be rebuilt bit-for-bit;
- finish-args that the application actually depends on: the PipeWire socket
  (recording), Wayland + fallback X11 (the window), the Notifications bus name
  (notify-send path) and the network (explicit runtime/model downloads only).

Exit 0 prints what was checked; exit 1 prints the first thing that is wrong.
This file is invoked by ci.yml and by scripts/release-gate, so the local truth
and CI truth are the same script.
"""

import json
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "io.github.stepan.WayVoice.json"

REQUIRED_FINISH_ARGS = {
    "--socket=wayland",
    "--socket=fallback-x11",
    "--filesystem=xdg-run/pipewire-0",
    "--talk-name=org.freedesktop.Notifications",
}


def fail(message: str) -> None:
    print(f"flatpak manifest: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if not MANIFEST.exists():
        fail(f"{MANIFEST.name} not found next to the repository root")
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"not valid JSON: {exc}")
    if not isinstance(data, dict):
        fail(f"top level is a {type(data).__name__}, not an object")

    if data.get("id") != "io.github.stepan.WayVoice":
        fail(f"id is {data.get('id')!r}")
    if data.get("runtime") != "org.gnome.Platform":
        fail(f"runtime is {data.get('runtime')!r}")
    if data.get("sdk") != "org.gnome.Sdk":
        fail(f"sdk is {data.get('sdk')!r}")
    if not data.get("runtime-version"):
        fail("runtime-version is missing")
    if data.get("command") != "wayvoice-settings":
        fail(
            f"command is {data.get('command')!r}; the launcher must open the "
            "settings window or the daemon would swallow flatpak run arguments"
        )

    modules = data.get("modules")
    if not isinstance(modules, list) or not modules:
        fail("modules is missing or empty")
    by_name = {}
    for module in modules:
        if isinstance(module, dict) and isinstance(module.get("name"), str):
            by_name[module["name"]] = module
    if "wayvoice" not in by_name:
        fail("the wayvoice module is missing")

    sources = by_name["wayvoice"].get("sources") or []
    if not any(
        isinstance(src, dict) and src.get("type") == "dir" for src in sources
    ):
        fail("the wayvoice module has no dir source: nothing builds from this tree")

    if "wl-clipboard" not in by_name:
        fail("the wl-clipboard module is missing: without wl-copy the text never "
             "reaches the clipboard")
    wl_sources = by_name["wl-clipboard"].get("sources") or []
    pinned = [
        src for src in wl_sources
        if isinstance(src, dict)
        and src.get("type") == "git"
        and src.get("tag")
        and src.get("commit")
    ]
    if not pinned:
        fail("wl-clipboard is not pinned to both a tag and a commit; the build "
             "would not be reproducible")

    finish_args = data.get("finish-args") or []
    if not isinstance(finish_args, list):
        fail("finish-args is not a list")
    missing = sorted(REQUIRED_FINISH_ARGS - set(finish_args))
    if missing:
        fail("missing finish-args: " + ", ".join(missing))

    print(
        f"flatpak manifest OK: id, runtime {data['runtime']} "
        f"{data['runtime-version']}, wayvoice module, wl-clipboard pinned, "
        f"{len(finish_args)} finish-args"
    )


if __name__ == "__main__":
    main()
