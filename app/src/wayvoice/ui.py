from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import __version__
from . import deps as deps_mod
from . import injector
from . import languages
from . import pkgsys
from . import service
from .cli import request
from .config import load_config, number, save_config
from .engine import (
    DEFAULT_ENGINE,
    engine_from_config,
    engine_ids,
    engine_label,
    engine_status,
    get_engine,
    request_engine_setup,
    stop_worker,
    worker_info,
)
from .i18n import resolve_language, tr
from . import model_store
from .models import MODEL_PRESETS, PRESET_LABELS, display_name, forced_language, preset_index, preset_subtitle
from .paths import command_path, setup_user_script
from .shortcut import apply_shortcut, label_for

DEVICES = ["auto", "cpu", "cuda"]
DEVICE_NAMES = ["Auto", "CPU", "NVIDIA CUDA"]
PASTE_MODES = ["standard", "terminal", "copy"]
TIMEOUT_VALUES = [30, 60, 90, 120, 180]
RECORD_VALUES = [30, 60, 120, 300, 600]
UI_LANGUAGE_IDS = ["auto", "ru", "en"]

CSS = r"""
.window-root { background: @window_bg_color; }
.content-wrap { padding: 24px 30px 34px 30px; }
.hero-card, .surface-card, .transcript-card, .health-card {
  background: alpha(@card_bg_color, 0.97);
  border: 1px solid alpha(@window_fg_color, 0.08);
  border-radius: 22px;
}
.hero-card { padding: 30px; }
.surface-card { padding: 18px; }
.transcript-card, .health-card { padding: 18px 20px; }
.hero-title { font-size: 24px; font-weight: 800; }
.hero-subtitle { font-size: 14px; color: alpha(@window_fg_color, 0.66); }
.section-title { font-size: 16px; font-weight: 700; }
.muted { color: alpha(@window_fg_color, 0.60); }
.metric-value { font-size: 15px; font-weight: 700; }
.mic-button {
  min-width: 92px; min-height: 92px; border-radius: 999px; padding: 0;
  background: @accent_bg_color; color: @accent_fg_color; border: none;
}
.mic-button.recording { background: @error_bg_color; color: @error_fg_color; }
.mic-button.busy { background: @warning_bg_color; color: @warning_fg_color; }
.status-pill {
  padding: 6px 11px; border-radius: 999px;
  background: alpha(@window_fg_color, 0.07); color: alpha(@window_fg_color, 0.72);
  font-size: 12px; font-weight: 700;
}
.status-pill.recording { background: alpha(@error_bg_color, 0.18); color: @error_color; }
.status-pill.busy { background: alpha(@warning_bg_color, 0.18); color: @warning_color; }
.status-pill.ready { background: alpha(@success_bg_color, 0.16); color: @success_color; }
.hotkey-pill { padding: 8px 12px; border-radius: 12px; background: alpha(@window_fg_color, 0.07); font-weight: 700; }
.kicker { font-size: 12px; font-weight: 800; letter-spacing: 0.08em; color: @accent_color; }
.warning-text { color: @warning_color; }
.error-text { color: @error_color; }
.capture-key { font-size: 24px; font-weight: 800; }
"""


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


class WayVoiceWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title("WayVoice")
        self.set_default_size(780, 760)
        self.set_size_request(640, 620)
        self.cfg = load_config()
        self.ui_lang_setting = str(self.cfg.get("ui_language", "auto"))
        self.ui_lang = resolve_language(self.ui_lang_setting)
        self._shortcut_binding = str(self.cfg.get("shortcut", "F8"))
        self._restart_requested = False
        self._ui_busy = False
        # Rows of the "Dependencies" group, keyed by dependency id. Each entry
        # is (row, button, spinner) and is refreshed from deps.status_all().
        self._dep_rows = {}
        self._dep_installing = set()
        #: The "Copy logs" button's work is on a thread, and the flag is what
        #: stops a second click from starting another one.
        self._logs_copying = False
        self._logs_button = None
        self._dep_last_refresh = 0.0
        # Model-files row: the cache is walked off the main loop, so a refresh
        # can be in flight while the user changes the selection.
        self._model_refresh_busy = False
        self._model_refresh_pending = False
        #: The model whose download is waiting for the user's answer. Set when a
        #: model is selected and cleared by the first state report that speaks
        #: about it, so a decision can never be asked for twice.
        self._download_confirmation_for: str | None = None
        self._model_deleting = False
        self._install_css()
        self._install_actions()

        self.toast = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        self.stack.add_titled_with_icon(self._build_home(), "home", self.t("nav.home"), "audio-input-microphone-symbolic")
        self.stack.add_titled_with_icon(self._build_settings(), "settings", self.t("nav.settings"), "preferences-system-symbolic")

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header = Adw.HeaderBar()
        header.set_title_widget(switcher)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text=self.t("nav.settings"))
        menu = Gio.Menu()
        menu.append(self.t("menu.diagnostics"), "win.diagnostics")
        menu.append(self.t("menu.about"), "win.about")
        menu.append(self.t("menu.quit"), "win.quit")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.toast.set_child(toolbar)
        self.toast.add_css_class("window-root")
        self.set_content(self.toast)

        GLib.idle_add(self._background_start)
        GLib.idle_add(self._model_state_start)
        GLib.timeout_add(650, self._poll_status)
        GLib.timeout_add(900, self._poll_engine_settings)

    def t(self, key: str, **kwargs) -> str:
        return tr(key, self.ui_lang, **kwargs)

    def _install_css(self):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _install_actions(self):
        for name, callback in (("about", self._show_about), ("diagnostics", self._copy_diagnostics), ("quit", self._quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    @staticmethod
    def _idx(values, value):
        try:
            return values.index(value)
        except ValueError:
            return 0

    @staticmethod
    def _label(text, css=None, *, wrap=False, xalign=0.0):
        label = Gtk.Label(label=text, xalign=xalign)
        label.set_wrap(wrap)
        if wrap:
            label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if css:
            label.add_css_class(css)
        return label

    def _apply_desktop_integration(self) -> None:
        """Run the per-user setup: raise the daemon, apply the shortcut and enable the
        ydotoold unit when ydotool is present.

        Also runs after a dependency is installed from the settings, since that integration
        is otherwise applied once, at package installation time. With a user manager the
        units and ``setup-user`` do it - the only way to enable the ydotoold unit - and
        without one the daemon is started directly and the shortcut applied through GSettings.
        See :mod:`wayvoice.service`.
        """
        # The daemon start and the shortcut both talk to the outside world and can block,
        # so they never run on the GTK main loop.
        threading.Thread(target=self._apply_desktop_integration_worker, daemon=True).start()

    def _apply_desktop_integration_worker(self) -> None:
        if not service.start_daemon():
            # Desktop integration is optional: never let it break the UI, but
            # keep the reason visible for bug reports.
            print("WayVoice: the background daemon did not come up.", file=sys.stderr)
        if service.systemd_available():
            setup_user = setup_user_script()
            if setup_user is None:
                print(
                    "WayVoice: setup-user script not found; skipping desktop integration.",
                    file=sys.stderr,
                )
                return
            try:
                subprocess.Popen([str(setup_user)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                print(f"WayVoice: failed to start {setup_user}: {exc}", file=sys.stderr)
            return
        ok, msg = service.apply_shortcut_now()
        if not ok:
            print(f"WayVoice: global shortcut not applied: {msg}", file=sys.stderr)

    def _background_start(self):
        self._apply_desktop_integration()
        return GLib.SOURCE_REMOVE

    def _model_state_start(self):
        self._refresh_model_state()
        return GLib.SOURCE_REMOVE

    def _build_home(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.add_css_class("content-wrap")
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_size_request(640, -1)
        scroller.set_child(outer)

        intro = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_hexpand(True)
        text.append(self._label("WAYVOICE", "kicker"))
        text.append(self._label(self.t("home.title"), "hero-title"))
        text.append(self._label(self.t("home.subtitle"), "hero-subtitle"))
        intro.append(text)
        self.status_pill = self._label(self.t("status.starting"), "status-pill")
        self.status_pill.set_valign(Gtk.Align.CENTER)
        intro.append(self.status_pill)
        outer.append(intro)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        hero.add_css_class("hero-card")
        self.mic_button = Gtk.Button()
        self.mic_button.add_css_class("mic-button")
        self.mic_button.set_halign(Gtk.Align.CENTER)
        self.mic_button.set_sensitive(False)
        self.mic_button.connect("clicked", self._toggle)
        self.mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        self.mic_icon.set_pixel_size(40)
        self.mic_button.set_child(self.mic_icon)
        hero.append(self.mic_button)
        self.hero_state = self._label(self.t("hero.starting"), "hero-title", xalign=0.5)
        self.hero_state.set_halign(Gtk.Align.CENTER)
        hero.append(self.hero_state)
        self.hero_caption = self._label("", "hero-subtitle", wrap=True, xalign=0.5)
        self.hero_caption.set_justify(Gtk.Justification.CENTER)
        self.hero_caption.set_halign(Gtk.Align.CENTER)
        self.hero_caption.set_max_width_chars(58)
        hero.append(self.hero_caption)
        shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        shortcut_box.set_halign(Gtk.Align.CENTER)
        shortcut_box.append(self._label(self.t("shortcut.global"), "muted"))
        self.hotkey_label = self._label(label_for(self._shortcut_binding), "hotkey-pill")
        shortcut_box.append(self.hotkey_label)
        hero.append(shortcut_box)
        outer.append(hero)

        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.set_column_homogeneous(True)
        self.engine_card = self._metric_card(grid, 0, self.t("card.engine"), engine_label(self.cfg.get("engine")) or "—", "applications-engineering-symbolic")
        self.model_card = self._metric_card(grid, 1, self.t("card.model"), display_name(str(self.cfg.get("model", "small"))), "applications-system-symbolic")
        self.paste_card = self._metric_card(grid, 2, self.t("card.paste"), "Ctrl+V", "edit-paste-symbolic")
        outer.append(grid)

        health = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        health.add_css_class("health-card")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(self._label(self.t("health.title"), "section-title"))
        self.health_summary = self._label(self.t("health.checking"), "muted")
        self.health_summary.set_hexpand(True)
        self.health_summary.set_halign(Gtk.Align.END)
        row.append(self.health_summary)
        health.append(row)
        self.health_detail = self._label("", "muted", wrap=True)
        health.append(self.health_detail)
        outer.append(health)

        transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        transcript.add_css_class("transcript-card")
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        h.append(self._label(self.t("transcript.title"), "section-title"))
        self.transcript_meta = self._label("", "muted")
        self.transcript_meta.set_hexpand(True)
        self.transcript_meta.set_halign(Gtk.Align.END)
        h.append(self.transcript_meta)
        transcript.append(h)
        self.last_text = self._label(self.t("transcript.empty"), "muted", wrap=True)
        self.last_text.set_selectable(True)
        transcript.append(self.last_text)
        outer.append(transcript)
        return scroller

    def _metric_card(self, grid, column, title, value, icon_name):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        card.add_css_class("surface-card")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(18)
        top.append(icon)
        top.append(self._label(title, "muted"))
        card.append(top)
        label = self._label(value, "metric-value")
        card.append(label)
        grid.attach(card, column, 0, 1, 1)
        return label

    def _build_settings(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page = Adw.PreferencesPage()
        page.set_halign(Gtk.Align.CENTER)
        page.set_size_request(640, -1)
        scroller.set_child(page)

        engine_group = Adw.PreferencesGroup(title=self.t("settings.recognition"))
        page.add(engine_group)
        engine_choice_ids, engine_choice_labels = engine_choices()
        self.engine = Adw.ComboRow(title=self.t("settings.engine"))
        self.engine.set_model(Gtk.StringList.new(engine_choice_labels))
        self.engine.set_selected(self._idx(engine_choice_ids, self.cfg.get("engine", DEFAULT_ENGINE)))
        self.engine.connect("notify::selected", self._on_engine_selected)
        engine_group.add(self.engine)

        self.engine_status_row = Adw.ActionRow(title=self.t("settings.engine_state"), subtitle=self.t("health.checking"))
        self.engine_setup_btn = Gtk.Button(label=self.t("settings.prepare"), valign=Gtk.Align.CENTER)
        self.engine_setup_btn.connect("clicked", self._setup_engine)
        self.engine_status_row.add_suffix(self.engine_setup_btn)
        self.engine_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.engine_spinner.set_visible(False)
        self.engine_status_row.add_suffix(self.engine_spinner)
        engine_group.add(self.engine_status_row)

        self.model = Adw.ComboRow(title=self.t("settings.model"))
        self.model.set_model(Gtk.StringList.new(PRESET_LABELS))
        self.model.set_selected(preset_index(str(self.cfg.get("model", "small"))))
        self.model.connect("notify::selected", self._on_model_selected)
        engine_group.add(self.model)

        self.custom_model = Adw.EntryRow(title=self.t("settings.custom_model"))
        current_model = str(self.cfg.get("model", "small"))
        custom_value = str(self.cfg.get("custom_model", ""))
        if preset_index(current_model) == len(MODEL_PRESETS) - 1 and current_model != "__custom__":
            custom_value = current_model
        self.custom_model.set_text(custom_value)
        # Typing in the custom field fires this per keystroke, so it goes through the
        # same coalescing as everything else instead of scanning per character.
        self.custom_model.connect("changed", lambda *_: self._refresh_model_state())
        engine_group.add(self.custom_model)

        self.model_state_row = Adw.ActionRow(title=self.t("store.state"), subtitle=self.t("health.checking"))
        # The download has to be reachable without changing the model: a model that is
        # not on disk used to be fetched as a side effect of choosing a different one,
        # which is a place a user goes to only by accident.
        self.model_fetch_btn = Gtk.Button(
            label=self.t("store.download_now"),
            valign=Gtk.Align.CENTER,
        )
        self.model_fetch_btn.connect("clicked", self._ask_to_fetch_the_model)
        self.model_fetch_btn.set_visible(False)
        self.model_state_row.add_suffix(self.model_fetch_btn)
        #: The last state the window heard about, so the button can be shown or hidden
        #: from either side: the cache walk says what is on disk, the daemon says what
        #: is being done about it.
        self._model_entry: dict = {}
        self._download_report: dict = {}
        self.model_delete_btn = Gtk.Button(
            label=self.t("common.delete"),
            valign=Gtk.Align.CENTER,
            sensitive=False,
        )
        self.model_delete_btn.add_css_class("destructive-action")
        self.model_delete_btn.connect("clicked", self._ask_delete_model)
        self.model_state_row.add_suffix(self.model_delete_btn)
        engine_group.add(self.model_state_row)

        # Downloading a model is the one thing here that takes minutes, so it gets its
        # own row with a real bar and a cancel button instead of a subtitle that would
        # have to be re-read to change.
        self.model_download_row = Adw.ActionRow(title=self.t("store.download"))
        self.model_download_bar = Gtk.ProgressBar(
            valign=Gtk.Align.CENTER,
            hexpand=True,
            show_text=False,
        )
        self.model_download_row.add_suffix(self.model_download_bar)
        self.model_download_cancel_btn = Gtk.Button(
            label=self.t("common.cancel"),
            valign=Gtk.Align.CENTER,
        )
        self.model_download_cancel_btn.connect("clicked", self._cancel_model_download)
        self.model_download_row.add_suffix(self.model_download_cancel_btn)
        self.model_download_row.set_visible(False)
        engine_group.add(self.model_download_row)

        self.model_disk_row = Adw.ActionRow(
            title=self.t("store.storage"),
            subtitle=self.t("store.disk", size="…", cache="…", free="…"),
        )
        engine_group.add(self.model_disk_row)

        self.device = Adw.ComboRow(title=self.t("settings.device"))
        self.device.set_model(Gtk.StringList.new(DEVICE_NAMES))
        self.device.set_selected(self._idx(DEVICES, self.cfg.get("device", "auto")))
        engine_group.add(self.device)
        self.vad = Adw.SwitchRow(title=self.t("settings.vad"))
        self.vad.set_active(bool(self.cfg.get("vad_filter", True)))
        engine_group.add(self.vad)
        self.worker = Adw.SwitchRow(title=self.t("settings.worker"), subtitle=self.t("settings.worker_sub"))
        self.worker.set_active(bool(self.cfg.get("engine_worker", True)))
        engine_group.add(self.worker)

        self.cpp_binary = Adw.EntryRow(title=self.t("settings.cpp_binary"))
        self.cpp_binary.set_text(str(self.cfg.get("whisper_cpp_binary", "")))
        engine_group.add(self.cpp_binary)
        self.cpp_model = Adw.EntryRow(title=self.t("settings.cpp_model"))
        self.cpp_model.set_text(str(self.cfg.get("whisper_cpp_model", "")))
        engine_group.add(self.cpp_model)
        self.cpp_gpu = Adw.SwitchRow(title=self.t("settings.cpp_gpu"))
        self.cpp_gpu.set_active(bool(self.cfg.get("whisper_cpp_gpu", True)))
        engine_group.add(self.cpp_gpu)

        self.custom_command = Adw.EntryRow(title=self.t("settings.custom_command"))
        self.custom_command.set_text(str(self.cfg.get("custom_command", "")))
        self.custom_command.set_tooltip_text(self.t("settings.custom_command_sub"))
        engine_group.add(self.custom_command)

        # Config key -> row for every engine-specific row. A row is shown exactly when
        # the selected engine owns its key, so a new engine brings its settings along
        # instead of needing an id comparison per row.
        self._engine_rows = (
            ("model", self.model),
            ("custom_model", self.custom_model),
            ("device", self.device),
            ("vad_filter", self.vad),
            ("engine_worker", self.worker),
            ("whisper_cpp_binary", self.cpp_binary),
            ("whisper_cpp_model", self.cpp_model),
            ("whisper_cpp_gpu", self.cpp_gpu),
            ("custom_command", self.custom_command),
        )

        text_group = Adw.PreferencesGroup(title=self.t("settings.text"))
        page.add(text_group)
        language_codes, language_labels = language_choices(self.ui_lang)
        self.language = LanguagePicker(
            self.t("settings.language"),
            language_codes,
            language_labels,
            # Old config.json files may hold anything at all here, including a
            # language Whisper no longer knows; normalize() turns all of that
            # into a code the list actually has.
            self._idx(language_codes, languages.normalize(self.cfg.get("language"))),
        )
        text_group.add(self.language.row)
        self.auto_punct = Adw.SwitchRow(title=self.t("settings.punctuation"))
        self.auto_punct.set_active(bool(self.cfg.get("auto_punctuation", True)))
        text_group.add(self.auto_punct)
        self.spoken = Adw.SwitchRow(title=self.t("settings.spoken"), subtitle=self.t("settings.spoken_sub"))
        self.spoken.set_active(bool(self.cfg.get("spoken_punctuation", True)))
        text_group.add(self.spoken)
        self.append_space = Adw.SwitchRow(title=self.t("settings.append_space"))
        self.append_space.set_active(bool(self.cfg.get("append_space", True)))
        text_group.add(self.append_space)

        control_group = Adw.PreferencesGroup(title=self.t("settings.control"))
        page.add(control_group)
        self.shortcut_row = Adw.ActionRow(title=self.t("settings.shortcut"), subtitle=label_for(self._shortcut_binding))
        shortcut_btn = Gtk.Button(label=self.t("settings.change"), valign=Gtk.Align.CENTER)
        shortcut_btn.connect("clicked", self._open_shortcut_capture)
        self.shortcut_row.add_suffix(shortcut_btn)
        control_group.add(self.shortcut_row)
        paste_labels = ["Ctrl+V", "Ctrl+Shift+V", self.t("paste.clipboard")]
        self.paste = Adw.ComboRow(title=self.t("settings.paste"))
        self.paste.set_model(Gtk.StringList.new(paste_labels))
        self.paste.set_selected(self._idx(PASTE_MODES, self.cfg.get("paste_mode", "standard")))
        control_group.add(self.paste)
        self.notifications = Adw.SwitchRow(title=self.t("settings.notifications"))
        self.notifications.set_active(bool(self.cfg.get("notify", True)))
        control_group.add(self.notifications)

        self._build_dependencies_group(page)

        safety_group = Adw.PreferencesGroup(title=self.t("settings.safety"))
        page.add(safety_group)
        self.timeout = Adw.ComboRow(title=self.t("settings.timeout"), subtitle=self.t("settings.timeout_sub"))
        self.timeout.set_model(Gtk.StringList.new([self._duration_label(x) for x in TIMEOUT_VALUES]))
        self.timeout.set_selected(self._nearest_index(TIMEOUT_VALUES, number(self.cfg, "transcription_timeout_sec", 90)))
        safety_group.add(self.timeout)
        self.max_recording = Adw.ComboRow(title=self.t("settings.max_recording"), subtitle=self.t("settings.max_recording_sub"))
        self.max_recording.set_model(Gtk.StringList.new([self._duration_label(x) for x in RECORD_VALUES]))
        self.max_recording.set_selected(self._nearest_index(RECORD_VALUES, number(self.cfg, "max_recording_sec", 120)))
        safety_group.add(self.max_recording)

        interface_group = Adw.PreferencesGroup(title=self.t("settings.interface"))
        page.add(interface_group)
        self.ui_language = Adw.ComboRow(title=self.t("settings.ui_language"))
        self.ui_language.set_model(Gtk.StringList.new([self.t("ui.auto"), self.t("ui.russian"), self.t("ui.english")]))
        self.ui_language.set_selected(self._idx(UI_LANGUAGE_IDS, self.ui_lang_setting))
        interface_group.add(self.ui_language)

        diag_group = Adw.PreferencesGroup(title=self.t("settings.diagnostics"))
        page.add(diag_group)
        diag_row = Adw.ActionRow(title=self.t("settings.copy_diagnostics"), subtitle=self.t("settings.copy_diagnostics_sub"))
        diag_btn = Gtk.Button(label=self.t("settings.copy_diagnostics"), valign=Gtk.Align.CENTER)
        diag_btn.connect("clicked", self._copy_diagnostics)
        diag_row.add_suffix(diag_btn)
        diag_group.add(diag_row)
        logs_row = Adw.ActionRow(title=self.t("settings.copy_logs"), subtitle=self.t("settings.copy_logs_sub"))
        logs_btn = Gtk.Button(label=self.t("settings.copy_logs"), valign=Gtk.Align.CENTER)
        logs_btn.connect("clicked", self._copy_logs)
        logs_row.add_suffix(logs_btn)
        diag_group.add(logs_row)

        actions = Adw.PreferencesGroup()
        page.add(actions)
        save = Adw.ActionRow(title=self.t("settings.save"))
        btn = Gtk.Button(label=self.t("settings.save"), valign=Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self._save)
        save.add_suffix(btn)
        actions.add(save)

        self._update_engine_visibility()
        self._sync_model_ui()
        return scroller

    # ------------------------------------------------------------------
    # Dependencies group
    # ------------------------------------------------------------------
    def _build_dependencies_group(self, page):
        group = Adw.PreferencesGroup(
            title=self.t("settings.dependencies"),
            description=self.t("settings.deps_sub"),
        )
        page.add(group)
        for dep in deps_mod.dependencies():
            row = Adw.ActionRow(title=dep.label)
            button = Gtk.Button(label=self.t("settings.install"), valign=Gtk.Align.CENTER)
            button.connect("clicked", self._install_dependency, dep.id)
            row.add_suffix(button)
            spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
            spinner.set_visible(False)
            row.add_suffix(spinner)
            group.add(row)
            self._dep_rows[dep.id] = (row, button, spinner)
        self._refresh_dependency_rows()

    def _refresh_dependency_rows(self, force: bool = False):
        """Recompute every dependency row from the current PATH state.

        ``_poll_status`` ticks every 650 ms, so the probe is throttled and must not repaint
        the rows on every tick. An install in progress bypasses the throttle so the spinner
        state stays correct.
        """
        if not force and not self._dep_installing:
            now = time.monotonic()
            if now - self._dep_last_refresh < 5.0:
                return
            self._dep_last_refresh = now
        manager = pkgsys.detect_manager()
        can_install = bool(manager and (not pkgsys.requires_privilege() or pkgsys.pkexec_path()))
        for row_data in deps_mod.status_all():
            dep_id = str(row_data["id"])
            entry = self._dep_rows.get(dep_id)
            if entry is None:
                continue
            row, button, spinner = entry
            missing = tuple(row_data.get("missing") or ())
            if not missing:
                row.set_subtitle(self.t("settings.deps_ok"))
                button.set_visible(False)
                spinner.stop()
                spinner.set_visible(False)
                continue
            purpose = self.t(str(row_data.get("purpose_key") or ""))
            subtitle = self.t("settings.deps_missing", binaries=", ".join(missing), purpose=purpose)
            if dep_id in self._dep_installing:
                spinner.start()
                spinner.set_visible(True)
                button.set_sensitive(False)
                subtitle = self.t("settings.installing")
            else:
                spinner.stop()
                spinner.set_visible(False)
                button.set_sensitive(True)
                packages = pkgsys.resolve_packages(dep_id, manager)
                if manager is None:
                    # No package manager at all: nothing we could run.
                    button.set_visible(False)
                    subtitle = self.t("settings.deps_no_manager", programs=dep_id)
                elif not can_install:
                    # Manager found but no way to become root (no pkexec).
                    button.set_visible(False)
                    subtitle = self.t("settings.deps_manual")
                elif packages is None:
                    # The package name for this manager is not known with certainty; never
                    # invent one.
                    button.set_visible(False)
                    subtitle = self.t("settings.deps_no_name")
                else:
                    button.set_visible(True)
            row.set_subtitle(self._clip_subtitle(subtitle))

    @staticmethod
    def _clip_subtitle(text: str, limit: int = 120) -> str:
        """Keep a subtitle short so a verbose error cannot break the layout."""
        flat = " ".join(str(text).split())
        return flat if len(flat) <= limit else flat[: limit - 1] + "…"

    def _install_dependency(self, _button, dep_id: str):
        """Explicit user action: install one dependency via the system manager."""
        if dep_id in self._dep_installing:
            return
        dep = deps_mod.get(dep_id)
        if dep is None:
            return
        manager = pkgsys.detect_manager()
        if manager is None:
            self._toast(self.t("settings.deps_no_manager", programs=dep_id))
            return
        packages = pkgsys.resolve_packages(dep, manager)
        if packages is None:
            self._toast(self.t("settings.deps_no_name"))
            return
        if pkgsys.requires_privilege() and pkgsys.pkexec_path() is None:
            self._toast(self.t("settings.deps_manual"))
            return

        self._dep_installing.add(dep_id)
        self._refresh_dependency_rows(force=True)
        # The package manager blocks and pkexec shows an authorization dialog, so the
        # work runs off the UI thread and comes back through GLib.idle_add.
        threading.Thread(
            target=self._install_dependency_worker,
            args=(dep_id, packages),
            daemon=True,
        ).start()

    def _install_dependency_worker(self, dep_id: str, packages: list[str]):
        try:
            ok, message = pkgsys.install_packages(packages, language=self.ui_lang)
        except Exception as exc:  # never let a worker kill the process
            ok, message = False, str(exc)
        GLib.idle_add(self._install_dependency_done, dep_id, ok, message)

    def _install_dependency_done(self, dep_id: str, ok: bool, message: str):
        self._dep_installing.discard(dep_id)
        self._refresh_dependency_rows(force=True)
        if ok:
            # Installing a package is not enough on its own: setup-user applies the global
            # shortcut and enables the ydotoold unit, and postinst runs it only once.
            # Re-run it so a dependency installed later really starts working.
            self._apply_desktop_integration()
            self.toast.add_toast(Adw.Toast(title=self.t("toast.deps_installed")))
            return GLib.SOURCE_REMOVE
        row = self._dep_rows.get(dep_id)
        if row is not None:
            row[0].set_subtitle(self._clip_subtitle(message))
        self.toast.add_toast(Adw.Toast(title=self.t("toast.deps_failed"), timeout=6))
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _nearest_index(values, value):
        return min(range(len(values)), key=lambda i: abs(values[i] - value))

    def _duration_label(self, seconds: int) -> str:
        if seconds < 60:
            return f"{seconds} s"
        minutes = seconds // 60
        return f"{minutes} min" if self.ui_lang == "en" else f"{minutes} мин"

    def _selected_model_preset(self):
        idx = int(self.model.get_selected()) if hasattr(self, "model") else 0
        if idx < 0 or idx >= len(MODEL_PRESETS):
            idx = 0
        return MODEL_PRESETS[idx]

    def _selected_model_id(self) -> str:
        preset = self._selected_model_preset()
        model_id = str(preset["id"])
        if model_id == "__custom__":
            value = self.custom_model.get_text().strip() if hasattr(self, "custom_model") else ""
            return value or str(self.cfg.get("custom_model", "")).strip() or "small"
        return model_id

    def _on_model_selected(self, *_args):
        self._sync_model_ui()
        model_id = self._selected_model_id()
        # Choosing a model is choosing weights, and weights are the one thing here that
        # costs gigabytes of somebody's bandwidth. A model already on disk needs no
        # permission - it is only loaded - so the answer comes from the state report,
        # and the download starts only after the user has said yes. Without the
        # question, one click in a dropdown started a download nobody asked for.
        self._download_confirmation_for = model_id
        self._refresh_model_state()

    def _decide_what_to_do_about_the_selected_model(self, entry: dict) -> None:
        """Warm what is on disk; ask before fetching what is not.

        Called from the state report, on the main loop, once per selection.
        """
        model_id = str(entry.get("id") or "")
        if not self._download_confirmation_for or model_id != self._download_confirmation_for:
            return
        self._download_confirmation_for = None
        if entry.get("downloaded"):
            # Free, and invisible otherwise: the first dictation would pay for loading
            # the model, and nothing would have said so.
            self._ask_daemon_to_prepare_model()
            return
        if str(entry.get("kind") or "") != model_store.KIND_REPO:
            # A local path or a name nothing can fetch: there is nothing to ask about,
            # and the daemon says so for itself.
            self._ask_daemon_to_prepare_model()
            return
        self._ask_about_download(model_id, int(entry.get("size_bytes") or 0))

    def _ask_about_download(self, model_id: str, size_bytes: int) -> None:
        """The question, before the gigabytes."""
        if size_bytes <= 0:
            # The size is what the catalogue says; a model whose files are not on disk
            # has none to count, and a question without a number is a guess.
            size = self.t("store.download_unknown_size")
        else:
            size = model_store.human_size(size_bytes, self.ui_lang)
        name = display_name(model_id)
        # Built by hand like the delete confirmation: a confirmation has to open
        # on the GTK 4 versions WayVoice supports.
        win = Gtk.Window(
            title=self.t("store.download_title"), transient_for=self, modal=True
        )
        win.set_default_size(460, 190)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)
        title = self._label(name, "section-title", xalign=0.5)
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)
        body = self._label(
            self.t("store.download_body", name=name, size=size), "muted", wrap=True, xalign=0.5
        )
        body.set_halign(Gtk.Align.CENTER)
        box.append(body)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.CENTER)
        later = Gtk.Button(label=self.t("store.download_later"))
        later.connect("clicked", lambda *_: win.close())
        go = Gtk.Button(label=self.t("store.download_now"))
        go.add_css_class("suggested-action")
        go.connect("clicked", self._download_confirmed, win)
        buttons.append(later)
        buttons.append(go)
        box.append(buttons)
        win.set_child(box)
        win.present()

    def _download_confirmed(self, _button, win) -> None:
        win.close()
        self._ask_daemon_to_prepare_model()

    def _ask_daemon_to_prepare_model(self) -> None:
        """Ask the daemon to make the selected model ready, off the main loop.

        Which half this is depends on what the model needs: a fetch if its weights are not on
        disk, a load into the worker if they are. Both belong to the daemon - the download is
        its child process, and only it can end that child without leaving a helper running -
        so this asks rather than does, and the answer comes back on the next status poll.

        The question about a fetch has already been asked and answered by the time anything
        calls this, so a refusal here would be a no-op.
        """

        def run() -> None:
            try:
                reply = request("prepare-model", timeout=5.0)
                # What the reply means is the user's business, and GTK is
                # touched on the main loop only: hand it over, decide there.
                if isinstance(reply, dict):
                    GLib.idle_add(self._handle_prepare_model_reply, reply)
            except Exception as exc:
                print(
                    f"WayVoice: could not ask the daemon to prepare the model: {exc}",
                    file=sys.stderr,
                )

        threading.Thread(target=run, daemon=True).start()

    def _handle_prepare_model_reply(self, reply: dict) -> None:
        """Say why the daemon refused to prepare the model, as a toast.

        A refusal the window swallowed looked exactly like a button that does
        nothing: the row kept its old state and no word explained the press.
        One refusal stays silent on purpose: ``not_applicable`` is the daemon's
        word for a value that is not a Hub repository at all, and the model row
        already says that about a local folder - repeating it as a toast on
        every selection would be noise.
        """
        if reply.get("ok"):
            return
        if str(reply.get("state") or "") == "not_applicable":
            return
        error = str(reply.get("error") or "")
        if error:
            self._toast(error)

    def _sync_model_ui(self):
        if not hasattr(self, "model"):
            return
        preset = self._selected_model_preset()
        self.model.set_subtitle(preset_subtitle(preset, self.ui_lang))
        is_custom = str(preset["id"]) == "__custom__"
        uses_models = self._selected_engine_uses_models()
        self.custom_model.set_visible(uses_models and is_custom)
        self.model_state_row.set_visible(uses_models)
        self.model_disk_row.set_visible(uses_models)
        forced = preset.get("language")
        if uses_models and forced:
            # The model decides the language; show which one, but do not let it
            # be edited into something the model was not trained for.
            self.language.select_code(str(forced))
            self.language.set_sensitive(False)
        else:
            self.language.set_sensitive(True)

    # ------------------------------------------------------------------
    # Model files on disk
    # ------------------------------------------------------------------
    def _model_state_text(self, entry: dict) -> tuple[str, bool]:
        """Subtitle for the selected model, and whether it may be deleted.

        A local path is never deletable: those files belong to the user and WayVoice did not
        put them there.
        """
        kind = str(entry.get("kind") or "")
        size = model_store.human_size(int(entry.get("size_bytes") or 0), self.ui_lang)
        if kind == "local":
            if entry.get("downloaded"):
                return self.t("store.local", size=size), False
            return self.t("store.local_missing"), False
        if entry.get("downloaded"):
            return self.t("store.downloaded", size=size), True
        return self.t("store.missing"), False

    def _apply_model_state(self, entry: dict, free_bytes: int, total_bytes: int, held: dict) -> None:
        """Paint the row from a result computed off the UI thread."""
        self._model_entry = dict(entry) if isinstance(entry, dict) else {}
        text, deletable = self._model_state_text(entry)
        # The whole hub is shown next to our own total: the cache is shared with other
        # applications, and a number that explains the folder beats one that looks
        # like it should.
        self.model_disk_row.set_subtitle(self.t(
            "store.disk",
            size=model_store.human_size(total_bytes, self.ui_lang),
            cache=model_store.human_size(model_store.hub_size(), self.ui_lang),
            free=model_store.human_size(free_bytes, self.ui_lang),
        ))
        # A model held in the warm worker cannot go away while the worker keeps it: the
        # next dictation would claim a model that is not on disk. The button explains
        # that instead of silently doing nothing.
        if deletable and held.get("running") and str(held.get("model") or "") == str(entry.get("id") or ""):
            deletable = False
            text = f"{text} · {self.t('store.delete_busy')}"
        self.model_state_row.set_subtitle(text)
        self.model_delete_btn.set_sensitive(deletable)
        # A model that is not there has no button to show; a local folder is greyed out
        # rather than hidden, so "you cannot delete this" is visible instead of looking
        # like a missing feature.
        self.model_delete_btn.set_visible(str(entry.get("kind") or "") != "custom")
        self._refresh_fetch_button()

    def _refresh_fetch_button(self) -> None:
        """Show the download button exactly when it is the right thing to press.

        A hub model that is not on disk, with nothing being done about it yet. While a
        download or a warm-up runs, the row below already shows the progress and the way to
        stop it, so a second button would be a third answer to the same question.
        """
        if not hasattr(self, "model_fetch_btn"):
            return
        entry = self._model_entry if isinstance(self._model_entry, dict) else {}
        report = self._download_report if isinstance(self._download_report, dict) else {}
        download = report.get("download") if isinstance(report.get("download"), dict) else {}
        phase = str(download.get("state") or "idle")
        model_id = str(entry.get("id") or "")
        # Check if the model is a repo model (hub model) and not downloaded
        is_repo_model = str(entry.get("kind") or "") == model_store.KIND_REPO
        is_not_downloaded = not entry.get("downloaded")
        # Determine if button should be visible
        wanted = (
            is_repo_model
            and is_not_downloaded
            and phase in {"idle", "error", "ready"}
            and str(download.get("model") or "") in {"", model_id}
        )
        self.model_fetch_btn.set_visible(wanted)

    def _ask_to_fetch_the_model(self, *_args) -> None:
        """The row's own way in: the same question a new choice asks."""
        entry = self._model_entry if isinstance(self._model_entry, dict) else {}
        model_id = str(entry.get("id") or "")
        if not model_id:
            return
        if str(entry.get("kind") or "") != model_store.KIND_REPO:
            # A local path cannot be fetched and its row says so in its own subtitle; a
            # button here would report success and change nothing.
            self._toast(self.t("store.refuse_local"))
            return
        self._ask_about_download(model_id, int(entry.get("size_bytes") or 0))

    def _apply_download_state(self, report: dict | None) -> None:
        """Paint the download row from the daemon's model report.

        The bar follows what the daemon says rather than anything the window measures: the
        download runs in the daemon, and a window watching the cache directory would be
        reporting a different thing from the one the user is waiting for.
        """
        if not hasattr(self, "model_download_row"):
            return
        report = report if isinstance(report, dict) else {}
        self._download_report = report
        # Before any branch below, all of which return: the button in the model row asks
        # about both what is on disk and what is being done about it.
        self._refresh_fetch_button()
        download = report.get("download") if isinstance(report.get("download"), dict) else {}
        state = str(download.get("state") or "idle")
        model_id = str(report.get("model") or "")
        if str(download.get("model") or "") != model_id:
            # Work on a different model than the selected one - the user changed the row
            # while the old model was still coming down or being loaded. Painting its
            # bytes, or its warm-up, next to the new model would be a lie, and this has
            # to come before the warm-up below.
            self.model_download_row.set_visible(False)
            return
        if state == "warming":
            # No download: the model is on disk and is being read into memory so that the
            # first dictation is as fast as the rest.
            self.model_download_cancel_btn.set_sensitive(False)
            self.model_download_bar.pulse()
            self.model_download_row.set_title(
                self.t("store.warming", model=display_name(model_id) if model_id else "")
            )
            self.model_download_row.set_subtitle(self.t("store.warming_sub"))
            self.model_download_row.set_visible(True)
            return
        if state == "downloading":
            done = int(download.get("done_bytes") or 0)
            total = int(download.get("total_bytes") or 0)
            name = display_name(model_id) if model_id else ""
            self.model_download_cancel_btn.set_sensitive(True)
            if download.get("warming"):
                # The weights are down and the model is going into memory: for a moment
                # there is nothing to measure, and it would look like a download that
                # stopped.
                self.model_download_bar.pulse()
                self.model_download_row.set_title(self.t("store.warming", model=name))
                self.model_download_row.set_subtitle(self.t("store.warming_sub"))
                # A load that cannot be interrupted: offering to stop it would be a button
                # that reports success and changes nothing.
                self.model_download_cancel_btn.set_sensitive(False)
            elif total > 0:
                if done > 0:
                    self.model_download_bar.set_fraction(min(1.0, done / total))
                else:
                    # Nothing has arrived yet but the size is known: a fixed fraction of
                    # zero would look stuck.
                    self.model_download_bar.pulse()
                self.model_download_row.set_title(self.t("store.downloading", model=name))
                self.model_download_row.set_subtitle(self.t(
                    "store.download_progress",
                    done=model_store.human_size(done, self.ui_lang),
                    total=model_store.human_size(total, self.ui_lang),
                    percent=int(min(100, done * 100 / total)),
                ))
            else:
                self.model_download_bar.pulse()
                self.model_download_row.set_title(self.t("store.downloading", model=name))
                self.model_download_row.set_subtitle(self.t("store.download_unknown"))
            self.model_download_row.set_visible(True)
            return
        if state == "error" and str(download.get("error") or ""):
            self.model_download_bar.set_fraction(0.0)
            self.model_download_row.set_title(self.t("store.download_failed"))
            self.model_download_row.set_subtitle(str(download.get("error")))
            self.model_download_row.set_visible(True)
            return
        self.model_download_row.set_visible(False)

    def _cancel_model_download(self, *_args) -> None:
        """Stop a running download through the daemon.

        Cancelling is the daemon's job: the download is its child process, and only it can
        end it without leaving a helper running.
        """
        threading.Thread(target=self._cancel_download_worker, daemon=True).start()

    def _cancel_download_worker(self) -> None:
        try:
            request("cancel-download", timeout=2.0)
        except Exception as exc:
            print(f"WayVoice: could not stop the download: {exc}", file=sys.stderr)

    def _hero_preparation_caption(self, model_report) -> tuple[str, str, str] | None:
        """What the main window should say about a model being fetched or loaded.

        Reads the same report the settings window paints its download row from.
        Returns ``None`` when the hero has nothing preparation-shaped to say: no
        report, work on a different model than the one selected here, or an
        error - an error belongs to the settings row and the toast, and quoting
        it in the hero would repeat it every 650 ms.
        """
        report = model_report if isinstance(model_report, dict) else {}
        download = report.get("download") if isinstance(report.get("download"), dict) else {}
        state = str(download.get("state") or "idle")
        if state not in {"downloading", "warming"}:
            return None
        model_id = str(download.get("model") or "")
        if not model_id or model_id != str(self._selected_model_id() or ""):
            # Work on a model other than the one this window names: painting it
            # here would describe a download the user may have already abandoned
            # by selecting something else.
            return None
        if state == "warming":
            return (
                self.t("hero.warming", model=display_name(model_id)),
                self.t("store.warming_sub"),
                "folder-download-symbolic",
            )
        done = int(download.get("done_bytes") or 0)
        total = int(download.get("total_bytes") or 0)
        if total > 0 and done > 0:
            caption = self.t(
                "store.download_progress",
                done=model_store.human_size(done, self.ui_lang),
                total=model_store.human_size(total, self.ui_lang),
                percent=int(min(100, done * 100 / total)),
            )
        elif total > 0:
            caption = self.t("store.download_unknown")
        else:
            caption = self.t("store.download_unknown")
        return (
            self.t("hero.downloading", model=display_name(model_id)),
            caption,
            "folder-download-symbolic",
        )

    def _show_hero_preparation(self, title: str, caption: str, icon: str) -> None:
        """Paint the hero from a preparation report, pill included."""
        self.status_pill.set_text(self.t("status.preparing"))
        self.hero_state.set_text(title)
        self.hero_caption.set_text(caption)
        self.mic_icon.set_from_icon_name(icon)

    def _refresh_model_state(self) -> None:
        """Recompute the model row, off the GTK main loop.

        Walking the cache follows every snapshot symlink into every blob and stats the
        results - ~6 ms warm here, slower on the first pass after a cold start - so it runs
        in a worker thread and comes back through ``GLib.idle_add``. A run already in flight
        is not joined by another one; the request is remembered and re-run when the first
        finishes, so the row cannot show a state older than the last change.
        """
        if not hasattr(self, "model_state_row"):
            return
        if self._model_refresh_busy:
            self._model_refresh_pending = True
            return
        self._model_refresh_busy = True
        self._model_refresh_pending = False
        model_id = self._selected_model_id()
        threading.Thread(target=self._model_state_worker, args=(model_id,), daemon=True).start()

    def _model_state_worker(self, model_id: str) -> None:
        try:
            entry = model_store.describe(model_id)
            free_bytes = model_store.disk_free()
            total_bytes = model_store.total_size()
            # The worker ping has a timeout of its own, so it belongs here and not on the
            # main loop between two frames.
            held = worker_info()
        except Exception as exc:  # never let a worker kill the process
            entry = {"id": model_id, "kind": "unknown", "downloaded": False, "size_bytes": 0}
            free_bytes = total_bytes = 0
            held = {"running": False, "model": ""}
            print(f"WayVoice: model state refresh failed: {exc}", file=sys.stderr)
        GLib.idle_add(self._model_state_ready, entry, free_bytes, total_bytes, held)

    def _model_state_ready(self, entry, free_bytes, total_bytes, held):
        self._model_refresh_busy = False
        self._apply_model_state(entry, free_bytes, total_bytes, held)
        self._decide_what_to_do_about_the_selected_model(entry)
        if self._model_refresh_pending:
            self._refresh_model_state()
        return GLib.SOURCE_REMOVE

    def _ask_delete_model(self, *_args) -> None:
        """Confirm, then delete the selected model."""
        if not self.model_delete_btn.get_sensitive() or self._model_deleting:
            return
        model_id = self._selected_model_id()
        if model_store.repo_dir_name(model_id) is None:
            self._toast(self.t("store.refuse_local"))
            return
        # Built by hand like the shortcut dialog rather than with Gtk.AlertDialog: that
        # widget does not have the same properties in every GTK 4 release, and a
        # confirmation has to open on the versions we support.
        name = display_name(model_id)
        win = Gtk.Window(title=self.t("store.delete_title"), transient_for=self, modal=True)
        win.set_default_size(440, 170)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)
        title = self._label(name, "section-title", xalign=0.5)
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)
        body = self._label(self.t("store.delete_body", name=name), "muted", wrap=True, xalign=0.5)
        body.set_halign(Gtk.Align.CENTER)
        box.append(body)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.CENTER)
        cancel = Gtk.Button(label=self.t("common.cancel"))
        cancel.connect("clicked", lambda *_: win.close())
        confirm = Gtk.Button(label=self.t("common.delete"))
        confirm.add_css_class("destructive-action")
        confirm.connect("clicked", self._delete_model_confirmed, win, model_id)
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)
        win.set_child(box)
        win.present()

    def _delete_model_confirmed(self, _button, win, model_id: str) -> None:
        win.close()
        if self._model_deleting:
            return
        self._model_deleting = True
        self.model_delete_btn.set_sensitive(False)
        self.model_delete_btn.set_label(self.t("store.deleting"))
        # rmtree of half a gigabyte plus a full rescan of the hub: seconds, not
        # milliseconds, so it must not run where the main loop draws.
        threading.Thread(target=self._delete_model_worker, args=(model_id,), daemon=True).start()

    def _delete_model_worker(self, model_id: str) -> None:
        try:
            # A warm worker holds the model in memory and may be mid-request, so it is
            # stopped first: that is what makes the deletion honest rather than a claim.
            # Best effort - a worker that is not there needs no stopping.
            held = worker_info()
            if held.get("running") and str(held.get("model") or "") == model_id:
                stop_worker()
            result = model_store.delete(model_id)
        except model_store.RefusedError as exc:
            result = {"ok": False, "error_key": exc.key, "detail": exc.detail, "model_id": model_id}
        except Exception as exc:  # never let a worker kill the process
            result = {"ok": False, "error_key": "store.delete_failed", "detail": str(exc), "model_id": model_id}
        GLib.idle_add(self._delete_model_ready, result)

    def _delete_model_ready(self, result: dict) -> None:
        self._model_deleting = False
        self.model_delete_btn.set_label(self.t("common.delete"))
        if not result.get("ok"):
            # A refusal carries a translation key rather than a sentence, so the reason
            # is localized here and never leaks an English-only string into the UI.
            reason = model_store.refusal_message(
                str(result.get("error_key") or "store.delete_failed"),
                str(result.get("detail") or ""),
                self.ui_lang,
            )
            self._toast(self.t("store.delete_refused", reason=reason), timeout=6)
            self._refresh_model_state()
            return GLib.SOURCE_REMOVE
        message_key = str(result.get("message_key") or "store.deleted")
        freed = int(result.get("freed_bytes") or 0)
        kept = int(result.get("kept_bytes") or 0)
        if message_key == "store.deleted" and kept > 0:
            # Some files had to stay: another model links to them. Saying only that the
            # model was deleted would hide files that are still on disk by design.
            message_key = "store.deleted_shared"
        self._toast(self.t(
            message_key,
            size=model_store.human_size(freed, self.ui_lang),
            freed=model_store.human_size(freed, self.ui_lang),
            kept=model_store.human_size(kept, self.ui_lang),
        ))
        self._refresh_model_state()
        return GLib.SOURCE_REMOVE

    def _on_engine_selected(self, *_args):
        self._update_engine_visibility()
        self._sync_model_ui()
        self._refresh_model_state()
        self._poll_engine_settings()

    def _selected_engine(self):
        return engine_ids()[self.engine.get_selected()]

    def _selected_engine_object(self):
        """The selected engine, or ``None`` while the list is not built yet."""
        if hasattr(self, "engine"):
            return get_engine(self._selected_engine())
        return get_engine(self.cfg.get("engine", DEFAULT_ENGINE))

    def _selected_engine_uses_models(self) -> bool:
        engine = self._selected_engine_object()
        return bool(engine and engine.uses_models)

    def _update_engine_visibility(self):
        engine = self._selected_engine_object()
        owned = set(engine.settings) if engine else set()
        for key, row in self._engine_rows:
            row.set_visible(key in owned)
        if hasattr(self, "engine_setup_btn"):
            self.engine_setup_btn.set_visible(bool(engine and engine.needs_setup))

    def _open_shortcut_capture(self, *_args):
        win = Gtk.Window(title=self.t("shortcut.title"), transient_for=self, modal=True)
        win.set_default_size(420, 190)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)
        title = self._label(self.t("shortcut.capture"), "section-title", xalign=0.5)
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)
        hint = self._label(self.t("shortcut.hint"), "muted", wrap=True, xalign=0.5)
        hint.set_halign(Gtk.Align.CENTER)
        box.append(hint)
        key_label = self._label(label_for(self._shortcut_binding), "capture-key", xalign=0.5)
        key_label.set_halign(Gtk.Align.CENTER)
        box.append(key_label)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.CENTER)
        disable = Gtk.Button(label=self.t("shortcut.disable"))
        disable.connect("clicked", self._disable_shortcut, win)
        cancel = Gtk.Button(label=self.t("common.cancel"))
        cancel.connect("clicked", lambda *_: win.close())
        buttons.append(disable)
        buttons.append(cancel)
        box.append(buttons)
        win.set_child(box)
        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._capture_shortcut_key, win, key_label)
        win.add_controller(controller)
        win.present()

    def _disable_shortcut(self, _button, win):
        self._shortcut_binding = ""
        self.shortcut_row.set_subtitle(self.t("shortcut.disabled"))
        self.hotkey_label.set_text(self.t("shortcut.disabled"))
        win.close()

    def _capture_shortcut_key(self, _controller, keyval, _keycode, state, win, key_label):
        name = Gdk.keyval_name(keyval) or ""
        if name in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Meta_L", "Meta_R", "Super_L", "Super_R", "ISO_Level3_Shift"}:
            return True
        if name == "Escape":
            win.close()
            return True
        allowed = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK
        mods = state & allowed
        is_function = name.startswith("F") and name[1:].isdigit()
        is_special = name in {"Pause", "Print", "Scroll_Lock"}
        if not mods and not is_function and not is_special:
            key_label.set_text(self.t("shortcut.need_modifier"))
            return True
        binding = Gtk.accelerator_name(keyval, mods)
        if binding:
            self._shortcut_binding = binding
            shown = label_for(binding)
            self.shortcut_row.set_subtitle(shown)
            self.hotkey_label.set_text(shown)
            win.close()
        return True

    def _save(self, *_args):
        preset = self._selected_model_preset()
        if self._selected_engine_uses_models() and str(preset["id"]) == "__custom__" and not self.custom_model.get_text().strip():
            self.toast.add_toast(Adw.Toast(title=self.t("toast.custom_model")))
            return
        model_id = self._selected_model_id()
        forced = forced_language(model_id)
        language = languages.normalize(forced or self.language.selected_code())
        new_ui_setting = UI_LANGUAGE_IDS[self.ui_language.get_selected()]
        cfg = load_config()
        cfg.update({
            "engine": self._selected_engine(),
            "model": model_id,
            "custom_model": self.custom_model.get_text().strip(),
            "language": language,
            "device": DEVICES[self.device.get_selected()],
            "vad_filter": self.vad.get_active(),
            "engine_worker": self.worker.get_active(),
            "auto_punctuation": self.auto_punct.get_active(),
            "spoken_punctuation": self.spoken.get_active(),
            "append_space": self.append_space.get_active(),
            "paste_mode": PASTE_MODES[self.paste.get_selected()],
            "notify": self.notifications.get_active(),
            "shortcut": self._shortcut_binding,
            "whisper_cpp_binary": self.cpp_binary.get_text().strip(),
            "whisper_cpp_model": self.cpp_model.get_text().strip(),
            "whisper_cpp_gpu": self.cpp_gpu.get_active(),
            "custom_command": self.custom_command.get_text().strip(),
            "transcription_timeout_sec": TIMEOUT_VALUES[self.timeout.get_selected()],
            "max_recording_sec": RECORD_VALUES[self.max_recording.get_selected()],
            "ui_language": new_ui_setting,
        })
        save_config(cfg)
        self.cfg = cfg
        ok, msg = apply_shortcut(self._shortcut_binding)
        self._prepare_selected_engine(cfg)
        if new_ui_setting != self.ui_lang_setting:
            # Restart the UI so the new interface language is applied. The settings
            # binary path is resolved explicitly and passed as $0, so the restart never
            # depends on a login-shell PATH.
            restart_cmd = command_path("wayvoice-settings")
            subprocess.Popen(
                ["/bin/sh", "-c", 'sleep 0.35; exec "$0"', restart_cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            app = self.get_application()
            if app:
                app.quit()
            return
        self.hotkey_label.set_text(label_for(self._shortcut_binding))
        self._update_cards()
        self.toast.add_toast(Adw.Toast(title=self.t("settings.saved") if ok else msg))
        self._refresh_model_state()
        self._poll_engine_settings()

    def _prepare_selected_engine(self, cfg):
        """Prepare the selected engine when it can be prepared and is not ready.

        Engines that need no preparation (whisper.cpp, an external command) are left alone:
        their settings are the only thing that can make them ready.
        """
        engine = engine_from_config(cfg)
        if engine is None or not engine.needs_setup:
            return
        if engine_status(cfg).get("state") in {"missing", "error"}:
            request_engine_setup(engine)

    def _setup_engine(self, *_args):
        cfg = load_config()
        cfg["engine"] = self._selected_engine()
        save_config(cfg)
        if not request_engine_setup(engine_from_config(cfg)):
            # The button is only visible for engines that need preparation, so there is
            # nothing to show a spinner for.
            return
        self.engine_status_row.set_subtitle(self.t("settings.preparing"))
        self.engine_setup_btn.set_visible(False)
        self.engine_spinner.set_visible(True)
        self.engine_spinner.start()

    def _toggle(self, *_args):
        command = "cancel" if self._ui_busy else "toggle"
        reply = request(command, timeout=0.8)
        if not reply.get("ok"):
            self.toast.add_toast(Adw.Toast(title=str(reply.get("error") or self.t("toast.dictation_failed"))[:150]))

    def _update_cards(self):
        cfg = load_config()
        engine = engine_from_config(cfg)
        self.engine_card.set_text(engine_label(cfg.get("engine")) or "—")
        # Only an engine with its own model list has a model to show; the others keep
        # theirs (or none) among their own settings.
        self.model_card.set_text(display_name(str(cfg.get("model", "small"))) if engine and engine.uses_models else "—")
        self.paste_card.set_text({"standard": "Ctrl+V", "terminal": "Ctrl+Shift+V", "copy": self.t("paste.clipboard_short")}.get(str(cfg.get("paste_mode")), "—"))

    def _set_state_style(self, state):
        for css in ("recording", "busy", "ready"):
            self.status_pill.remove_css_class(css)
            self.mic_button.remove_css_class(css)
        if state in {"recording", "busy", "ready"}:
            self.status_pill.add_css_class(state)
        if state in {"recording", "busy"}:
            self.mic_button.add_css_class(state)

    def _missing_required(self):
        """Blocking dependencies that are currently unusable."""
        return [
            dep for dep in deps_mod.dependencies()
            if dep.required and not deps_mod.status_of(dep)["ok"]
        ]

    def _poll_status(self):
        reply = request("status", timeout=0.12)
        self._update_cards()
        if not reply.get("ok"):
            self._ui_busy = False
            self.mic_button.set_sensitive(False)
            self.status_pill.set_text(self.t("status.start"))
            self.hero_state.set_text(self.t("hero.starting"))
            self.hero_caption.set_text("")
            self.health_summary.set_text(self.t("status.waiting"))
            self.health_detail.set_text("")
            self._set_state_style("offline")
            return GLib.SOURCE_CONTINUE

        daemon_version = str(reply.get("version") or "")
        if daemon_version != __version__ and not self._restart_requested and not reply.get("recording") and not reply.get("busy"):
            self._restart_requested = True
            # Restarting waits for the new daemon to answer, so it must not block the GTK
            # main loop the polling runs on.
            threading.Thread(target=service.restart_daemon, daemon=True).start()
            return GLib.SOURCE_CONTINUE

        engine = reply.get("engine") or {}
        est = str(engine.get("state") or "missing")
        self._apply_download_state(reply.get("model"))
        recording = bool(reply.get("recording"))
        busy = bool(reply.get("busy"))
        self._ui_busy = busy
        error = str(reply.get("last_error") or "")
        warning = str(reply.get("last_warning") or "")
        text = str(reply.get("last_text") or "")
        shortcut = str(reply.get("shortcut") or label_for(self._shortcut_binding))
        self.hotkey_label.set_text(shortcut)

        if recording:
            state = "recording"
            elapsed = int(float(reply.get("recording_seconds") or 0))
            self.mic_button.set_sensitive(True)
            self.status_pill.set_text(self.t("status.recording"))
            self.hero_state.set_text(self.t("hero.recording"))
            self.hero_caption.set_text(f"{self.t('hero.recording_hint')}  ·  {elapsed}s")
            self.mic_icon.set_from_icon_name("media-playback-stop-symbolic")
        elif busy:
            state = "busy"
            elapsed = int(float(reply.get("busy_seconds") or 0))
            limit = number(load_config(), "transcription_timeout_sec", 90)
            self.mic_button.set_sensitive(True)
            self.status_pill.set_text(self.t("status.transcribing"))
            self.hero_state.set_text(self.t("hero.transcribing"))
            self.hero_caption.set_text(f"{self.t('hero.cancel_hint')}  ·  {elapsed}/{limit}s")
            self.mic_icon.set_from_icon_name("process-stop-symbolic")
        else:
            preparation = self._hero_preparation_caption(reply.get("model"))
            if preparation is not None:
                # The daemon is fetching or loading the model. The settings window
                # paints its row, but this window used to keep saying "press and
                # speak" for the whole wait - an invitation the hot key cannot
                # honour yet, since starting now answers "model missing".
                state = "busy"
                self.mic_button.set_sensitive(True)
                self._show_hero_preparation(*preparation)
            elif est == "ready":
                state = "ready"
                self.mic_button.set_sensitive(True)
                self.status_pill.set_text(self.t("status.ready"))
                self.hero_state.set_text(self.t("hero.record"))
                self.hero_caption.set_text(f"{self.t('shortcut.global')}: {shortcut}")
                self.mic_icon.set_from_icon_name("audio-input-microphone-symbolic")
            else:
                state = "offline"
                self.mic_button.set_sensitive(False)
                self.status_pill.set_text(self.t("status.engine"))
                self.hero_state.set_text(self.t("hero.not_ready"))
                self.hero_caption.set_text(str(engine.get("message") or self.t("hero.open_settings")))
                self.mic_icon.set_from_icon_name("emblem-system-symbolic")

        self._set_state_style(state)
        # Missing blocking dependencies are reported as a warning, below a real error and
        # above a plain daemon warning: error > missing dependency > warning > engine
        # state.
        missing_deps = self._missing_required()
        dep_warning = self.t("health.deps_missing", names=", ".join(d.label for d in missing_deps)) if missing_deps else ""
        config_broken = str(reply.get("config_error") or "")
        if config_broken:
            # Above a warning and below a real error: the daemon works, but on
            # settings the user did not choose, and they should know that before
            # they start wondering why their shortcut or model changed.
            self.health_summary.set_text(self.t("health.warning"))
            self.health_detail.set_text(self._clip_subtitle(config_broken))
            self.health_detail.remove_css_class("error-text")
            self.health_detail.add_css_class("warning-text")
        elif error:
            self.health_summary.set_text(self.t("health.error"))
            self.health_detail.set_text(error)
            self.health_detail.remove_css_class("warning-text")
            self.health_detail.add_css_class("error-text")
        elif dep_warning:
            self.health_summary.set_text(self.t("health.warning"))
            self.health_detail.set_text(dep_warning)
            self.health_detail.remove_css_class("error-text")
            self.health_detail.add_css_class("warning-text")
        elif warning:
            self.health_summary.set_text(self.t("health.warning"))
            self.health_detail.set_text(warning)
            self.health_detail.remove_css_class("error-text")
            self.health_detail.add_css_class("warning-text")
        else:
            self.health_summary.set_text(self.t("health.ready") if est == "ready" else self.t("health.not_ready"))
            self.health_detail.set_text("" if est == "ready" else str(engine.get("message") or ""))
            self.health_detail.remove_css_class("error-text")
            self.health_detail.remove_css_class("warning-text")

        if self._dep_rows:
            self._refresh_dependency_rows()

        if text:
            self.last_text.remove_css_class("muted")
            self.last_text.set_text(text)
            self.transcript_meta.set_text(self.t("transcript.last"))
        else:
            self.transcript_meta.set_text("")
        return GLib.SOURCE_CONTINUE

    def _pending_engine_config(self) -> dict:
        """The config as this window currently shows it, for a status probe.

        Every engine-specific row is taken from its widget, so switching to an engine reports
        that engine's state without saving anything first.
        """
        cfg = load_config()
        cfg["engine"] = self._selected_engine()
        for key, row in self._engine_rows:
            if isinstance(row, Adw.EntryRow):
                cfg[key] = row.get_text().strip()
            elif isinstance(row, Adw.SwitchRow):
                cfg[key] = bool(row.get_active())
        return cfg

    def _poll_engine_settings(self):
        st = engine_status(self._pending_engine_config())
        state = str(st.get("state") or "")
        engine = self._selected_engine_object()
        if state == "ready":
            self.engine_status_row.set_subtitle(self.t("status.ready"))
            self.engine_setup_btn.set_visible(False)
            self.engine_spinner.stop()
            self.engine_spinner.set_visible(False)
        elif state == "installing":
            self.engine_status_row.set_subtitle(self.t("settings.preparing"))
            self.engine_setup_btn.set_visible(False)
            self.engine_spinner.set_visible(True)
            self.engine_spinner.start()
        else:
            self.engine_status_row.set_subtitle(str(st.get("message") or self.t("health.not_ready")))
            self.engine_spinner.stop()
            self.engine_spinner.set_visible(False)
            self.engine_setup_btn.set_visible(bool(engine and engine.needs_setup))
            self.engine_setup_btn.set_sensitive(True)
            self.engine_setup_btn.set_label(self.t("settings.repair") if state == "error" else self.t("settings.prepare"))
        return GLib.SOURCE_CONTINUE

    def _language_label(self, value) -> str:
        """Recognition language as the user knows it, plus the raw code.

        The name makes the report understandable; the code makes it reproducible, and it is
        the only part a developer can paste.
        """
        code = languages.normalize(value)
        name = tr("language.auto", self.ui_lang) if code == languages.AUTO else languages.display_name(code, self.ui_lang)
        return f"{name} ({code})"

    def _diagnostics_text(self) -> str:
        status = request("status", timeout=0.35)
        cfg = load_config()
        engine = status.get("engine") if isinstance(status, dict) else {}
        manager = pkgsys.detect_manager() or "not detected"
        lines = [
            f"WayVoice {__version__}",
            f"OS: {platform.platform()}",
            f"Python: {platform.python_version()}",
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', self.t('diagnostics.not_available'))}",
            f"Session: {os.environ.get('XDG_SESSION_TYPE', self.t('diagnostics.not_available'))}",
            f"Engine: {cfg.get('engine')} / {cfg.get('model')}",
            f"Engine state: {(engine or {}).get('state', 'unknown') if isinstance(engine, dict) else 'unknown'}",
            f"Device: {cfg.get('device')}",
            # The code alone ("yue") means nothing to whoever reads the report; the name
            # plus the code is both readable and unambiguous.
            f"Recognition language: {self._language_label(cfg.get('language'))}",
            f"Timeout: {cfg.get('transcription_timeout_sec')}s",
            f"Max recording: {cfg.get('max_recording_sec')}s",
            f"Shortcut: {label_for(str(cfg.get('shortcut', '')))}",
        ]
        if isinstance(status, dict) and status.get("config_error"):
            # The daemon is running on defaults because this file could not be read. That
            # is the first thing a report should say, since every other line below
            # describes a configuration the user never chose.
            lines.append(f"Config problem: {status.get('config_error')}")
        if isinstance(status, dict) and status.get("last_error"):
            lines.append(f"Last error: {status.get('last_error')}")
        if isinstance(status, dict) and status.get("last_warning"):
            lines.append(f"Last warning: {status.get('last_warning')}")
        # The whole block is plain English on purpose: it is pasted into bug reports,
        # where the existing OS/Python/Engine lines set the format.
        lines.append(f"Package manager: {manager}")
        lines.append("Dependencies:")
        for row in deps_mod.status_all():
            missing = tuple(row.get("missing") or ())
            detail = ", ".join(missing) if missing else str(row.get("binary_path") or "")
            label = f"{row.get('label')}{' (required)' if row.get('required') else ''}"
            status = "missing" if missing else "found"
            lines.append(f"  {label}: {status} ({detail or '-'})")
        return "\n".join(lines)

    def _copy_diagnostics(self, *_args):
        # Gdk.Display.get_default().get_clipboard() is the GTK3 clipboard, and on
        # Wayland it silently accepts the text and never offers it: the button
        # reported success and the clipboard stayed whatever it was. The same
        # wl-copy path the dictation itself uses is what actually works here, and
        # it is already tested in injector.py.
        language = self.ui_lang
        try:
            injector.copy_to_clipboard(self._diagnostics_text(), language)
        except injector.InjectionError as exc:
            self._toast(self.t("toast.diagnostics_failed", detail=str(exc)))
            return
        self._toast(self.t("toast.diagnostics_copied"))

    def _copy_logs(self, button=None) -> None:
        """Put the tail of the daemon's journal into the clipboard.

        This button used to show a toast with the journalctl command and nothing
        else, so "Open logs" opened nothing. A bug report needs the text, and the
        clipboard is where the diagnostics button already puts it.

        ``journalctl`` takes as long as the journal takes - measured at the full
        five-second timeout when it stalls - and running it inside the click handler
        stopped the window redrawing and answering the keyboard for all of it. It goes
        to a thread now, the same rule this file already follows for gsettings,
        systemctl and clipboard work.
        """
        if self._logs_copying:
            return
        self._logs_copying = True
        self._logs_button = button
        if button is not None:
            button.set_sensitive(False)

        def worker() -> None:
            try:
                report = self._journal_tail()
            except (OSError, subprocess.SubprocessError) as exc:
                GLib.idle_add(self._copy_logs_finished, str(exc))
                return
            if not report.strip():
                GLib.idle_add(self._copy_logs_finished, None, True)
                return
            try:
                injector.copy_to_clipboard(report, self.ui_lang)
            except injector.InjectionError as exc:
                GLib.idle_add(self._copy_logs_finished, str(exc))
                return
            GLib.idle_add(self._copy_logs_finished, None)

        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError as exc:
            # No thread could be started. Nothing has begun, so nothing has to be
            # undone - but the button must not stay greyed out, which is what happens
            # when the flag is set before start() and never cleared.
            self._logs_copying = False
            self._logs_button = None
            if button is not None:
                button.set_sensitive(True)
            self._toast(self.t("toast.logs_failed", detail=str(exc)))

    def _copy_logs_finished(self, problem, empty: bool = False) -> bool:
        button, self._logs_button = self._logs_button, None
        self._logs_copying = False
        if button is not None:
            button.set_sensitive(True)
        if problem is not None:
            self._toast(self.t("toast.logs_failed", detail=problem))
        elif empty:
            self._toast(self.t("toast.logs_empty"))
        else:
            self._toast(self.t("toast.logs_copied"))
        return GLib.SOURCE_REMOVE

    #: How much of the journal the button hands over. Enough for a bug report, small
    #: enough that the clipboard owner is not holding a megabyte of text.
    JOURNAL_LINES = 200
    JOURNAL_TIMEOUT = 5.0

    def _journal_tail(self) -> str:
        command = ["journalctl", "--user", "-u", "wayvoice.service",
                   "-n", str(self.JOURNAL_LINES), "--no-pager", "--output=short-iso"]
        cp = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.JOURNAL_TIMEOUT,
        )
        if cp.returncode != 0:
            # journalctl exits non-zero when the unit has never run, and says why on
            # stderr; an empty stdout with no reason is the case worth reporting.
            # SubprocessError rather than RuntimeError, so the caller has one family
            # to catch for "the program did not give us the log".
            detail = (cp.stderr or "").strip() or f"exit {cp.returncode}"
            raise subprocess.SubprocessError(detail)
        return cp.stdout or ""

    def _toast(self, title: str, timeout: int = 3) -> None:
        """Show a toast.

        The title is clipped: an ``Adw.Toast`` does not wrap, so a refusal that quotes
        the manager's output stretches across the whole window. The clipping used to
        live in a *second* ``_toast`` defined earlier in the class, which the later
        definition silently replaced - so no caller's text was ever clipped and the
        default timeout was 3 s instead of 4.
        """
        if hasattr(self, "toast"):
            self.toast.add_toast(
                Adw.Toast(title=self._clip_subtitle(title, 160), timeout=timeout))

    def _show_about(self, *_args):
        about = Adw.AboutWindow(transient_for=self, modal=True)
        about.set_application_name("WayVoice")
        about.set_application_icon("io.github.stepan.WayVoice")
        about.set_version(__version__)
        about.set_comments(self.t("about.comments"))
        about.set_developer_name(self.t("about.developer"))
        about.set_developers([self.t("about.developer")])
        about.set_license_type(Gtk.License.AGPL_3_0)
        about.set_copyright("© 2026 WayVoice contributors")
        about.present()

    def _quit(self, *_args):
        app = self.get_application()
        if app:
            app.quit()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.stepan.WayVoice", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = WayVoiceWindow(self)
        win.present()


def main():
    app = App()
    raise SystemExit(app.run(sys.argv))


if __name__ == "__main__":
    main()
