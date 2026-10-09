"""Localized recovery guidance; raw backend messages remain available separately."""
from gi.repository import Gtk


def recovery_text(t, detail, category=None):
    text = str(detail).lower()
    if category == 'daemon':
        return t('health.daemon_unavailable'), t('health.retry_hint')
    if category == 'config':
        key = 'config'
    elif any(word in text for word in ('pipewire', 'pw-record', 'microphone', 'recorder')):
        key = 'recorder'
    elif 'timeout' in text or 'timed out' in text or 'exceeded' in text:
        key = 'timeout'
    elif any(word in text for word in ('clipboard', 'wl-copy', 'wl-clipboard', 'ydotool', 'paste')):
        key = 'clipboard'
    else:
        key = 'backend'
    return t(f'health.{key}_error'), t(f'health.{key}_hint')


def paint_health(ctx, detail='', category=None, warning=False):
    home = ctx.home
    if detail or category:
        reason, hint = recovery_text(ctx.state.t, detail, category)
        home.health_summary.set_text(ctx.state.t('health.warning' if warning else 'health.attention'))
        home.health_detail.set_text(f'{reason}\n{hint}')
    else:
        home.health_detail.set_text('')
    home.health_detail.remove_css_class('warning-text' if not warning else 'error-text')
    if detail or category:
        home.health_detail.add_css_class('warning-text' if warning else 'error-text')
    else:
        home.health_detail.remove_css_class('error-text')
        home.health_detail.remove_css_class('warning-text')
    if hasattr(home, 'health_raw'):
        home.health_raw.set_text(str(detail))
        home.health_expander.set_visible(bool(detail))


def microphone_accessibility(ctx, state):
    """Update names only on semantic transitions, not on every elapsed-time tick."""
    name = {'recording': 'stop', 'busy': 'cancel', 'ready': 'record',
            'preparing': 'preparing'}.get(state, 'unavailable')
    label = ctx.state.t(f'mic.{name}')
    ctx.home.mic_button.set_tooltip_text(label)
    ctx.home.mic_button.update_property(
        [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION], [label, ctx.state.t('home.button_clipboard') if state == 'ready' else label])


def show_operation_error(ctx, detail, category=None):
    """Keep the original failure available while showing recovery guidance."""
    ctx.status._action_error = str(detail)
    reason, _hint = recovery_text(ctx.state.t, detail, category)
    ctx.window._toast(reason)
    if hasattr(ctx.home, 'health_summary'):
        paint_health(ctx, detail, category)
