from __future__ import annotations
import ast
import re
import subprocess

from .paths import command_path

SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"
KEY = f"{BASE}/wayvoice/"

DEFAULT_SHORTCUT = "F8"


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


def apply_shortcut(binding: str) -> tuple[bool, str]:
    try:
        test = subprocess.run(
            ["gsettings", "writable", SCHEMA, "custom-keybindings"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if test.returncode != 0 or test.stdout.strip().lower() != "true":
            return False, "Не удалось изменить глобальную клавишу."

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
        wayvoice_cmd = f"{command_path('wayvoice')} toggle"
        subprocess.run(["gsettings", "set", path_schema, "command", wayvoice_cmd], check=True, timeout=1.0)
        subprocess.run(["gsettings", "set", path_schema, "binding", binding], check=True, timeout=1.0)
        return True, "Глобальная клавиша применена"
    except FileNotFoundError:
        return False, "gsettings не найден."
    except Exception as exc:
        return False, f"Не удалось применить сочетание: {exc}"
