"""First-run window. Downloads are explicit and owned by this window's job."""
import threading

from gi.repository import Adw, Gtk

from .. import onboarding
from ..config import load_config
from ..engine import TranscriptionCancelled
from ..i18n import SUPPORTED_UI_LANGUAGES, tr, ui_language_labels
from .localization import set_text_direction
from ..model_store import human_size
from ..shortcut import manual_shortcut_required, manual_shortcut_hint, portal_shortcut_desktop
from .async_tasks import TaskRunner


class OnboardingWindow(Adw.ApplicationWindow):
    def __init__(self, app, finished):
        super().__init__(application=app, title='WayVoice')
        self.set_default_size(660, 700)
        self.set_size_request(360, 360)
        self.tasks = TaskRunner()
        self.finished = finished
        saved = load_config()
        self._expected_config = dict(saved)
        self.language = saved.get('ui_language', 'auto')
        if self.language not in SUPPORTED_UI_LANGUAGES:
            self.language = 'auto'
        self.model = saved.get('model', 'small') if saved.get('onboarding_completed') is False else 'small'
        if self.model not in onboarding.MODELS:
            self.model = 'small'
        self.step = 0
        self.busy = False
        self.prepared = False
        self.cancel_event = threading.Event()
        self._close_pending = False
        self.connect('close-request', self._close_requested)
        self.connect('unrealize', self._dispose_ui)
        self._render()

    def t(self, key, **values):
        return tr('onboard.' + key, self.language, **values)

    def _label(self, text, style=None):
        label = Gtk.Label(label=text, wrap=True, xalign=0.5, justify=Gtk.Justification.CENTER)
        if style:
            label.add_css_class(style)
        return label

    def _button(self, text, callback, primary=False):
        button = Gtk.Button(label=text)
        if primary:
            button.add_css_class('suggested-action')
            button.add_css_class('pill')
        button.connect('clicked', callback)
        self.body.append(button)
        return button

    def _render(self):
        set_text_direction(self, self.language)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label='WayVoice'))
        if self.step in (1, 2) and not self.busy:
            back = Gtk.Button(icon_name='go-previous-symbolic', tooltip_text=self.t('back'))
            back.connect('clicked', lambda *_: self._go(self.step - 1))
            header.pack_start(back)
        toolbar.add_top_bar(header)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=420)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                            margin_start=24, margin_end=24, margin_top=24, margin_bottom=24)
        clamp.set_child(self.body)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)
        self.set_content(toolbar)
        self.body.append(self._label(self.t('step', number=self.step + 1), 'dim-label'))
        icon = Gtk.Image.new_from_icon_name('audio-input-microphone-symbolic' if self.step == 0 else
                                          'emblem-ok-symbolic' if self.step == 3 else 'folder-download-symbolic')
        icon.set_pixel_size(64)
        self.body.append(icon)
        title = ('welcome', 'models', 'preparing' if self.busy else 'prepare',
                 'ready' if self.prepared else 'later')[self.step]
        self.body.append(self._label(self.t(title), 'title-1'))
        if self.step == 0:
            self._language_page()
        elif self.step == 1:
            self._models_page()
        elif self.step == 2:
            self._prepare_page()
        else:
            self._ready_page()

    def _language_page(self):
        self.body.append(self._label(self.t('welcome_detail')))
        group = Adw.PreferencesGroup()
        row = Adw.ComboRow(title=self.t('language'))
        row.set_model(Gtk.StringList.new(ui_language_labels(self.language)))
        codes = SUPPORTED_UI_LANGUAGES
        row.set_selected(codes.index(self.language))
        def changed(*_):
            self.language = codes[row.get_selected()]
            self._render()
        row.connect('notify::selected', changed)
        group.add(row)
        self.body.append(group)
        self.body.append(self._label(self.t('language_detail'), 'dim-label'))
        self.primary = self._button(self.t('continue'), lambda *_: self._go(1), True)

    def _models_page(self):
        self.body.append(self._label(self.t('models_detail')))
        group = Adw.PreferencesGroup()
        first = None
        for model in onboarding.MODELS:
            size = human_size(onboarding.MODEL_DOWNLOAD_BYTES[model], self.language)
            row = Adw.ActionRow(title=self.t(model),
                                subtitle=self.t(model + '_detail') + '\n' + self.t('size', size=size))
            radio = Gtk.CheckButton()
            radio.update_property([Gtk.AccessibleProperty.LABEL], [self.t(model)])
            if first is not None:
                radio.set_group(first)
            else:
                first = radio
            radio.set_active(self.model == model)
            radio.connect('toggled', self._model_selected, model)
            row.add_prefix(radio)
            row.set_activatable_widget(radio)
            group.add(row)
        self.body.append(group)
        self.body.append(self._label(self.t('no_download'), 'dim-label'))
        self.primary = self._button(self.t('continue'), lambda *_: self._go(2), True)

    def _model_selected(self, button, model):
        if button.get_active():
            self.model = model

    def _prepare_page(self):
        self.body.append(self._label(self.t(self.model)))
        self.body.append(self._label(self.t('size', size=human_size(
            onboarding.MODEL_DOWNLOAD_BYTES[self.model], self.language)), 'dim-label'))
        self.body.append(self._label(self.t('network')))
        self.phase_label = self._label(self.t('runtime') if self.busy else self.t('confirm'))
        self.body.append(self.phase_label)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_visible(self.busy)
        self.body.append(self.progress)
        self.error = self._label('')
        self.error.set_selectable(True)
        self.error.add_css_class('error')
        self.body.append(self.error)
        if self.busy:
            self.spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
            self.body.append(self.spinner)
            self.cancel_button = self._button(self.t('cancel'), self._cancel)
            self.body.append(self._label(self.t('close_detail'), 'dim-label'))
        else:
            self.primary = self._button(self.t('install'), self._start, True)
            self._button(self.t('defer'), lambda *_: self._go(3))

    def _ready_page(self):
        self.body.append(self._label(self.t('ready_detail' if self.prepared else 'later_detail')))
        if manual_shortcut_required():
            guidance = self._label(self.t('kde_shortcut') if portal_shortcut_desktop()
                                   else manual_shortcut_hint(self.language))
            guidance.set_selectable(True)
            self.body.append(guidance)
        else:
            self.body.append(self._label(self.t('shortcut')))
        self.body.append(self._label(self.t('clipboard'), 'dim-label'))
        self.error = self._label('')
        self.error.set_selectable(True)
        self.body.append(self.error)
        self.primary = self._button(self.t('open'), self._finish, True)

    def _go(self, step):
        if not self.busy:
            self.step = step
            self._render()

    def _start(self, *_):
        if self.busy:
            return
        self.busy = True
        self.cancel_event = threading.Event()
        self._render()
        language, model, event = self.language, self.model, self.cancel_event
        expected = dict(self._expected_config)
        def work():
            cfg = onboarding.save_selection(language, model, completed=False, expected=expected)
            accepted = onboarding.selection_changes(language, model, False, False, expected)
            self.tasks.idle(self._selection_saved, accepted)
            return onboarding.prepare_selection(cfg, event,
                lambda done, total: self.tasks.idle(self._progress, done, total),
                lambda phase: self.tasks.idle(self._phase_changed, phase))
        self.tasks.run(work, self._prepared, self._failed)

    def _selection_saved(self, accepted):
        # Publication runs on the GTK loop even if subsequent preparation fails.
        self._expected_config.update(accepted)

    def _phase_changed(self, phase):
        self.phase_label.set_text(self.t(phase))
        self.progress.set_text('')
        self.progress.set_fraction(0)

    def _progress(self, done, total):
        if total > 0:
            self.progress.set_fraction(min(1, max(0, done / total)))
            self.progress.set_text(f'{human_size(done, self.language)} / {human_size(total, self.language)}')
        else:
            self.progress.pulse()
            self.progress.set_text(human_size(done, self.language))

    def _prepared(self, _result):
        self.busy = False
        if self._close_pending:
            self.close()
            return
        self.prepared = True
        self._go(3)

    def _failed(self, exc):
        self.busy = False
        if self._close_pending:
            self.close()
            return
        self._render()
        self.error.set_text(self.t('cancelled') if isinstance(exc, TranscriptionCancelled) else self.t('failed', reason=str(exc)))

    def _cancel(self, *_):
        self.cancel_event.set()
        self.cancel_button.set_sensitive(False)
        self.phase_label.set_text(self.t('cancelling'))

    def _finish(self, *_):
        if self.busy:
            return
        self.busy = True
        self.primary.set_sensitive(False)
        language, model, deferred = self.language, self.model, not self.prepared
        expected = dict(self._expected_config)
        self.tasks.run(lambda: onboarding.save_selection(language, model, completed=True, deferred=deferred, expected=expected),
                       self._finished, self._finish_failed)

    def _finished(self, _cfg):
        self.busy = False
        if self._close_pending:
            self.close()
        else:
            try:
                self.finished(self)
            except Exception as exc:
                self._finish_failed(exc)

    def _finish_failed(self, exc):
        self.busy = False
        if self._close_pending:
            self.close()
            return
        self.primary.set_sensitive(True)
        self.error.set_text(self.t('failed', reason=str(exc)))

    def _close_requested(self, *_):
        if self.busy:
            self._close_pending = True
            self.cancel_event.set()
            if hasattr(self, 'phase_label'):
                self.phase_label.set_text(self.t('cancelling'))
            return True
        self._dispose_ui()
        return False

    def _dispose_ui(self, *_):
        self.cancel_event.set()
        self.tasks.close()
