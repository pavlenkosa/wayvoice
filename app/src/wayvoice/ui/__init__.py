"""Compatibility imports and launcher for the decomposed settings UI."""
from .window import WayVoiceWindow
from .application import App, main
from .style import CSS
from .widgets.language_picker import LanguagePicker, engine_choices, language_choices
from .. import __version__

__all__ = ['App', 'WayVoiceWindow', 'main', 'CSS', 'LanguagePicker', 'engine_choices', 'language_choices']
