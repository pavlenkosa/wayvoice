from __future__ import annotations
import json
import os
import socket
import subprocess
import sys

from . import deps
from .config import load_config
from .engine import (
    engine_from_config,
    engine_status,
    request_engine_setup,
)
from .i18n import tr
from .paths import setup_user_script
from .pkgsys import (
    detect_manager,
    dry_run_command,
    install_packages,
    pkexec_path,
    requires_privilege,
    resolve_packages,
)
from .protocol import socket_path
from .shortcut import apply_shortcut


def request(command: str, timeout: float = 1.5) -> dict:
    path = socket_path()
    if not path.exists():
        return {"ok": False, "error": "Фоновый сервис WayVoice ещё не запущен."}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
        sock.sendall((command + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"Сервис WayVoice не ответил: {exc}"}
    finally:
        sock.close()


def _language() -> str | None:
    """Return the configured UI language, or ``None`` to follow the locale."""
    try:
        return load_config().get("ui_language")
    except Exception:
        return None


def _run_setup_user() -> None:
    """Re-apply the per-user desktop integration after a package install.

    ``setup-user`` applies the configured global shortcut and enables the
    ydotoold unit when ydotool is present; postinst only runs it once, at
    package installation time. Best effort: a failure here must not turn a
    successful install into an error.
    """
    script = setup_user_script()
    if script is None:
        return
    try:
        subprocess.run(
            [str(script)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _install_deps(args: list[str]) -> None:
    """Implement ``wayvoice deps [--install ID | --install-all]``.

    Installing only ever happens on an explicit request, and never happens as a
    side effect of plain ``wayvoice deps``.
    """
    lang = _language()
    wanted: list[str] = []
    install_all = False
    known = {d.id: d for d in deps.dependencies()}
    skip_next = False
    for index, value in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if value == "--install-all":
            install_all = True
        elif value == "--install":
            if index + 1 >= len(args):
                print(tr("cli.deps_unknown_id", lang, id="", ids=", ".join(known)), file=sys.stderr)
                raise SystemExit(2)
            wanted.append(args[index + 1])
            skip_next = True
        elif value.startswith("--install="):
            wanted.append(value.split("=", 1)[1])
        else:
            print(tr("cli.deps_unknown_id", lang, id=value, ids=", ".join(known)), file=sys.stderr)
            raise SystemExit(2)

    if not install_all and not wanted:
        print(json.dumps(deps.status_all(), ensure_ascii=False, indent=2))
        return

    targets = []
    for dep_id in wanted:
        dep = known.get(dep_id)
        if dep is None:
            print(tr("cli.deps_unknown_id", lang, id=dep_id, ids=", ".join(known)), file=sys.stderr)
            raise SystemExit(2)
        if deps.status_of(dep)["ok"]:
            continue
        targets.append(dep)
    if install_all:
        for row in deps.status_all():
            if not row["missing"]:
                continue
            dep = known.get(str(row["id"]))
            if dep is not None and dep not in targets:
                targets.append(dep)

    if not targets:
        print(tr("cli.deps_nothing_missing", lang))
        return

    manager = detect_manager()
    if manager is None:
        print(tr("cli.deps_no_manager", lang, programs=", ".join(d.label for d in targets)), file=sys.stderr)
        raise SystemExit(1)
    # Resolve every target on its own: one dependency with an unknown package
    # name (e.g. ydotool on Alpine or Void) must not block the others, it is
    # only reported so the user can install that one by hand.
    packages: list[str] = []
    unknown: list[str] = []
    for dep in targets:
        resolved = resolve_packages(dep, manager)
        if resolved is None:
            unknown.append(dep.label)
            continue
        for name in resolved:
            if name not in packages:
                packages.append(name)
    if not packages:
        print(
            tr("cli.deps_unknown_package", lang, manager=manager, programs=", ".join(unknown)),
            file=sys.stderr,
        )
        raise SystemExit(1)
    if unknown:
        print(
            tr("cli.deps_unknown_package", lang, manager=manager, programs=", ".join(unknown)),
            file=sys.stderr,
        )
    if requires_privilege() and pkexec_path() is None:
        print(tr("cli.deps_need_root", lang), file=sys.stderr)
        raise SystemExit(1)

    print(tr("cli.deps_running", lang, command=" ".join(dry_run_command(packages, manager) or [])))
    ok, message = install_packages(packages)
    print(message, file=sys.stdout if ok else sys.stderr)
    if not ok:
        raise SystemExit(1)
    # The desktop integration (shortcut, ydotoold unit) is applied by
    # setup-user, so re-run it: without this an installed package stays inert.
    _run_setup_user()
    print(json.dumps(deps.status_all(), ensure_ascii=False, indent=2))


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "settings"

    if command in {"settings", "ui", "config"}:
        os.execvp("wayvoice-settings", ["wayvoice-settings"])
    if command == "deps":
        _install_deps(args[1:])
        return
    if command == "apply-shortcut":
        cfg = load_config()
        ok, msg = apply_shortcut(str(cfg.get("shortcut", "F8")))
        if not ok:
            print(msg, file=sys.stderr)
            raise SystemExit(1)
        return
    if command == "engine-setup":
        cfg = load_config()
        engine = engine_from_config(cfg)
        if engine is None:
            # A broken config, not an engine without setup: saying the latter
            # would hide the actual problem.
            print(f"Неизвестный движок распознавания: {cfg.get('engine')}", file=sys.stderr)
            raise SystemExit(1)
        if request_engine_setup(engine):
            print(f"Запуск подготовки {engine.label} запрошен.")
            return
        # The registry says this engine has nothing to prepare. Say so instead
        # of starting a runtime for a recognizer that is not in use.
        print(f"{engine.label} не требует подготовки.", file=sys.stderr)
        raise SystemExit(1)
    if command == "engine-status":
        print(json.dumps(engine_status(load_config()), ensure_ascii=False, indent=2))
        return
    if command not in {"toggle", "start", "stop", "cancel", "status", "ping", "quit"}:
        print("Использование: wayvoice {toggle|start|stop|cancel|status|deps [--install ID|--install-all]|settings|engine-setup|engine-status}", file=sys.stderr)
        raise SystemExit(2)

    reply = request(command)
    if command == "status" or not reply.get("ok"):
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    if not reply.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
