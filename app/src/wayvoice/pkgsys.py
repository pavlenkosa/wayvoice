"""Detection of the system package manager and non-interactive installs.

The module knows two things:

* which package manager this system uses and what its *client binary* is;
* what the non-interactive install command for a set of packages looks like.

Two rules are enforced throughout:

* commands are built as ``list[str]`` argument vectors and are executed without
  a shell (``shell=False``), so package names can never be interpreted as
  shell syntax;
* installing is never triggered implicitly -- :func:`install_packages` is only
  reached from an explicit user action (the settings button or
  ``wayvoice deps --install``).

Package names themselves live in :mod:`wayvoice.deps`, because they describe
the dependency and not the manager.  Only managers whose names are known for
certainly appear in :data:`MANAGERS`; an unknown combination yields ``None``
and the caller asks the user to install the package manually instead of running
a command that is bound to fail.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass, field

from . import deps
from .i18n import tr

# Longest tail of the package manager output that is handed back to the caller.
_OUTPUT_TAIL_LINES = 12
_OUTPUT_MAX_CHARS = 900


@dataclass(frozen=True)
class Manager:
    """One supported package manager."""

    id: str
    #: Name of the client binary; this is also the first argv element.
    binary: str
    #: Fully non-interactive install flags, without the package names.
    install_flags: tuple[str, ...] = field(default_factory=tuple)

    def command(self, packages: list[str]) -> list[str]:
        """Return the argv used to install ``packages``."""
        return [self.binary, *self.install_flags, *packages]


MANAGERS: dict[str, Manager] = {
    deps.APT: Manager(deps.APT, "apt-get", ("install", "-y", "--no-install-recommends")),
    deps.DNF: Manager(deps.DNF, "dnf", ("install", "-y")),
    deps.PACMAN: Manager(deps.PACMAN, "pacman", ("-S", "--needed", "--noconfirm")),
    deps.ZYPPER: Manager(deps.ZYPPER, "zypper", ("--non-interactive", "install")),
    deps.APK: Manager(deps.APK, "apk", ("add",)),
    # Void's client binary is xbps-install; "xbps" alone is not a program.
    deps.XBPS: Manager(deps.XBPS, "xbps-install", ("-Sy",)),
}

# Probe order, most unambiguous binary name first:
#
#   xbps-install  only exists on Void Linux;
#   pacman        only on Arch and its derivatives;
#   zypper        SUSE family, checked before dnf because openSUSE also ships a
#                 dnf front-end while a Fedora system never has zypper;
#   apt-get       Debian family. Checked before dnf: the reverse mistake is the
#                 plausible one, because Debian based images sometimes carry a
#                 dnf front-end, whereas apt-get essentially never appears on a
#                 Fedora or RHEL system;
#   dnf           Fedora/RHEL family;
#   apk           last: the bare name "apk" is easily provided by unrelated
#                 software (the apk-tools Python bindings, Android tooling), so
#                 it is only trusted when nothing more specific matched.
DETECT_ORDER: tuple[str, ...] = (
    deps.XBPS,
    deps.PACMAN,
    deps.ZYPPER,
    deps.APT,
    deps.DNF,
    deps.APK,
)


def manager_candidates() -> list[str]:
    """Return every known manager id, in a stable order.

    Used to build the hint shown when no manager could be detected.
    """
    return list(DETECT_ORDER)


def detect_manager() -> str | None:
    """Return the id of the first supported manager found in ``PATH``.

    The binaries are only looked up, never executed.  A broken ``PATH`` entry
    can make ``shutil.which`` raise, which must not take the caller down, so
    every probe is guarded.
    """
    for manager_id in DETECT_ORDER:
        try:
            found = shutil.which(MANAGERS[manager_id].binary)
        except OSError:
            continue
        if found:
            return manager_id
    return None


def requires_privilege() -> bool:
    """Return ``True`` when the current process is not root."""
    try:
        return os.geteuid() != 0
    except AttributeError:  # pragma: no cover - non-POSIX platform
        return True


def pkexec_path() -> str | None:
    """Return the path of ``pkexec``, or ``None`` when it is not installed."""
    return shutil.which("pkexec")


def resolve_packages(dep_or_id, manager: str | None = None) -> list[str] | None:
    """Return the package names of one dependency for ``manager``.

    ``manager`` defaults to the detected one.  ``None`` is returned when the
    manager is unknown or when the package name for that manager is not known
    with certainty -- the caller must then ask the user to install it manually.
    """
    dep = dep_or_id if isinstance(dep_or_id, deps.Dependency) else deps.get(str(dep_or_id))
    if dep is None:
        return None
    key = manager or detect_manager()
    if key is None:
        return None
    name = dep.packages.get(key)
    if not name:
        return None
    return [name]


def resolve_all(entries, manager: str | None = None) -> list[str] | None:
    """Return de-duplicated package names for several dependencies.

    ``None`` is returned as soon as one entry has no confidently known package
    name, so a partial install command is never built.
    """
    names: list[str] = []
    for entry in entries:
        resolved = resolve_packages(entry, manager)
        if resolved is None:
            return None
        for name in resolved:
            if name not in names:
                names.append(name)
    return names


def dry_run_command(packages, manager: str | None = None) -> list[str] | None:
    """Return the argv that :func:`install_packages` would run.

    ``None`` when nothing is known well enough to build a command (no packages,
    no detectable manager, or an unknown manager id).  This never executes
    anything and exists for the tests, the diagnostics text and the UI preview.
    """
    if not packages:
        return None
    key = manager or detect_manager()
    if key is None:
        return None
    entry = MANAGERS.get(key)
    if entry is None:
        return None
    return entry.command(list(packages))


def _tail(text: str) -> str:
    """Return the last lines of ``text``, bounded in size for display."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    tail = lines[-_OUTPUT_TAIL_LINES:]
    joined = "\n".join(tail)
    if len(joined) > _OUTPUT_MAX_CHARS:
        joined = "…" + joined[-_OUTPUT_MAX_CHARS:]
    return joined


def install_packages(
    packages: list[str],
    timeout: float = 300.0,
    *,
    language: str | None = None,
) -> tuple[bool, str]:
    """Install ``packages`` non-interactively and report the outcome.

    Runs the manager directly when the process is already root, otherwise
    through ``pkexec``, which shows the polkit password prompt.  Returns
    ``(ok, message)`` where ``message`` is a short tail of the manager output
    suitable for a toast or a subtitle.

    ``language`` is the interface language the messages are written in.  It is
    passed in rather than resolved here because every message below is shown in
    the window, and a window in English with a message in the system's language
    is a message the user cannot act on.

    This function is never called implicitly; it must be triggered by an
    explicit user request.
    """
    argv = dry_run_command(packages)
    if argv is None:
        return False, tr("pkgsys.no_packages", language)
    if not packages:
        return False, tr("pkgsys.no_packages", language)

    elevated: list[str] = []
    if requires_privilege():
        pkexec = pkexec_path()
        if not pkexec:
            return False, tr("pkgsys.need_root", language)
        elevated = [pkexec]

    command = [*elevated, *argv]
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Its own session, so the timeout below can take down the whole
            # tree.  Killing only pkexec leaves apt-get running, and apt still
            # holds /var/lib/dpkg/lock: the next attempt by the user then fails
            # with "Could not get lock", which says nothing about the timeout
            # that caused it.
            start_new_session=True,
        )
    except OSError as exc:
        return False, tr("pkgsys.failed", language, reason=str(exc))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            return False, tr("pkgsys.timeout", language, seconds=_seconds(timeout))
        detail = _tail(stderr) or _tail(stdout)
        if not detail:
            return False, tr("pkgsys.timeout", language, seconds=_seconds(timeout))
        return False, tr(
            "pkgsys.timeout_detail",
            language,
            seconds=_seconds(timeout),
            detail=detail,
        )
    return _result(proc.returncode, packages, stdout, stderr, language)


def _seconds(timeout: float) -> int:
    """Whole seconds for a message; never "0 s" for something that timed out."""
    return max(1, int(round(float(timeout))))


def _kill_tree(proc) -> None:
    """Terminate a package manager and everything it started."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            try:
                proc.kill() if sig == signal.SIGKILL else proc.terminate()
            except OSError:
                return
        try:
            proc.wait(timeout=3.0)
            return
        except subprocess.TimeoutExpired:
            continue


def _result(
    returncode: int,
    packages,
    stdout: str,
    stderr: str,
    language: str | None = None,
) -> tuple[bool, str]:
    if returncode == 0:
        return True, tr("pkgsys.installed", language, packages=", ".join(packages))
    output = _tail(stderr) or _tail(stdout)
    return False, tr(
        "pkgsys.failed", language, reason=output or f"exit code {returncode}"
    )
