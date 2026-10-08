"""GTK application entry point."""
import sys
from gi.repository import Adw, Gio
from .window import WayVoiceWindow


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.stepan.WayVoice', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = WayVoiceWindow(self)
        win.present()

    def do_shutdown(self):
        for window in self.get_windows():
            if isinstance(window, WayVoiceWindow):
                window._dispose_ui()
        Adw.Application.do_shutdown(self)


def main():
    raise SystemExit(App().run(sys.argv))
