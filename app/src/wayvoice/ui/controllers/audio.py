"""Explicit device discovery and a short, cancellable microphone check."""
import json
import time
from gi.repository import Gtk
from ...audio_devices import list_inputs
from ...cli import request


class AudioController:
    def __init__(self, ctx):
        self.ctx = ctx
        saved = str(ctx.state.cfg.get('microphone', ''))
        self.names = [''] + ([saved] if saved else [])
        self.pending = False

    def selected(self):
        return self.names[self.ctx.settings.microphone.get_selected()]

    def refresh(self, *_args):
        if self.pending:
            return
        selected = self.selected()
        self.pending = True
        self.ctx.settings.audio_refresh.set_sensitive(False)
        self.ctx.settings.microphone.set_sensitive(False)
        def done(devices):
            self.pending = False
            self.ctx.settings.audio_refresh.set_sensitive(True)
            self.ctx.settings.microphone.set_sensitive(True)
            self.names = [''] + [d['name'] for d in devices]
            labels = [self.ctx.state.t('audio.default')] + [d['label'] for d in devices]
            if selected and selected not in self.names:
                self.names.append(selected)
                labels.append(self.ctx.state.t('audio.missing', name=selected))
            self.ctx.settings.microphone.set_model(Gtk.StringList.new(labels))
            self.ctx.settings.microphone.set_selected(self.names.index(selected))
        self.ctx.tasks.run(list_inputs, done, self.failed)

    def check(self, *_args):
        if self.pending:
            return
        target = self.selected()
        self.pending = True
        self.ctx.settings.audio_check.set_sensitive(False)
        self.ctx.settings.microphone.set_sensitive(False)
        self.ctx.settings.audio_meter.set_value(0)
        self.ctx.settings.audio_level.set_subtitle(self.ctx.state.t('audio.listening'))
        def done(level):
            self.pending = False
            self.ctx.settings.audio_check.set_sensitive(True)
            self.ctx.settings.microphone.set_sensitive(True)
            self.ctx.settings.audio_meter.set_value(level)
            self.ctx.settings.audio_level.set_subtitle(self.ctx.state.t(
                'audio.silent' if level < .01 else 'audio.detected'))
        def work():
            result = request('check-microphone ' + json.dumps({'target': target, 'expires': time.monotonic() + 2}), timeout=12)
            if not result.get('ok'):
                raise RuntimeError(result.get('error', 'Microphone check failed.'))
            return result['level']
        self.ctx.tasks.run(work, done, self.failed)

    def failed(self, exc):
        self.pending = False
        self.ctx.settings.audio_check.set_sensitive(True)
        self.ctx.settings.audio_refresh.set_sensitive(True)
        self.ctx.settings.microphone.set_sensitive(True)
        self.ctx.settings.audio_level.set_subtitle(str(exc))

    def selection_changed(self, *_args):
        self.ctx.settings.audio_meter.set_value(0)
        self.ctx.settings.audio_level.set_subtitle(self.ctx.state.t('audio.check_hint'))
