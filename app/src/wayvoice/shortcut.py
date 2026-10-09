from __future__ import annotations
import ast
import os
import re
import shlex
import subprocess
from pathlib import Path

from .i18n import tr
from .paths import command_path

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
KEY = f"{BASE}/wayvoice/"



def label_for(binding: str) -> str:
    binding = (binding or "").strip()
    if not binding:
        return "Отключено"
    if binding.upper().startswith("F") and binding[1:].isdigit():
        return binding.upper()
    label = binding
    replacements = [
        ("<Control>", "Ctrl + "),
        ("<Primary>", "Ctrl + "),
        ("<Shift>", "Shift + "),
        ("<Alt>", "Alt + "),
        ("<Super>", "Super + "),
        ("<Meta>", "Meta + "),
    ]
    for src, dst in replacements:
        label = label.replace(src, dst)
    label = re.sub(r"\s+", " ", label).strip()
    key_names = {
        "space": "Space",
        "Return": "Enter",
        "KP_Enter": "Enter",
        "Escape": "Esc",
        "BackSpace": "Backspace",
    }
    for src, dst in key_names.items():
        if label.endswith(src):
            label = label[:-len(src)] + dst
            break
    if label and len(label.rsplit(" ", 1)[-1]) == 1:
        head, sep, tail = label.rpartition(" ")
        label = (head + sep + tail.upper()) if sep else tail.upper()
    return label


def manual_command() -> str:
    """A command the desktop can launch outside the application sandbox."""
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").is_file():
        return shlex.join(["flatpak", "run", "--command=wayvoice",
                           "io.github.stepan.WayVoice", "toggle"])
    return shlex.join([command_path("wayvoice"), "toggle"])


def shortcut_support(language: str | None = None) -> tuple[bool, str]:
    """Check the active native GNOME backend, without changing any settings."""
    command = manual_command()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").is_file() or "GNOME" not in desktop:
        return False, tr("shortcut.manual_required", language, command=command)
    try:
        owner = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus", "--method",
             "org.freedesktop.DBus.NameHasOwner", "org.gnome.SettingsDaemon.MediaKeys"],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
        if owner.returncode != 0 or owner.stdout.strip() != "(true,)":
            return False, tr("shortcut.backend_unavailable", language, command=command)
        writable = subprocess.run(
            ["gsettings", "writable", SCHEMA, "custom-keybindings"],
            capture_output=True, text=True, timeout=1.0, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, tr("shortcut.backend_unavailable", language, command=command)
    if writable.returncode != 0 or writable.stdout.strip().lower() != "true":
        return False, tr("shortcut.backend_unavailable", language, command=command)
    return True, ""


def apply_shortcut(binding: str, language: str | None = None) -> tuple[bool, str]:
    supported, reason = shortcut_support(language)
    if not supported:
        return False, reason
    try:
        current = subprocess.run(
            ["gsettings", "get", SCHEMA, "custom-keybindings"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=True,
        ).stdout.strip()
        try:
            values = ast.literal_eval(current)
            if not isinstance(values, list):
                values = []
        except Exception:
            values = []
        if KEY not in values:
            values.append(KEY)

        subprocess.run(["gsettings", "set", SCHEMA, "custom-keybindings", repr(values)], check=True, timeout=1.0)
        path_schema = f"{SCHEMA}.custom-keybinding:{KEY}"
        subprocess.run(["gsettings", "set", path_schema, "name", "WayVoice"], check=True, timeout=1.0)
        # GSettings spawns the command directly, with no shell and no login
        # environment, so a bare "wayvoice" would be looked up in a minimal PATH.
        # Store an absolute path.
        wayvoice_cmd = manual_command()
        subprocess.run(["gsettings", "set", path_schema, "command", wayvoice_cmd], check=True, timeout=1.0)
        subprocess.run(["gsettings", "set", path_schema, "binding", binding], check=True, timeout=1.0)
        return True, tr("shortcut.gnome_configured", language)
    except Exception as exc:
        return False, tr("shortcut.apply_failed", language, error=exc, command=manual_command())
