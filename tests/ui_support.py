"""Isolated controller/view fixtures; no services or GTK widget construction."""
from types import SimpleNamespace
from unittest import mock
import threading


class ImmediateTasks:
    """Deterministic completion seam for existing handler tests.

    Thread ownership and disposal are verified separately with the real runner.
    """
    closed = False

    def idle(self, callback, *args):
        if not self.closed:
            return callback(*args)

    def run(self, work, done, failed=None):
        try:
            value = work()
        except Exception as exc:
            if failed:
                failed(exc)
        else:
            self.idle(done, value)
        return True


def controller_context():
    from wayvoice.ui.state import UiContext, UiState
    from wayvoice.ui.window import WayVoiceWindow
    from wayvoice.ui.controllers.models import ModelsController
    from wayvoice.ui.controllers.settings import SettingsController
    from wayvoice.ui.controllers.integration import IntegrationController
    from wayvoice.ui.controllers.status import StatusController
    from wayvoice.ui.controllers.shortcut import ShortcutController
    window = WayVoiceWindow.__new__(WayVoiceWindow)
    state = UiState({'ui_language': 'en'})
    window.state = state
    window.toast = mock.Mock()
    ctx = UiContext(window, state, ImmediateTasks())
    ctx.home = SimpleNamespace()
    ctx.settings = SimpleNamespace()
    ctx.models = ModelsController(ctx)
    ctx.preferences = SettingsController(ctx)
    ctx.integration = IntegrationController(ctx)
    ctx.status = StatusController(ctx)
    ctx.shortcut = ShortcutController(ctx)
    window.context = ctx
    window.tasks = ctx.tasks
    window._close_pending = False
    window._quit_pending = False
    return ctx
