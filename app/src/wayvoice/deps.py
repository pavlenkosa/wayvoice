"""Registry of the external (system) requirements of WayVoice.

Everything WayVoice needs that does *not* come from a Python distribution is
described here in one place: the CLI binaries that have to be present, whether
the application still works without them, why they are needed and how they are
named by the package managers we support.

The registry deliberately does not install anything.  Callers (settings UI,
``wayvoice deps``) ask for a status, and only an explicit user action may
trigger :mod:`wayvoice.pkgsys`.

Package names are never guessed: a manager without a confidently known package
name simply has no entry for that dependency, and the caller falls back to
telling the user to install the package by hand.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from .i18n import tr

# Package-name keys used in ``Dependency.packages``.  They match the manager
# keys of wayvoice.pkgsys, so the two tables can be joined directly.
APT = "apt"
DNF = "dnf"
PACMAN = "pacman"
ZYPPER = "zypper"
APK = "apk"
XBPS = "xbps"


@dataclass(frozen=True)
class Dependency:
    """One external requirement of the application.

    Attributes:
        id: stable identifier used by the CLI (``wayvoice deps --install id``).
        label: short human-readable name; kept untranslated because it names
            a program or a well-known component.
        binaries: every command that must be present.  A dependency is
            satisfied only when *all* of them are found.
        required: ``True`` when the main dictation flow stops working without
            it, ``False`` when there is a usable fallback.
        purpose_key: i18n key explaining why the dependency exists.
        packages: package names per manager; a manager that is missing from
            the mapping means "not known for that manager".
        note: rare, non-localizable hint.
    """

    id: str
    label: str
    binaries: tuple[str, ...]
    required: bool
    purpose_key: str
    packages: dict[str, str] = field(default_factory=dict)
    note: str = ""


# Order is the display order: blocking requirements first, optional ones after.
# Inside a group the order is stable so that the settings page and the CLI
# output do not shuffle between runs.
DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency(
        id="pipewire",
        label="PipeWire tools",
        binaries=("pw-record",),
        required=True,
        purpose_key="deps.pipewire",
        packages={
            # Debian/Ubuntu split the CLI tools out of the main package...
            APT: "pipewire-bin",
            # ...Fedora calls the very same set of tools "pipewire-utils"...
            DNF: "pipewire-utils",
            # ...while Arch and Void ship them inside the main package.
            PACMAN: "pipewire",
            XBPS: "pipewire",
            # openSUSE and Alpine both use the upstream name.
            ZYPPER: "pipewire-tools",
            APK: "pipewire-tools",
        },
    ),
    Dependency(
        id="wl-clipboard",
        label="wl-clipboard",
        binaries=("wl-copy",),
        required=True,
        purpose_key="deps.wl_clipboard",
        # The upstream project name is used verbatim by every distribution
        # we support, which is why there is no renaming note here.
        packages={
            APT: "wl-clipboard",
            DNF: "wl-clipboard",
            PACMAN: "wl-clipboard",
            ZYPPER: "wl-clipboard",
            APK: "wl-clipboard",
            XBPS: "wl-clipboard",
        },
    ),
    Dependency(
        id="notify",
        label="libnotify",
        binaries=("notify-send",),
        required=False,
        purpose_key="deps.notify",
        packages={
            # Only Debian/Ubuntu append the "-bin" suffix; the RPM and the
            # source based distributions all name the package after the
            # library that ships notify-send.
            APT: "libnotify-bin",
            DNF: "libnotify",
            PACMAN: "libnotify",
            ZYPPER: "libnotify",
            APK: "libnotify",
            XBPS: "libnotify",
        },
    ),
    Dependency(
        id="ydotool",
        label="ydotool",
        # ydotoold is the daemon that owns /dev/uinput; without it the client
        # command is useless, so both are required.
        binaries=("ydotool", "ydotoold"),
        required=False,
        purpose_key="deps.ydotool",
        packages={
            APT: "ydotool",
            # Fedora carries ydotool in its own repositories since Fedora 43;
            # older releases need RPM Fusion enabled.
            DNF: "ydotool",
            PACMAN: "ydotool",
            ZYPPER: "ydotool",
            # Alpine Linux and Void Linux do not package ydotool at all, so no
            # name is offered here: the UI tells the user to build or install
            # it manually instead of running a command that cannot succeed.
        },
    ),
)


def dependencies() -> tuple[Dependency, ...]:
    """Return the registry in display order."""
    return DEPENDENCIES


def get(dep_id: str) -> Dependency | None:
    """Return the dependency with this id, or ``None``."""
    for dep in DEPENDENCIES:
        if dep.id == dep_id:
            return dep
    return None


def status_of(dep: Dependency) -> dict:
    """Probe ``PATH`` for every binary of ``dep``.

    Returns a dict with ``found`` (at least one binary present), ``missing``
    (the binaries that are absent), ``binary_path`` (the first binary found,
    for diagnostics) and ``ok`` (every binary present).
    """
    missing: list[str] = []
    binary_path: str | None = None
    for name in dep.binaries:
        found = shutil.which(name)
        if found:
            if binary_path is None:
                binary_path = found
        else:
            missing.append(name)
    return {
        "found": bool(binary_path),
        "missing": tuple(missing),
        "binary_path": binary_path,
        # A dependency counts as usable only when every command it needs is
        # there, so a half-installed ydotool is still reported as missing.
        "ok": not missing,
    }


def status_all() -> list[dict]:
    """Return the status of every dependency, in registry order."""
    rows = []
    for dep in DEPENDENCIES:
        state = status_of(dep)
        rows.append({
            "id": dep.id,
            "label": dep.label,
            "purpose_key": dep.purpose_key,
            "required": dep.required,
            "found": state["found"],
            "missing": state["missing"],
            "binary_path": state["binary_path"],
        })
    return rows


def missing_required() -> list[dict]:
    """Return the status rows of blocking dependencies that are not usable."""
    return [row for row in status_all() if row["required"] and not _row_ok(row)]


def missing_optional() -> list[dict]:
    """Return the status rows of optional dependencies that are not usable."""
    return [row for row in status_all() if not row["required"] and not _row_ok(row)]


def _row_ok(row: dict) -> bool:
    """Return ``True`` when a :func:`status_all` row has nothing missing.

    ``status_all()`` is documented to expose ``found`` and ``missing``; the
    "all binaries present" decision is derived from ``missing`` so that both
    row shapes stay consistent.
    """
    return not row.get("missing")


def describe_missing(dep_or_id, language: str | None = None) -> str:
    """Return a localized "what is missing and why" sentence.

    ``dep_or_id`` may be a :class:`Dependency` or its id.  ``language`` is
    forwarded to :func:`wayvoice.i18n.tr` so that long-lived callers (the
    daemon, the injector) can report in the language the user picked.  The text
    is built from the actual list of missing binaries, so it never mentions a
    package name that may be wrong for the running distribution.
    """
    dep = dep_or_id if isinstance(dep_or_id, Dependency) else get(str(dep_or_id))
    if dep is None:
        return ""
    missing = status_of(dep)["missing"]
    if not missing:
        return ""
    return tr("deps.missing", language, binaries=", ".join(missing)) + " " + tr(dep.purpose_key, language)
