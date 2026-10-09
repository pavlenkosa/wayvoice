"""Opt-in status window: read-only, no transcript and no activation on updates."""
from gi.repository import Adw, Gtk, GLib
from .. import deps
from ..cli import request
from ..config import load_config
from ..i18n import tr
from .async_tasks import TaskRunner
from .health_presentation import recovery_text
from .setup_presentation import model_missing
from .widgets.labels import make_label


class IndicatorWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.tasks = TaskRunner()
        self._poll_running = False
        self.language = None
        self.set_title(self.t('indicator.title'))
        self.set_default_size(340, 220)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, f'set_margin_{side}')(16)
        self.state_label = make_label(self.t('status.waiting'), 'title-2', wrap=True)
        self.detail_label = make_label('', wrap=True)
        box.append(self.state_label)
        box.append(self.detail_label)
        self.limit_label = make_label(self.t('indicator.limit'), wrap=True)
        box.append(self.limit_label)
        buttons = Gtk.Box(spacing=8)
        self.settings_button = Gtk.Button(label=self.t('nav.settings'))
        self.settings_button.connect('clicked', lambda *_: app.activate())
        close = Gtk.Button(label=self.t('common.close'))
        close.connect('clicked', lambda *_: self.close())
        buttons.append(self.settings_button)
        buttons.append(close)
        box.append(buttons)
        self.close_button = close
        self.set_content(box)
        self.connect('unrealize', self._dispose_ui)
        self.connect('close-request', self._dispose_ui)
        self.tasks.idle(self._poll)
        self.tasks.every(650, self._poll)

    def t(self, key, **kwargs):
        return tr(key, self.language, **kwargs)

    def _dispose_ui(self, *_args):
        self.tasks.close()
        return False

    @staticmethod
    def _snapshot():
        reply = request('status', timeout=0.3)
        reply['_missing_deps'] = [d.label for d in deps.dependencies()
                                  if d.required and not deps.status_of(d)['ok']]
        return reply, load_config()

    def _poll(self):
        if not self._poll_running:
            self._poll_running = True
            self.tasks.run(self._snapshot, self._paint,
                           lambda exc: self._paint(({'ok': False, 'error': str(exc)}, {})))
        return GLib.SOURCE_CONTINUE

    def _paint(self, snapshot):
        self._poll_running = False
        reply, cfg = snapshot
        self.language = cfg.get('ui_language', self.language or 'auto')
        self.set_title(self.t('indicator.title'))
        self.settings_button.set_label(self.t('nav.settings'))
        self.close_button.set_label(self.t('common.close'))
        self.limit_label.set_text(self.t('indicator.limit'))
        if not reply.get('ok'):
            state = self.t('health.attention')
            detail = self.t('health.daemon_unavailable')
        elif reply.get('recording'):
            state = self.t('status.recording')
            detail = self.t('hero.recording_hint')
        elif reply.get('busy'):
            state = self.t('status.transcribing')
            detail = self.t('indicator.processing')
        elif reply.get('config_error') or reply.get('last_error'):
            state = self.t('health.attention')
            reason = reply.get('config_error') or reply['last_error']
            detail = '\n'.join(recovery_text(self.t, reason,
                                            'config' if reply.get('config_error') else None))
        elif ((reply.get('model') or {}).get('download') or {}).get('state') in {'downloading', 'warming'}:
            state = self.t('settings.preparing')
            detail = self.t('indicator.preparing')
        elif reply.get('_missing_deps'):
            state = self.t('health.attention')
            detail = self.t('health.deps_missing', names=', '.join(reply['_missing_deps']))
        elif reply.get('last_warning'):
            state = self.t('health.attention')
            detail = '\n'.join(recovery_text(self.t, reply['last_warning']))
        elif (reply.get('engine') or {}).get('state') != 'ready' or model_missing(reply):
            state = self.t('health.attention')
            detail = self.t('setup.model_next' if model_missing(reply) else 'hero.open_settings')
        else:
            state = self.t('status.ready')
            detail = self.t('indicator.ready' if cfg.get('shortcut') else 'setup.shortcut_disabled')
        self.state_label.set_text(state)
        self.detail_label.set_text(detail)
