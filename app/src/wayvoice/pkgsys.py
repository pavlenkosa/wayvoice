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

import fcntl
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import deps
from .i18n import tr
from .protocol import runtime_dir

# Longest tail of the package manager output that is handed back to the caller.
_OUTPUT_TAIL_LINES = 12
_OUTPUT_MAX_CHARS = 900

#: Serialises installs across processes - the settings window and the command line
#: can both be asked to install, and they are separate processes.
#:
#: The window guards only against starting the *same* dependency twice, so clicking
#: Install on two different missing dependencies ran two managers at once. The loser
#: fails with "Unable to acquire dpkg frontend lock", one dependency stays missing,
#: and the message says nothing about the other install that is still going.
#:
#: ``flock`` rather than a lock file the callers have to remember: it is released when
#: the process dies, so a crashed install cannot wedge the next one.
_INSTALL_LOCK_NAME = "wayvoice-pkgsys.lock"

#: Seconds to wait for another install to finish before giving up on the lock.
_INSTALL_LOCK_WAIT = 2.0


def _install_lock_path() -> Path:
    return runtime_dir() / _INSTALL_LOCK_NAME


@contextmanager
def _install_lock(wait: float = _INSTALL_LOCK_WAIT):
    """Hold the cross-process install lock; yield whether we are the one holding it.

    ``flock`` rather than a lock file the callers have to remember about: it is
    released when the process dies, so a crashed or killed install cannot wedge the
    next one.

    An unavailable lock refuses installation: we cannot safely own a transaction
    without cross-process serialization.
    """
    try:
        path = _install_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - released below
    except OSError as exc:
        print(f"WayVoice: no install lock at {_install_lock_path()}: {exc}",
              file=sys.stderr)
        yield False
        return
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.1)
        yield True
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()

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
#   xbps-install  only on Void Linux;
#   pacman        only on Arch and its derivatives;
#   zypper        SUSE family, before dnf: openSUSE ships a dnf front-end while a
#                 Fedora system never has zypper;
#   apt-get       Debian family, before dnf for the same reason in reverse - Debian
#                 based images sometimes carry a dnf front-end;
#   dnf           Fedora/RHEL family;
#   apk           last: the bare name is easily provided by unrelated software.
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


#: How long to wait for the manager to be asked what it has. A lookup, not a
#: download, even though it runs on the settings window's main loop.
AVAILABILITY_TIMEOUT = 4.0


def package_available(name: str, manager: str | None = None) -> bool | None:
    """Whether this system's repositories actually carry ``name``.

    ``True``/``False`` when the manager answered, ``None`` when it could not be
    asked - in which case the caller must carry on as before rather than refuse
    an install out of caution.

    Knowing the package name is not the same as the package existing: Debian 13
    has no ``ydotool`` at all, and a manager asked to install one says so in four
    words that the user has to decode.  Asking first turns that into an answer
    before anything is run, and no dialog that cannot succeed.
    """
    key = manager or detect_manager()
    if key is None:
        return None
    try:
        if key == deps.APT:
            return _apt_has_candidate(name)
    except (OSError, subprocess.SubprocessError):
        return None
    # Other managers: the registry only offers a name where one is known, and
    # nothing here can answer the question cheaply enough to refuse on, so the
    # manager keeps the last word.
    return None


def _apt_has_candidate(name: str) -> bool | None:
    """Read ``apt-cache policy`` for a version to install.

    The locale is forced to C, and that is not decoration: apt translates its
    output, so on a Russian system the line says ``Кандидат:`` and a parser
    written against ``Candidate:`` finds nothing and answers "don't know" for
    every package - which is how this check passed silently for a package that
    is there and for one that is not.
    """
    query = shutil.which("apt-cache")
    if not query:
        return None
    environment = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    try:
        done = subprocess.run(
            [query, "policy", name],
            capture_output=True,
            text=True,
            timeout=AVAILABILITY_TIMEOUT,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            # The name is known and there is no version to install. Both shapes
            # of "no" exist: a stanza with "Candidate: (none)", and no stanza at
            # all for a name apt has never heard of.
            value = stripped.split(":", 1)[1].strip()
            return value not in ("", "(none)")
    # Nothing was printed, which is what apt does for a name it does not know.
    # That is an answer, not a shrug: the package cannot be installed.
    return False if not done.stdout.strip() else None


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
    suitable for a toast or a subtitle. A detached owner retains the install lock
    through completion, including after GUI/CLI exit. ``timeout`` only controls
    how often the caller resumes waiting; it never cancels a package transaction.

    ``language`` is the interface language the messages are written in.  It is
    passed in rather than resolved here because every message below is shown in
    the window, and a window in English with a message in the system's language
    is a message the user cannot act on.

    This function is never called implicitly; it must be triggered by an
    explicit user request.
    """
    if not packages:
        return False, tr("pkgsys.no_packages", language)
    if dry_run_command(packages) is None:
        return False, tr("pkgsys.no_packages", language)

    # One package the repositories do not have must not cancel the rest. Asked for
    # all of them - which is what "install everything missing" does, and what
    # `wayvoice deps --install-all` does - this used to return on the first name
    # apt knows nothing about, before the manager was started at all. On a system
    # where the distribution ships no ydotool package, that left pipewire-bin,
    # wl-clipboard and libnotify-bin uninstalled while the user was told only about
    # ydotool.
    #
    # Availability is checked before anything is started, and before a password
    # prompt: there is nothing to install for those names, and a dialog that
    # cannot succeed is worse than an answer. "None" - meaning "not known" - is not
    # treated as unavailable, so an unrefreshed package index still gets the try.
    skipped = [name for name in packages if package_available(name) is False]
    wanted = [name for name in packages if name not in skipped]
    if not wanted:
        return False, tr("pkgsys.package_unavailable", language, package=skipped[0])

    elevated: list[str] = []
    if requires_privilege():
        pkexec = pkexec_path()
        if not pkexec:
            return False, tr("pkgsys.need_root", language)
        environment = shutil.which("env")
        if not environment:
            return False, tr("pkgsys.failed", language, reason="env is unavailable")
        # pkexec sanitizes its environment. Set these constants *after* elevation.
        # Use the generic auth_admin policy; never trust an arbitrary env invocation.
        elevated = [pkexec, environment, "DEBIAN_FRONTEND=noninteractive", "LC_ALL=C", "LANG=C"]

    command = [*elevated, *dry_run_command(wanted)]
    ok, message = _run_manager(command, wanted, timeout, language)
    if ok and skipped:
        # The install worked; say plainly that something in the request did not, or
        # the row in the window stays missing and the user has no idea why.
        message += " " + tr("pkgsys.package_skipped", language,
                             packages=", ".join(skipped))
    return ok, message


def _run_manager(
    command: list[str],
    packages: list[str],
    timeout: float,
    language: str | None,
) -> tuple[bool, str]:
    """Wait for a detached owner; UI lifetime never owns the package transaction.

    ``timeout`` is an observation interval, not permission to kill a manager or
    release its lock. The owner holds the lock even if this caller exits.
    """
    owner = Path(__file__).with_name("pkgsys_owner.py")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(owner), language or "", *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_manager_env(), start_new_session=True,
        )
    except OSError as exc:
        return False, tr("pkgsys.failed", language, reason=str(exc))
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=max(0.1, timeout))
                break
            except subprocess.TimeoutExpired:
                # Only the owner can declare the transaction finished.
                continue
    finally:
        # Closing pipes never cancels the owner. It captures manager output itself
        # and tolerates a disappeared caller after the transaction has finished.
        for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
            if stream is not None:
                stream.close()
    return _result(proc.returncode, packages, stdout, stderr, language)


def _manager_env() -> dict[str, str]:
    """The environment a package manager is started with.

    Two things are forced rather than inherited:

    * ``DEBIAN_FRONTEND=noninteractive``, so debconf never asks for terminal input.
      For pkexec, the same constants are supplied after elevation as argv.
    * ``LC_ALL=C``, because apt translates its output. The availability check
      already does this; the output shown to the user did not, so a Russian apt
      answered in Russian inside an English sentence.
    """
    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


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
