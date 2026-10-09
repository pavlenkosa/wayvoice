"""Readiness guidance from existing snapshots; no probes or implicit setup."""


def setup_steps(reply, cfg, missing_deps):
    """Return (target, message key) without claiming a live dictation was verified."""
    if not reply.get('ok'):
        return [('engine_status_row', 'setup.waiting')]
    engine = reply.get('engine') or {}
    model = reply.get('model') or {}
    download = model.get('download') or {}
    ids = {dep.id for dep in missing_deps}
    model_pending = download.get('state') in {'downloading', 'warming'} and (
        str(download.get('model') or '') == str(model.get('model') or ''))
    recognition = 'setup.runtime_ready' if engine.get('state') == 'ready' else 'setup.runtime_missing'
    if model.get('supported'):
        weights = ('setup.model_pending' if model_pending else
                   'setup.model_ready' if model.get('present') else 'setup.model_missing')
    else:
        weights = 'setup.model_external'
    capture = 'setup.capture_missing' if 'pipewire' in ids else 'setup.capture_available'
    delivery = 'setup.clipboard_missing' if 'wl-clipboard' in ids else 'setup.clipboard_available'
    shortcut = ('setup.shortcut_disabled' if not cfg.get('shortcut') else
                'setup.shortcut_configured' if (reply.get('shortcut_support') or (False, ''))[0]
                else 'setup.shortcut_manual')
    return [('engine_status_row', recognition), ('model_state_row', weights),
            ('dependencies', capture), ('paste', delivery), ('shortcut_row', shortcut)]


def model_missing(reply):
    model = reply.get('model') or {}
    return bool(model.get('supported') and not model.get('present'))


def paint_setup(ctx, reply, cfg, missing_deps):
    if not hasattr(ctx.home, 'setup_rows'):
        return
    for target, message in setup_steps(reply, cfg, missing_deps):
        if target in ctx.home.setup_rows:
            ctx.home.setup_rows[target].set_subtitle(ctx.state.t(message))
    if not reply.get('ok'):
        for target, row in ctx.home.setup_rows.items():
            if target != 'engine_status_row':
                row.set_subtitle(ctx.state.t('health.checking'))
