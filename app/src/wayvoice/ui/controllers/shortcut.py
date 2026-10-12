"""ShortcutController owns its operations; pages own widgets."""


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk, Adw

from ...shortcut import label_for, portal_shortcut_desktop
from ... import service
from ...cli import request
from ...config import update_config
from ..health_presentation import show_operation_error


class ShortcutController:
    def __init__(self, context):
        self.ctx = context
        self._configuring = False


    def _open_shortcut_capture(self, *_args):
        if portal_shortcut_desktop():
            self._configure_portal_shortcut()
            return
        from ..dialogs.shortcut_window import shortcut_capture
        shortcut_capture(self.ctx.window, self.ctx.state.shortcut_binding, self.ctx.state.t, self._disable_shortcut, self._capture_shortcut_key)

    def _configure_portal_shortcut(self):
        if self._configuring:
            return
        self._configuring = True
        self.ctx.settings.shortcut_button.set_sensitive(False)

        expected = self.ctx.state.cfg.get("shortcut_backend")

        def work():
            cfg = update_config({"shortcut_backend": "portal"}, {"shortcut_backend": expected})
            if not service.start_daemon():
                raise RuntimeError(self.ctx.state.t("health.daemon_unavailable"))
            reply = request("configure-shortcut", timeout=2.0)
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or self.ctx.state.t("shortcut.portal.unavailable"))
            return cfg

        self.ctx.preferences._queue_mutation(work, self._portal_configured, self._portal_failed)

    def _portal_configured(self, result):
        if isinstance(result, dict):
            self.ctx.state.cfg["shortcut_backend"] = result["shortcut_backend"]
        self._configuring = False
        self.ctx.settings.shortcut_button.set_sensitive(True)
        self.ctx.window.toast.add_toast(Adw.Toast(title=self.ctx.state.t("shortcut.portal.choose")))

    def _portal_failed(self, exc):
        self._configuring = False
        self.ctx.settings.shortcut_button.set_sensitive(True)
        show_operation_error(self.ctx, exc)

    def _disable_shortcut(self, _button, win):
        self.ctx.state.shortcut_binding = ""
        self.ctx.settings.shortcut_row.set_subtitle(self.ctx.state.t("shortcut.disabled"))
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
            shown = label_for(binding, self.ctx.state.ui_lang)
            self.ctx.settings.shortcut_row.set_subtitle(shown)
            win.close()
        return True
