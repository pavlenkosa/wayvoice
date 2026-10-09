"""Ephemeral transcript actions: clipboard copy and explicit daemon text clearing."""
from ... import injector
from ...cli import request


class TranscriptController:
    def __init__(self, context):
        self.ctx = context
        self.text = ''
        self.pending = None
        self.epoch = 0

    def paint(self, text, epoch=None):
        if epoch is not None and epoch != self.epoch:
            return
        if self.pending == 'clear':
            return
        self.text = str(text)
        home = self.ctx.home
        home.last_text.set_text(self.text or self.ctx.state.t('transcript.empty'))
        if self.text:
            home.last_text.remove_css_class('muted')
        else:
            home.last_text.add_css_class('muted')
        home.transcript_meta.set_text(self.ctx.state.t('transcript.last') if self.text else '')
        self._buttons()

    def _buttons(self):
        for name in ('transcript_copy', 'transcript_clear'):
            if hasattr(self.ctx.home, name):
                button = getattr(self.ctx.home, name)
                operation = 'copy' if name == 'transcript_copy' else 'clear'
                label = f'transcript.{operation}ing' if self.pending == operation else f'transcript.{operation}'
                button.set_label(self.ctx.state.t(label))
                button.set_sensitive(bool(self.text) and not self.pending)

    def copy(self, *_args):
        if self.pending or not self.text:
            return
        text, language = self.text, self.ctx.state.ui_lang
        self.pending = 'copy'
        self._buttons()
        self.ctx.tasks.run(lambda: injector.copy_to_clipboard(text, language),
                           lambda _: self._copied(), lambda exc: self._failed('copy', exc))

    def _copied(self):
        self.pending = None
        self._buttons()
        self.ctx.window._toast(self.ctx.state.t('transcript.copied'))

    def clear(self, *_args):
        if self.pending or not self.text:
            return
        self.epoch += 1
        self.pending = 'clear'
        self._buttons()
        self.ctx.tasks.run(lambda: request('clear-text', timeout=0.8), self._cleared,
                           lambda exc: self._failed('clear', exc))

    def _cleared(self, reply):
        if not reply.get('ok'):
            self._failed('clear', reply.get('error') or '')
            return
        self.epoch += 1
        self.pending = None
        self.paint('')

    def _failed(self, operation, exc):
        self.pending = None
        self._buttons()
        self.ctx.window._toast(self.ctx.state.t(f'transcript.{operation}_failed', detail=str(exc)))
