"""Explicit, window-owned external delivery check; never records or transcribes."""
import math
import threading
import time
from gi.repository import Gtk
from .. import injector
from ..config import load_config
from .async_tasks import TaskRunner
from .widgets.labels import close_on_escape

TEST_TEXT = 'WayVoice: Привет, мир! Hello 123'
MODES = ('standard', 'terminal', 'copy')


class DeliveryCheck:
    """Small lifecycle owner, separated from the view for cancellation tests."""
    def __init__(self, tasks, changed, publish):
        self.tasks, self.changed, self.publish = tasks, changed, publish
        self.state = 'idle'
        self.seconds = 0
        self.warning = ''
        self.result = None
        self.cancel_event = threading.Event()
        self.closed = False
        self._timer_active = False

    @property
    def running(self):
        return self.state in {'countdown', 'sending'}

    def start(self, cfg, mode):
        if self.closed or self.running or mode not in MODES:
            return
        self.cfg = dict(cfg, paste_mode=mode)
        self.mode = mode
        self.cancel_event = threading.Event()
        self.result = None
        self.warning = ''
        self.state = 'countdown'
        self.deadline = time.monotonic() + 5
        self.tick()
        if not self._timer_active:
            self._timer_active = True
            self.tasks.every(250, self.tick)

    def tick(self):
        if self.closed or self.state != 'countdown':
            self._timer_active = False
            return False
        self.seconds = max(0, math.ceil(self.deadline - time.monotonic()))
        if self.seconds:
            self.changed()
            return True
        self._timer_active = False
        self.state = 'sending'
        self.changed()
        if not self.tasks.run(lambda: injector.inject(TEST_TEXT, self.cfg, cancel_event=self.cancel_event),
                              self.finished, self.failed):
            self.failed(RuntimeError('Could not start delivery check'))
        return False

    def _record(self, outcome, route='unknown'):
        self.result = {'outcome': outcome, 'mode': self.mode, 'route': route, 'at': time.time()}
        self.publish(dict(self.result))

    def finished(self, result):
        if self.closed:
            return
        self.warning = result.warning
        self.state = 'command_sent' if result.pasted else 'copied'
        self._record(self.state, 'auto' if result.pasted else 'clipboard')
        self.changed()

    def failed(self, exc):
        if self.closed:
            return
        self.state = 'cancelled' if isinstance(exc, injector.InjectionCancelled) else 'failed'
        self.warning = '' if self.state == 'cancelled' else str(exc)
        self._record(self.state)
        self.changed()

    def cancel(self):
        self.cancel_event.set()
        if self.state == 'countdown':
            self.state = 'cancelled'
            self._record('cancelled')
            self.changed()
        # A sending operation resolves from its actual result, not this request.

    def confirm(self, received):
        if self.closed or self.state not in {'command_sent', 'copied'}:
            return
        self.state = 'confirmed' if received else 'not_received'
        self._record(self.state, self.result['route'])
        self.changed()

    def close(self):
        if self.closed:
            return
        if self.running:
            self.cancel_event.set()
            # Clipboard/keys may already have been sent by the worker.
            self._record('interrupted' if self.state == 'sending' else 'cancelled')
        self.closed = True
        self.tasks.close()


def show_delivery_test(parent):
    existing = getattr(parent, '_delivery_dialog', None)
    if existing is not None:
        existing.present()  # Explicit user action only; completion never presents.
        return existing
    t = parent.t
    app = parent.get_application()
    saved = load_config()
    language = saved.get('ui_language', 'auto')
    if hasattr(parent, 'language'):
        language = parent.language
    saved = dict(saved, ui_language=language)
    win = Gtk.Window(title=t('delivery_test.title'), transient_for=parent,
                     modal=True, destroy_with_parent=True)
    win.set_default_size(500, 480)
    parent._delivery_dialog = win
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                  margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
    notice = Gtk.Label(label=t('delivery_test.notice'), wrap=True, xalign=0)
    box.append(notice)
    box.append(Gtk.Label(label=TEST_TEXT, selectable=True, wrap=True, xalign=0))
    mode = Gtk.DropDown(model=Gtk.StringList.new([t('delivery_test.'+m) for m in MODES]))
    mode.set_selected(MODES.index(saved.get('paste_mode')) if saved.get('paste_mode') in MODES else 0)
    mode.update_property([Gtk.AccessibleProperty.LABEL], [t('delivery_test.mode')])
    box.append(mode)
    status = Gtk.Label(label=t('delivery_test.idle'), wrap=True, xalign=0, selectable=True)
    box.append(status)
    previous = getattr(app, 'delivery_test_result', None)
    if previous:
        box.append(Gtk.Label(label=t('delivery_test.previous', mode=t('delivery_test.'+previous['mode']),
            outcome=t('delivery_test.'+previous['outcome']),
            when=time.strftime('%H:%M:%S', time.localtime(previous['at']))), wrap=True, xalign=0))
    start = Gtk.Button(label=t('delivery_test.start'))
    start.add_css_class('suggested-action')
    cancel = Gtk.Button(label=t('common.cancel'))
    yes = Gtk.Button(label=t('delivery_test.yes'))
    no = Gtk.Button(label=t('delivery_test.no'))
    close = Gtk.Button(label=t('common.close'))
    for button in (start, cancel, yes, no, close):
        box.append(button)

    def publish(result):
        app.delivery_test_result = result

    def changed():
        state = check.state
        text = t('delivery_test.'+state, seconds=check.seconds)
        if check.warning:
            text += '\n'+check.warning
        status.set_text(text)
        start.set_sensitive(not check.running)
        mode.set_sensitive(not check.running)
        cancel.set_visible(check.running)
        yes.set_visible(state in {'command_sent', 'copied'})
        no.set_visible(state in {'command_sent', 'copied'})

    check = DeliveryCheck(TaskRunner(), changed, publish)
    win.check = check
    start.connect('clicked', lambda *_: check.start(saved, MODES[mode.get_selected()]))
    cancel.connect('clicked', lambda *_: check.cancel())
    yes.connect('clicked', lambda *_: check.confirm(True))
    no.connect('clicked', lambda *_: check.confirm(False))
    close.connect('clicked', lambda *_: win.close())
    def dispose(*_):
        check.close()
        parent._delivery_dialog = None
        return False
    win.connect('close-request', dispose)
    win.connect('unrealize', dispose)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroll.set_child(box)
    win.set_child(scroll)
    close_on_escape(win)
    changed()
    win.present()
    return win
