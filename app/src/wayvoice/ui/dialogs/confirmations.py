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
        title=t("store.download_title"), transient_for=window, modal=True, destroy_with_parent=True
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


def unsaved_confirmation(window, t, respond):
    """Navigation confirmation compatible with the supported GTK versions."""
    win = Gtk.Window(title=t("settings.unsaved_title"), transient_for=window, modal=True, destroy_with_parent=True)
    win.set_default_size(490, 180)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(24)
    box.append(make_label(t("settings.unsaved_body"), wrap=True, xalign=0.5))
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.CENTER)
    answered = False

    def answer(choice):
        nonlocal answered
        if answered:
            return
        answered = True
        win.close()
        respond(choice)

    for choice, key in (("stay", "settings.stay"), ("leave", "settings.leave_unsaved"), ("save", "settings.save")):
        button = Gtk.Button(label=t(key))
        if choice == "save":
            button.add_css_class("suggested-action")
        button.connect("clicked", lambda _button, value=choice: answer(value))
        buttons.append(button)
    box.append(buttons)
    win.connect("close-request", lambda *_: answer("stay"))
    win.set_child(box)
    win.present()
