"""Home quick selection; disk probes and daemon requests run outside GTK."""
import json
import time
from gi.repository import Gtk
from ... import languages
from ...models import forced_language, display_name
from ...selection import FIELDS, prepared_choices
from ...cli import request
from ..widgets.language_picker import LanguagePicker, language_choices


class SelectionController:
    def __init__(self, ctx):
        self.ctx = ctx
        self.options = []
        self.effective = {}
        self.pending = False
        self._choice_key = None
        self._cache_at = 0
        self._cache_key = None
        self._cache = []
        self._last_reply = {}
        self._last_cfg = {}
        self._dirty = False
        self._painting = False

    def snapshot(self, cfg):
        key = tuple(str(cfg.get(k)) for k in ('engine', 'model', 'whisper_cpp_binary', 'whisper_cpp_model'))
        if key != self._cache_key or time.monotonic() - self._cache_at >= 5:
            self._cache = prepared_choices(cfg)
            self._cache_key, self._cache_at = key, time.monotonic()
        return self._cache

    def attach(self, group):
        from gi.repository import Adw
        self.model = Adw.ComboRow(title=self.ctx.state.t('settings.model'))
        self.model.connect('notify::selected', self.changed)
        self.model.set_sensitive(False)
        group.add(self.model)
        codes, labels = language_choices(self.ctx.state.ui_lang)
        self.language = LanguagePicker(self.ctx.state.t('settings.language'), codes, labels, 0)
        self.language._control().connect('notify::selected', self.changed)
        self.language.set_sensitive(False)
        group.add(self.language.row)
        self.apply = Gtk.Button(label=self.ctx.state.t('settings.save'), valign=Gtk.Align.CENTER)
        self.apply.set_sensitive(False)
        self.apply.connect('clicked', self.save)
        self.model.add_suffix(self.apply)
        reload_button = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text=self.ctx.state.t('health.retry'), valign=Gtk.Align.CENTER)
        reload_button.connect('clicked', self.reload)
        self.model.add_suffix(reload_button)
        self.configure = Adw.ActionRow(title=self.ctx.state.t('settings.prepare'))
        button = Gtk.Button(label=self.ctx.state.t('setup.configure'), valign=Gtk.Align.CENTER)
        button.connect('clicked', lambda *_: self.ctx.window.open_settings('model_state_row'))
        self.configure.add_suffix(button)
        group.add(self.configure)

    def changed(self, *_):
        if self._painting:
            return
        if not self._dirty:
            self._draft_expected = dict(self.effective)
        self._dirty = True
        index = self.model.get_selected()
        if index < len(self.options):
            forced = self.options[index].get('forced_language')
            if forced:
                self._painting = True
                self.language.select_code(forced)
                self._painting = False
            self.language.set_sensitive(not forced and self.model.get_sensitive())

    def paint(self, reply, cfg):
        self._last_reply, self._last_cfg = reply, cfg
        options = list(reply.get('_prepared_choices', self.options))
        options = [o for o in options if not o.get('unavailable')]
        if options and not any(o['engine'] == cfg.get('engine') and o['model'] == cfg.get('model') for o in options):
            options.insert(0, {'engine': cfg.get('engine'), 'model': cfg.get('model'), 'forced_language': None, 'unavailable': True, 'label': display_name(str(cfg.get('model'))) + ' · ' + self.ctx.state.t('hero.not_ready')})
        key = tuple(cfg.get(k) for k in FIELDS)
        changed = options != self.options or key != self._choice_key
        self.effective = {k: cfg.get(k) for k in FIELDS}
        if changed and not self.pending and not self._dirty:
            self._painting = True
            self.options = list(options)
            self.model.set_model(Gtk.StringList.new([o['label'] for o in options]))
            index = next((i for i, o in enumerate(options) if o['engine'] == cfg.get('engine') and o['model'] == cfg.get('model')), Gtk.INVALID_LIST_POSITION)
            self.model.set_selected(index)
            forced = options[index].get('forced_language') if index < len(options) else None
            self.language.select_code(forced or languages.normalize(cfg.get('language')))
            self._painting = False
            self._choice_key = key
            self._dirty = False
        state = (reply.get('model') or {}).get('download') or {}
        busy = (not reply.get('ok') or reply.get('busy') or reply.get('recording') or
                state.get('state') in ('downloading', 'warming') or self.pending or
                getattr(self.ctx.window, '_update_busy', False) or self.ctx.preferences._saving)
        available = bool(self.options) and not busy
        self.model.set_sensitive(available)
        self.apply.set_sensitive(available and self.model.get_selected() < len(self.options) and not self.options[self.model.get_selected()].get("unavailable"))
        index = self.model.get_selected()
        forced = self.options[index].get('forced_language') if index < len(self.options) else None
        self.language.set_sensitive(available and not forced)
        self.configure.set_visible(True)
        matches = state.get('engine') == cfg.get('engine') and state.get('model') == cfg.get('model')
        if matches and state.get('state') == 'error':
            self.model.set_subtitle(str(state.get('error') or self.ctx.state.t('hero.not_ready')))
        elif busy:
            self.model.set_subtitle(self.ctx.state.t('status.preparing') if state.get('state') == 'warming' else '')
        else:
            self.model.set_subtitle(self.ctx.state.t('status.ready') if any(not o.get('unavailable') and o['engine'] == cfg.get('engine') and o['model'] == cfg.get('model') for o in self.options) else self.ctx.state.t('hero.not_ready'))

    def reload(self, *_):
        if self.pending:
            return
        self._dirty = False
        self._choice_key = None
        self.paint(self._last_reply, self._last_cfg)

    def save(self, *_):
        if self.pending or not self.apply.get_sensitive():
            return
        index = self.model.get_selected()
        if index >= len(self.options):
            return
        option = self.options[index]
        if option.get('unavailable'):
            return
        wanted = {'engine': option['engine'], 'model': option['model'],
                  'language': forced_language(option['model']) if option['engine'] == 'faster-whisper' else None}
        wanted['language'] = wanted['language'] or self.language.selected_code()
        expected = self._draft_expected if self._dirty else self.effective
        changes = {k: v for k, v in wanted.items() if expected.get(k) != v}
        payload = {'changes': changes, 'expected': dict(expected),
                   'expires': time.monotonic() + 15}
        command = 'select-recognition ' + json.dumps(payload)
        self.pending = True
        self.apply.set_sensitive(False)
        self.model.set_sensitive(False)
        self.language.set_sensitive(False)
        accepted = self.ctx.tasks.run(lambda: request(command, timeout=10), self.finished,
                                      lambda exc: self.finished({'ok': False, 'error': str(exc)}))
        if not accepted:
            self.pending = False

    def finished(self, reply):
        self.pending = False
        if reply.get('cfg'):
            self.ctx.preferences.sync_effective_selection(reply['cfg'])
        if not reply.get('ok'):
            self.ctx.window._toast(str(reply.get('error') or self.ctx.state.t('hero.not_ready')))
        else:
            key = 'status.preparing' if reply.get('state') == 'warming' else 'status.ready'
            self.ctx.window._toast(self.ctx.state.t(key))
        if reply.get("ok"):
            self._dirty = False
            self._choice_key = None
        self.ctx.status._poll_status()
