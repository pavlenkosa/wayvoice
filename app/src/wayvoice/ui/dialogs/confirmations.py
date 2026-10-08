"""Confirmation dialogs for WayVoice."""

from gi.repository import Gtk

from ... import model_store
from ...models import display_name
from ..widgets.labels import make_label


def download_confirmation(window, model_id, size_bytes, t, language, confirmed):
    """Show download confirmation dialog."""
    if size_bytes <= 0:
        # The size is what the catalogue says; a model whose files are not on disk
        # has none to count, and a question without a number is a guess.
        size = t("store.download_unknown_size")
    else:
        size = model_store.human_size(size_bytes, language)
    name = display_name(model_id)
    # Built by hand like the delete confirmation: a confirmation has to open
    # on the GTK 4 versions WayVoice supports.
    win = Gtk.Window(
        title=t("store.download_title"), transient_for=window, modal=True
    )
    win.set_default_size(460, 190)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(24)
    title = make_label(name, "section-title", xalign=0.5)
    title.set_halign(Gtk.Align.CENTER)
    box.append(title)
    body = make_label(
        t("store.download_body", name=name, size=size), "muted", wrap=True, xalign=0.5
    )
    body.set_halign(Gtk.Align.CENTER)
    box.append(body)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.CENTER)
    later = Gtk.Button(label=t("store.download_later"))
    later.connect("clicked", lambda *_: win.close())
    go = Gtk.Button(label=t("store.download_now"))
    go.add_css_class("suggested-action")
    go.connect("clicked", confirmed, win)
    buttons.append(later)
    buttons.append(go)
    box.append(buttons)
    win.set_child(box)
    win.present()


def delete_confirmation(window, model_id, name, t, confirmed):
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
    body = make_label(t("store.delete_body", name=name), "muted", wrap=True, xalign=0.5)
    body.set_halign(Gtk.Align.CENTER)
    box.append(body)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.CENTER)
    cancel = Gtk.Button(label=t("common.cancel"))
    cancel.connect("clicked", lambda *_: win.close())
    confirm = Gtk.Button(label=t("common.delete"))
    confirm.add_css_class("destructive-action")
    confirm.connect("clicked", confirmed, win, model_id)
    buttons.append(cancel)
    buttons.append(confirm)
    box.append(buttons)
    win.set_child(box)
    win.present()
