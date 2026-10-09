"""Composition and navigation shell; domain operations belong to controllers."""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, Gtk
from ..config import load_config
from .async_tasks import TaskRunner
from .state import UiContext, UiState
from .style import CSS
from .pages.home import HomePage
from .pages.settings import SettingsPage
from .controllers.models import ModelsController
from .controllers.integration import IntegrationController
from .controllers.settings import SettingsController
from .controllers.shortcut import ShortcutController
from .controllers.status import StatusController
from .dialogs.about import show_about
from .widgets.labels import clip_subtitle


class WayVoiceWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title('WayVoice')
        self.set_default_size(780, 760)
        self.set_size_request(640, 620)
        self.state = UiState(load_config())
        self.tasks = TaskRunner()
        self._close_pending = False
        self._quit_pending = False
        ctx = self.context = UiContext(self, self.state, self.tasks)
        self.toast = Adw.ToastOverlay()
        ctx.models = ModelsController(ctx)
        ctx.integration = IntegrationController(ctx)
        ctx.status = StatusController(ctx)
        ctx.preferences = SettingsController(ctx)
        ctx.shortcut = ShortcutController(ctx)
        self.home = HomePage(ctx)
        self.settings = SettingsPage(ctx)
        self._install_css()
        for name, callback in (('about', self._show_about), ('diagnostics', ctx.status._copy_diagnostics), ('quit', self._quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.stack.add_titled_with_icon(self.home.root, 'home', self.t('nav.home'), 'audio-input-microphone-symbolic')
        self.stack.add_titled_with_icon(self.settings.root, 'settings', self.t('nav.settings'), 'preferences-system-symbolic')
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar()
        header.set_title_widget(switcher)
        menu_button = Gtk.MenuButton(icon_name='open-menu-symbolic', tooltip_text=self.t('nav.settings'))
        menu = Gio.Menu()
        for label, action in (('menu.diagnostics', 'diagnostics'), ('menu.about', 'about'), ('menu.quit', 'quit')):
            menu.append(self.t(label), f'win.{action}')
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)
        self.save_button = Gtk.Button(label=self.t('settings.save'))
        self.save_button.add_css_class('suggested-action')
        self.save_button.connect('clicked', ctx.preferences._save)
        header.pack_start(self.save_button)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.toast.set_child(toolbar)
        self.toast.add_css_class('window-root')
        self.set_content(self.toast)
        self.save_button.set_visible(False)
        self._previous_page = self.stack.get_visible_child_name()
        self.stack.connect('notify::visible-child', self._save_button_visibility)
        self.connect('close-request', self._close_requested)
        self.connect('unrealize', self._dispose_ui)
        ctx.preferences._update_engine_visibility()
        ctx.models._sync_model_ui()
        ctx.preferences.remember_draft()
        self.tasks.idle(ctx.integration._background_start)
        self.tasks.idle(ctx.models._refresh_model_state)
        self.tasks.every(650, ctx.status._poll_status)
        self.tasks.every(900, ctx.preferences._poll_engine_settings)

    def t(self, key, **kwargs):
        return self.state.t(key, **kwargs)

    def _install_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode('utf-8'))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _save_button_visibility(self, *_args):
        page = self.stack.get_visible_child_name()
        previous = self._previous_page
        self._previous_page = page
        if previous == 'settings' and page != 'settings' and self.context.preferences.has_unsaved_changes():
            self.stack.set_visible_child_name('settings')
            def proceed():
                self._previous_page = None
                self.stack.set_visible_child_name(page)
            self.context.preferences.confirm_leaving(proceed)
        self.save_button.set_visible(self.stack.get_visible_child_name() == 'settings')

    def _toast(self, title, timeout=3):
        self.toast.add_toast(Adw.Toast(title=clip_subtitle(title, 160), timeout=timeout))

    def _show_about(self, *_args):
        show_about(self)

    def _dispose_ui(self, *_args):
        self.tasks.close()

    def _close_requested(self, *_args):
        if self.context.preferences._mutations:
            # An accepted Save must finish; the GTK loop remains responsive.
            self._close_pending = True
            return True
        self._dispose_ui()
        return False

    def _mutations_finished(self):
        if self._quit_pending:
            self._quit()
        elif self._close_pending:
            self.close()

    def _quit(self, *_args):
        if self.context.preferences._mutations:
            self._quit_pending = True
            return
        self._dispose_ui()
        app = self.get_application()
        if app:
            app.quit()
