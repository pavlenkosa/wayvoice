"""Model-only confirmation with an explicit warm-worker stop choice."""
from gi.repository import Gtk
from ..widgets.labels import make_label


def delete_confirmation(window, model_id, name, t, confirmed, held=False):
    """Show delete confirmation dialog."""
    # Built by hand like the shortcut dialog rather than with Gtk.AlertDialog: that
    # widget does not have the same properties in every GTK 4 release, and a
    # confirmation has to open on the versions we support.
    win = Gtk.Window(title=t("store.delete_title"), transient_for=window, modal=True)
    win.set_default_size(440, 170)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(24)
    title = make_label(name, "section-title", xalign=0.5)
    title.set_halign(Gtk.Align.CENTER)
    box.append(title)
    body = make_label(t("store.delete_stop_body" if held else "store.delete_body", name=name), "muted", wrap=True, xalign=0.5)
    body.set_halign(Gtk.Align.CENTER)
    box.append(body)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.CENTER)
    cancel = Gtk.Button(label=t("common.cancel"))
    cancel.connect("clicked", lambda *_: win.close())
    confirm = Gtk.Button(label=t("store.stop_and_delete" if held else "common.delete"))
    confirm.add_css_class("destructive-action")
    confirm.connect("clicked", confirmed, win, model_id)
    buttons.append(cancel)
    buttons.append(confirm)
    box.append(buttons)
    win.set_child(box)
    win.present()
