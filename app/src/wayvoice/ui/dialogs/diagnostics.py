"""Review a report before replacing the clipboard or saving a private copy."""
from gi.repository import Gio, GLib, Gtk

from ..widgets.labels import make_label


def diagnostics_preview(parent, text, t, on_copy):
    win = Gtk.Window(title=t("diagnostics.preview_title"), transient_for=parent,
                     modal=True, destroy_with_parent=True)
    win.set_default_size(720, 540)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(18)
    box.append(make_label(t("diagnostics.preview_notice"), wrap=True))
    view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.get_buffer().set_text(text)
    scroll = Gtk.ScrolledWindow(vexpand=True)
    scroll.set_child(view)
    box.append(scroll)
    message = make_label("", wrap=True)
    box.append(message)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    buttons.set_halign(Gtk.Align.END)
    copy = Gtk.Button(label=t("diagnostics.copy"))
    copy.connect("clicked", lambda *_: on_copy())
    save = Gtk.Button(label=t("diagnostics.save"))
    close = Gtk.Button(label=t("common.close"))
    close.connect("clicked", lambda *_: win.close())
    for button in (copy, save, close):
        buttons.append(button)
    box.append(buttons)
    closed = False
    chooser = None

    def saved(file, result):
        try:
            file.replace_contents_finish(result)
            detail = t("diagnostics.saved")
        except GLib.Error as exc:
            detail = t("diagnostics.save_failed", detail=str(exc))
        if not closed:
            save.set_sensitive(True)
            message.set_text(detail)

    def selected(dialog, response):
        nonlocal chooser
        file = dialog.get_file() if response == Gtk.ResponseType.ACCEPT else None
        dialog.hide()
        chooser = None
        if file is not None and not closed:
            save.set_sensitive(False)
            file.replace_contents_async(
                text.encode("utf-8"), None, False,
                Gio.FileCreateFlags.PRIVATE | Gio.FileCreateFlags.REPLACE_DESTINATION,
                None, saved,
            )

    def choose(*_args):
        nonlocal chooser
        if chooser is not None:
            return
        chooser = Gtk.FileChooserNative(
            title=t("diagnostics.save"), transient_for=win, modal=True,
            action=Gtk.FileChooserAction.SAVE,
            accept_label=t("diagnostics.save"), cancel_label=t("common.cancel"),
        )
        chooser.set_current_name("wayvoice-diagnostics.txt")
        chooser.connect("response", selected)
        chooser.show()

    def dispose(*_args):
        nonlocal closed
        closed = True
        if chooser is not None:
            chooser.hide()
        return False

    save.connect("clicked", choose)
    win.connect("close-request", dispose)
    win.connect("unrealize", dispose)
    win.set_child(box)
    win.present()
    return win
