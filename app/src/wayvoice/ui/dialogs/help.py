"""Task-focused help; external pages open only on an explicit click."""
import os
from pathlib import Path
from gi.repository import Gtk
from ..widgets.labels import make_label

REPOSITORY = 'https://github.com/pavlenkosa/wayvoice'


def show_help(parent):
    t = parent.t
    window = Gtk.Window(title=t('help.title'), transient_for=parent,
                        modal=True, destroy_with_parent=True)
    window.set_default_size(540, 480)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    for side in ('top', 'bottom', 'start', 'end'):
        getattr(box, f'set_margin_{side}')(20)
    box.append(make_label(t('help.steps'), wrap=True))
    flatpak = bool(os.environ.get('FLATPAK_ID')) or Path('/.flatpak-info').is_file()
    box.append(make_label(t('help.flatpak' if flatpak else 'help.native'), wrap=True))
    for key, suffix in (('help.releases', '/releases'), ('help.issues', '/issues')):
        box.append(Gtk.LinkButton(uri=REPOSITORY + suffix, label=t(key)))
    close = Gtk.Button(label=t('common.close'))
    close.connect('clicked', lambda *_: window.close())
    box.append(close)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_child(box)
    window.set_child(scroll)
    window.present()
