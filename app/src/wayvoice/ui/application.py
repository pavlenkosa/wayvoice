"""GTK application entry point."""
import sys
from gi.repository import Adw, Gio, GLib
from .window import WayVoiceWindow
from .indicator import IndicatorWindow


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.stepan.WayVoice', flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option('indicator', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             'Open the compact status indicator', None)

    def do_activate(self):
        win = next((w for w in self.get_windows() if isinstance(w, WayVoiceWindow)), None)
        if not win:
            win = WayVoiceWindow(self)
        win.present()

    def do_command_line(self, command_line):
        if command_line.get_options_dict().contains('indicator'):
            self.open_indicator()
        else:
            self.activate()
        return 0

    def open_indicator(self):
        win = next((w for w in self.get_windows() if isinstance(w, IndicatorWindow)), None)
        if win is None:
            win = IndicatorWindow(self)
        win.present()

    def replace_settings(self, old):
        # Keep an application window alive throughout language replacement.
        replacement = WayVoiceWindow(self)
        old._dispose_ui()
        old.destroy()
        replacement.present()

    def do_shutdown(self):
        for window in self.get_windows():
            if isinstance(window, (WayVoiceWindow, IndicatorWindow)):
                window._dispose_ui()
        Adw.Application.do_shutdown(self)


def main():
    raise SystemExit(App().run(sys.argv))
