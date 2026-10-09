"""Shortcut capture dialog for WayVoice."""

from gi.repository import Gtk

from ...shortcut import label_for
from ..widgets.labels import make_label


def shortcut_capture(window, binding, t, disable_binding, capture_key):
    """Show shortcut capture dialog."""
    win = Gtk.Window(title=t("shortcut.title"), transient_for=window, modal=True, destroy_with_parent=True)
    win.set_default_size(420, 190)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(24)
    title = make_label(t("shortcut.capture"), "section-title", xalign=0.5)
    title.set_halign(Gtk.Align.CENTER)
    box.append(title)
    hint = make_label(t("shortcut.hint"), "muted", wrap=True, xalign=0.5)
    hint.set_halign(Gtk.Align.CENTER)
    box.append(hint)
    key_label = make_label(label_for(binding), "capture-key", xalign=0.5)
    key_label.set_halign(Gtk.Align.CENTER)
    box.append(key_label)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.CENTER)
    disable = Gtk.Button(label=t("shortcut.disable"))
    disable.connect("clicked", disable_binding, win)
    cancel = Gtk.Button(label=t("common.cancel"))
    cancel.connect("clicked", lambda *_: win.close())
    buttons.append(disable)
    buttons.append(cancel)
    box.append(buttons)
    win.set_child(box)
    controller = Gtk.EventControllerKey.new()
    controller.connect("key-pressed", capture_key, win, key_label)
    win.add_controller(controller)
    win.present()
