"""ShortcutController owns its operations; pages own widgets."""


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk

from ...shortcut import label_for


class ShortcutController:
    def __init__(self, context):
        self.ctx = context


    def _open_shortcut_capture(self, *_args):
        from ..dialogs.shortcut_window import shortcut_capture
        shortcut_capture(self.ctx.window, self.ctx.state.shortcut_binding, self.ctx.state.t, self._disable_shortcut, self._capture_shortcut_key)

    def _disable_shortcut(self, _button, win):
        self.ctx.state.shortcut_binding = ""
        self.ctx.settings.shortcut_row.set_subtitle(self.ctx.state.t("shortcut.disabled"))
        self.ctx.home.hotkey_label.set_text(self.ctx.state.t("shortcut.disabled"))
        win.close()

    def _capture_shortcut_key(self, _controller, keyval, _keycode, state, win, key_label):
        name = Gdk.keyval_name(keyval) or ""
        if name in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R", "ISO_Level3_Shift"}:
            return True
        if name == "Escape":
            win.close()
            return True
        allowed = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK
        mods = state & allowed
        is_function = name.startswith("F") and name[1:].isdigit()
        is_special = name in {"Pause", "Print", "Scroll_Lock"}
        if not mods and not is_function and not is_special:
            key_label.set_text(self.ctx.state.t("shortcut.need_modifier"))
            return True
        binding = Gtk.accelerator_name(keyval, mods)
        if binding:
            self.ctx.state.shortcut_binding = binding
            shown = label_for(binding)
            self.ctx.settings.shortcut_row.set_subtitle(shown)
            self.ctx.home.hotkey_label.set_text(shown)
            win.close()
        return True
