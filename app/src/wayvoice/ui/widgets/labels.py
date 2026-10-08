"""Label utility functions extracted from wayvoice.ui."""

from gi.repository import Gtk, Pango


def clip_subtitle(text, limit=120):
    """Keep a subtitle short so a verbose error cannot break the layout."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def nearest_index(values, value):
    return min(range(len(values)), key=lambda i: abs(values[i] - value))


def make_label(text, css=None, *, wrap=False, xalign=0.0):
    label = Gtk.Label(label=text, xalign=xalign)
    label.set_wrap(wrap)
    if wrap:
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        label.add_css_class(css)
    return label

def index_or_zero(values, value):
    try:
        return values.index(value)
    except ValueError:
        return 0
