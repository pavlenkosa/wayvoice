"""Convenience choices edit existing settings, without adding configuration state."""
from ...engine import engine_ids
from ...models import preset_index
from ..settings_values import DEVICES

PROFILES = {'fast': 'tiny', 'balanced': 'small', 'accurate': 'medium'}


class ProfilesController:
    def __init__(self, context):
        self.ctx = context

    def select(self, name):
        model = PROFILES[name]
        page = self.ctx.settings
        # Batch the draft: normal model selection may offer a download or warm
        # weights. A convenience preset explicitly promises to do neither.
        self.ctx.models._download_confirmation_for = None
        page.engine.handler_block(page.engine_selection_handler)
        page.model.handler_block(page.model_selection_handler)
        try:
            page.engine.set_selected(engine_ids().index('faster-whisper'))
            page.model.set_selected(preset_index(model))
            page.device.set_selected(DEVICES.index('cpu'))
        finally:
            page.model.handler_unblock(page.model_selection_handler)
            page.engine.handler_unblock(page.engine_selection_handler)
        self.ctx.preferences._on_engine_selected()
        self.ctx.preferences._refresh_save_state()
        self.ctx.window._toast(self.ctx.state.t('profiles.selected'))
