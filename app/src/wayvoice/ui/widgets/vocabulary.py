"""Bounded local dictionary editor and preview of the actual text pipeline."""
from gi.repository import Adw, Gtk, Pango
from ...vocabulary import parse_rules


class VocabularyEditor:
    def __init__(self, ctx):
        self.ctx = ctx
        t = ctx.state.t
        self.root = Adw.PreferencesGroup(title=t('vocab.title'), description=t('vocab.hint'))
        self.enabled = Adw.SwitchRow(title=t('vocab.enabled'))
        self.enabled.set_active(bool(ctx.state.cfg.get('vocabulary_enabled', False)))
        self.root.add(self.enabled)
        self.buffer = Gtk.TextBuffer()
        self.buffer.set_text(str(ctx.state.cfg.get('vocabulary_rules', '')))
        self.buffer.connect('insert-text', self._limit)
        editor = self.editor = Gtk.TextView(buffer=self.buffer, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                              top_margin=8, bottom_margin=8, left_margin=10, right_margin=10)
        editor.set_tooltip_text(t('vocab.example'))
        scroll = Gtk.ScrolledWindow(min_content_height=120, max_content_height=180, propagate_natural_height=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(editor)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8)
        box.append(Gtk.Label(label=t('vocab.example'), xalign=0, wrap=True))
        box.append(scroll)
        self.sample = Gtk.Entry(placeholder_text=t('vocab.sample'), max_length=2000, hexpand=True)
        button = Gtk.Button(label=t('vocab.preview'))
        button.connect('clicked', self.preview)
        row = Gtk.Box(spacing=8)
        row.append(self.sample)
        row.append(button)
        box.append(row)
        self.result = Gtk.Label(label=t('vocab.empty'), xalign=0, wrap=True, selectable=True)
        self.result.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.result.set_max_width_chars(80)
        editor.update_property([Gtk.AccessibleProperty.LABEL], [t('vocab.title')])
        result_scroll = Gtk.ScrolledWindow(max_content_height=140, propagate_natural_height=True)
        result_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        result_scroll.set_child(self.result)
        box.append(result_scroll)
        self.root.add(box)

    def _limit(self, buffer, _where, text, _length):
        if buffer.get_char_count() + len(text) > 16384:
            buffer.stop_emission_by_name('insert-text')
            self.result.set_text(self.ctx.state.t('vocab.limit'))

    def rules_text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), True)

    def validate(self):
        try:
            parse_rules(self.rules_text())
        except ValueError as exc:
            self.result.set_text(str(exc))
            self.editor.update_property([Gtk.AccessibleProperty.DESCRIPTION], [str(exc)])
            self.editor.grab_focus()
            return False
        self.editor.reset_property(Gtk.AccessibleProperty.DESCRIPTION)
        return True

    def preview(self, *_args):
        if not self.validate():
            return
        from ...engine import _postprocess
        cfg = self.ctx.preferences._draft_values()
        try:
            result = _postprocess(self.sample.get_text(), cfg)
        except ValueError as exc:
            self.result.set_text(str(exc))
            return
        self.result.set_text(result or self.ctx.state.t('vocab.empty'))
        self.result.set_tooltip_text(self.ctx.state.t('vocab.off') if not self.enabled.get_active() else None)
