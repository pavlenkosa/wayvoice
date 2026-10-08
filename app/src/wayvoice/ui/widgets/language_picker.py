"""Language picker widget extracted from wayvoice.ui."""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ...i18n import tr
from ... import languages
from ...engine import engine_ids, engine_label


def language_choices(ui_lang: str) -> tuple[list[str], list[str]]:
    """Recognition-language selector contents: codes with their labels.

    Detection comes first and is not a language, so it gets its own translated label;
    the rest are named by the languages themselves.
    """
    codes = [languages.AUTO, *languages.CODES]
    labels = [tr("language.auto", ui_lang)]
    labels += [languages.display_name(code, ui_lang) for code in languages.CODES]
    return codes, labels


def engine_choices() -> tuple[list[str], list[str]]:
    """Engine selector contents: ids with their labels.

    Both lists come from the engine registry in registration order, so the
    dropdown cannot drift away from what the daemon actually accepts.
    """
    ids = engine_ids()
    return ids, [engine_label(engine_id) for engine_id in ids]


def _enable_dropdown_search(dropdown) -> bool:
    """Give a :class:`Gtk.DropDown` its search entry, best match mode.

    A hundred languages are not browsable by scrolling, so the user has to be able to
    type "deutsch", "german" or "de" and see what survives. The entry arrived in GTK 4.6
    and the substring match mode in 4.10, under an enum 4.16 renamed, so each piece is
    probed: a GTK that lacks them gets a longer popup rather than a traceback.
    """
    if not hasattr(dropdown, "set_enable_search"):
        return False
    try:
        dropdown.set_enable_search(True)
    except (TypeError, AttributeError, ValueError):
        return False
    for enum_name, member in (("StringFilterMatchMode", "SUBSTRING"), ("SearchMatchMode", "SEARCH_ALL")):
        mode = getattr(getattr(Gtk, enum_name, None), member, None)
        if mode is not None:
            try:
                dropdown.set_search_match_mode(mode)
            except (TypeError, AttributeError, ValueError):
                pass
            break
    return True


class LanguagePicker:
    """The recognition-language control, whichever GTK gave us.

    With a searchable :class:`Gtk.DropDown` a hundred languages are a two-keystroke
    affair. Without one there is only :class:`Adw.ComboRow`, which still works and still
    lists everything, so the settings around it do not have to care which was built.
    """

    def __init__(self, title: str, codes: list[str], labels: list[str], selected: int):
        self.codes = list(codes)
        self._index = selected if 0 <= selected < len(codes) else 0
        dropdown = Gtk.DropDown()
        dropdown.set_model(Gtk.StringList.new(labels))
        dropdown.set_valign(Gtk.Align.CENTER)
        if _enable_dropdown_search(dropdown):
            self._dropdown = dropdown
            self.row = Adw.ActionRow(title=title)
            self.row.add_suffix(dropdown)
        else:
            self._dropdown = None
            combo = Adw.ComboRow(title=title)
            combo.set_model(Gtk.StringList.new(labels))
            self.row = combo
        self.set_selected(self._index)

    def _control(self):
        return self._dropdown if self._dropdown is not None else self.row

    # Interface shared by both controls, so callers do not branch.

    def get_selected(self) -> int:
        """Index the user actually picked.

        Read back from the control rather than from the value last written: a
        :class:`Gtk.DropDown` changes its own selection without telling anyone, so a cached
        index would report whatever was selected before the user touched the row.
        """
        try:
            index = int(self._control().get_selected())
        except (TypeError, ValueError):
            index = self._index
        if 0 <= index < len(self.codes):
            self._index = index
        return self._index

    def set_selected(self, index: int) -> None:
        self._index = index if 0 <= index < len(self.codes) else 0
        self._control().set_selected(self._index)

    def set_sensitive(self, sensitive: bool) -> None:
        self.row.set_sensitive(sensitive)
        if self._dropdown is not None:
            self._dropdown.set_sensitive(sensitive)

    def selected_code(self) -> str:
        return self.codes[self.get_selected()]

    def select_code(self, code: str) -> bool:
        """Select ``code``; unknown values fall back to detection.

        Says whether the code was in the list at all, which ``normalize`` hides: it maps
        anything unknown to ``auto``, so comparing the selection afterwards would answer
        "yes" for a language that does not exist.
        """
        wanted = str(code or "").strip().lower()
        found = wanted in self.codes
        self.set_selected(self.codes.index(wanted) if found else 0)
        return found