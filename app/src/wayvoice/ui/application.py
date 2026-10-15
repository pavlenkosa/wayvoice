"""GTK application entry point."""
import sys
import os
import shutil
from gi.repository import Adw, Gio, GLib
from .window import WayVoiceWindow
from .indicator import IndicatorWindow
from .onboarding import OnboardingWindow
from ..onboarding import needs_onboarding


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id='io.github.stepan.WayVoice', flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option('indicator', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             'Open the compact status indicator', None)

        self.add_main_option('restore-indicator', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             'Restore the indicator after updating', None)

    def do_activate(self):
        win = next((w for w in self.get_windows() if isinstance(w, (WayVoiceWindow, OnboardingWindow))), None)
        if not win:
            win = OnboardingWindow(self, self._onboarding_finished) if needs_onboarding() else WayVoiceWindow(self)
        win.present()

    def _onboarding_finished(self, wizard):
        replacement = WayVoiceWindow(self)
        wizard._dispose_ui()
        wizard.destroy()
        replacement.present()

    def do_command_line(self, command_line):
        if command_line.get_options_dict().contains('indicator'):
            self.open_indicator()
        else:
            self.activate()
            if command_line.get_options_dict().contains("restore-indicator"):
                self.open_indicator()
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
            if isinstance(window, (WayVoiceWindow, IndicatorWindow, OnboardingWindow)):
                window._dispose_ui()
        Adw.Application.do_shutdown(self)


def restart_installed_ui(window):
    from ..paths import appimage_path
    image = appimage_path()
    launcher = image or shutil.which('wayvoice-settings')
    if not launcher:
        raise FileNotFoundError(window.state.t('update.launcher_missing'))
    args = [launcher, "--settings"] if image else [launcher]
    if any(isinstance(w, IndicatorWindow) for w in window.get_application().get_windows()):
        args.append('--restore-indicator')
    # exec replaces old imported modules and releases the application's D-Bus
    # name before the fresh GTK process registers it. A failed exec keeps the UI.
    os.execv(launcher, args)


def main():
    raise SystemExit(App().run(sys.argv))
