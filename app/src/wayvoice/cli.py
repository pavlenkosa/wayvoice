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


#: The command list in the usage line, so that the message and the parser below
#: cannot drift apart: both are built from this one string.
_COMMANDS = (
    "toggle|start|stop|cancel|status|model [--download|--cancel]"
    "|deps [--install ID|--install-all]|settings|engine-setup|engine-status"
)


def request(command: str, timeout: float = 1.5) -> dict:
    path = socket_path()
    if not path.exists():
        return {"ok": False, "error": tr("cli.service_not_running")}
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
        return {"ok": False, "error": tr("cli.service_no_answer", reason=exc)}
    finally:
        sock.close()


def _model_command(args: list[str]) -> None:
    """Implement ``wayvoice model [--download | --cancel]``.

    The download belongs to the daemon - it is the daemon's child process, and
    only the daemon can end it without leaving a helper running - so every
    action here is a command sent to it rather than a download started here.
    That also means there is nothing to do when the daemon is not running, and
    that is reported instead of quietly downloading into a cache no daemon will
    read.
    """
    lang = _language()
    if args not in ([], ["--download"], ["--cancel"]):
        # A model name is not accepted here, and that has to be said rather than
        # ignored: ``wayvoice model --download medium`` would otherwise download
        # whatever the settings name and report success, which is exactly the
        # kind of quiet wrong answer this command exists to avoid.
        print(tr("cli.model_usage", lang), file=sys.stderr)
        print(tr("cli.model_choose_in_settings", lang), file=sys.stderr)
        raise SystemExit(2)
    if "--download" in args:
        reply = request("prepare-model", timeout=10.0)
        if reply.get("ok"):
            # "The download has started" is only true when it has. Preparing a
            # model that is already on disk loads it into the worker instead, and
            # saying otherwise would report a download that will never happen.
            print(tr(
                "cli.model_warming" if str(reply.get("state") or "") == "warming"
                else "cli.model_download_started",
                lang,
            ))
            return
        print(str(reply.get("error") or tr("cli.model_download_failed", lang)),
              file=sys.stderr)
        raise SystemExit(1)
    if "--cancel" in args:
        reply = request("cancel-download", timeout=5.0)
        phase = str(reply.get("phase") or "")
        if reply.get("ok"):
            # "Stopped" is only true of the transfer; the load that follows it
            # finishes on its own, and saying otherwise would be a lie.
            print(tr(
                "cli.model_download_stopped" if phase == "download"
                else "cli.model_warm_continues",
                lang,
            ))
            return
        print(str(reply.get("error") or tr("cli.model_nothing_running", lang)),
              file=sys.stderr)
        raise SystemExit(1)
    reply = request("status", timeout=5.0)
    if not reply.get("ok"):
        print(str(reply.get("error") or ""), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(reply.get("model") or {}, ensure_ascii=False, indent=2))


def _language() -> str | None:
    """Return the configured UI language, or ``None`` to follow the locale."""
    try:
        return load_config().get("ui_language")
    except Exception:
        return None


def _run_setup_user() -> tuple[bool, str]:
    """Re-apply the per-user desktop integration after a package install.

    ``setup-user`` applies the configured global shortcut and enables the
    ydotoold unit when ydotool is present; postinst only runs it once, at
    package installation time.

    The result is reported rather than discarded.  The package is installed at
    this point, so a failure here cannot be allowed to look like one - but it
    also must not be hidden, because what it breaks is the hotkey: a user whose
    shortcut was not applied sees an app that ignores the key and has no way of
    finding out why.
    """
    script = setup_user_script()
    if script is None:
        return False, "setup-user not found"
    try:
        proc = subprocess.run(
            [str(script)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "setup-user did not finish within 30s"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, detail[-1] if detail else f"setup-user exited with {proc.returncode}"


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
    ok, message = install_packages(packages, language=_language())
    print(message, file=sys.stdout if ok else sys.stderr)
    if not ok:
        raise SystemExit(1)
    # The desktop integration (shortcut, ydotoold unit) is applied by
    # setup-user, so re-run it: without this an installed package stays inert.
    applied, detail = _run_setup_user()
    if not applied:
        # Not an install failure - the package is in - but the hotkey may be
        # dead, and that has to be said out loud.
        print(tr("cli.deps_setup_user_failed", lang, reason=detail), file=sys.stderr)
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
            print(tr("cli.unknown_engine", engine=cfg.get("engine")), file=sys.stderr)
            raise SystemExit(1)
        if request_engine_setup(engine):
            print(tr("cli.setup_started", engine=engine.label))
            return
        # The registry says this engine has nothing to prepare. Say so instead
        # of starting a runtime for a recognizer that is not in use.
        print(tr("cli.engine_needs_no_setup", engine=engine.label), file=sys.stderr)
        raise SystemExit(1)
    if command == "engine-status":
        print(json.dumps(engine_status(load_config()), ensure_ascii=False, indent=2))
        return
    if command == "model":
        _model_command(args[1:])
        return
    if command not in {"toggle", "start", "stop", "cancel", "status", "ping", "quit"}:
        print(tr("cli.usage", commands=_COMMANDS), file=sys.stderr)
        raise SystemExit(2)

    reply = request(command)
    if command == "status" or not reply.get("ok"):
        print(json.dumps(reply, ensure_ascii=False, indent=2))
    if not reply.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
