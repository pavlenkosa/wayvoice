"""Settings shared across pages; operation state stays in controllers."""
from dataclasses import dataclass, field
from typing import Any
from ..i18n import resolve_language, tr


@dataclass
class UiState:
    cfg: dict[str, Any]
    ui_lang_setting: str = field(init=False)
    ui_lang: str = field(init=False)
    shortcut_binding: str = field(init=False)

    def __post_init__(self):
        self.ui_lang_setting = str(self.cfg.get('ui_language', 'auto'))
        self.ui_lang = resolve_language(self.ui_lang_setting)
        self.shortcut_binding = str(self.cfg.get('shortcut', 'F8'))

    def t(self, key, **kwargs):
        return tr(key, self.ui_lang, **kwargs)

    def duration_label(self, seconds):
        if seconds < 60:
            return f'{seconds} s'
        return f'{seconds // 60} min' if self.ui_lang == 'en' else f'{seconds // 60} мин'


@dataclass
class UiContext:
    """Explicit composition links; no dynamic attribute delegation."""
    window: Any
    state: UiState
    tasks: Any
    home: Any = None
    settings: Any = None
    models: Any = None
    integration: Any = None
    status: Any = None
    preferences: Any = None
    shortcut: Any = None
