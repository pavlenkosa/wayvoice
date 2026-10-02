from __future__ import annotations

import json
import os
import platform
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from . import __version__
from .cli import request
from .config import load_config, save_config
from .engine import ENGINE_LABELS, engine_status, request_faster_setup
from .i18n import resolve_language, tr
from .models import MODEL_PRESETS, PRESET_LABELS, display_name, forced_language, preset_index, preset_subtitle
from .paths import command_path, setup_user_script
from .shortcut import apply_shortcut, label_for

ENGINE_IDS = ["faster-whisper", "whisper-cpp"]
ENGINE_NAMES = ["Faster-Whisper", "whisper.cpp"]
DEVICES = ["auto", "cpu", "cuda"]
DEVICE_NAMES = ["Auto", "CPU", "NVIDIA CUDA"]
LANGUAGES = ["auto", "ru", "en", "de", "fr", "es", "it", "uk", "pl", "pt", "zh", "ja", "ko", "tr"]
LANGUAGE_NAMES = [
    "Auto", "Русский", "English", "Deutsch", "Français", "Español", "Italiano",
    "Українська", "Polski", "Português", "中文", "日本語", "한국어", "Türkçe",
]
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

    def _background_start(self):
        commands = [["systemctl", "--user", "start", "wayvoice.service"]]
        setup_user = setup_user_script()
        if setup_user is None:
            print(
                "WayVoice: setup-user script not found; skipping desktop integration.",
                file=sys.stderr,
            )
        else:
            commands.append([str(setup_user)])
        for cmd in commands:
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                # Desktop integration is optional: never let it break the UI,
                # but keep the reason visible for bug reports.
                print(f"WayVoice: failed to start {' '.join(cmd)}: {exc}", file=sys.stderr)
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
        self.engine_card = self._metric_card(grid, 0, self.t("card.engine"), ENGINE_LABELS.get(self.cfg.get("engine"), "—"), "applications-engineering-symbolic")
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
        self.engine = Adw.ComboRow(title=self.t("settings.engine"))
        self.engine.set_model(Gtk.StringList.new(ENGINE_NAMES))
        self.engine.set_selected(self._idx(ENGINE_IDS, self.cfg.get("engine", "faster-whisper")))
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
        engine_group.add(self.custom_model)

        self.device = Adw.ComboRow(title=self.t("settings.device"))
        self.device.set_model(Gtk.StringList.new(DEVICE_NAMES))
        self.device.set_selected(self._idx(DEVICES, self.cfg.get("device", "auto")))
        engine_group.add(self.device)
        self.vad = Adw.SwitchRow(title=self.t("settings.vad"))
        self.vad.set_active(bool(self.cfg.get("vad_filter", True)))
        engine_group.add(self.vad)

        self.cpp_binary = Adw.EntryRow(title=self.t("settings.cpp_binary"))
        self.cpp_binary.set_text(str(self.cfg.get("whisper_cpp_binary", "")))
        engine_group.add(self.cpp_binary)
        self.cpp_model = Adw.EntryRow(title=self.t("settings.cpp_model"))
        self.cpp_model.set_text(str(self.cfg.get("whisper_cpp_model", "")))
        engine_group.add(self.cpp_model)
        self.cpp_gpu = Adw.SwitchRow(title=self.t("settings.cpp_gpu"))
        self.cpp_gpu.set_active(bool(self.cfg.get("whisper_cpp_gpu", True)))
        engine_group.add(self.cpp_gpu)

        text_group = Adw.PreferencesGroup(title=self.t("settings.text"))
        page.add(text_group)
        self.language = Adw.ComboRow(title=self.t("settings.language"))
        self.language.set_model(Gtk.StringList.new(LANGUAGE_NAMES))
        self.language.set_selected(self._idx(LANGUAGES, self.cfg.get("language", "ru")))
        text_group.add(self.language)
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

        safety_group = Adw.PreferencesGroup(title=self.t("settings.safety"))
        page.add(safety_group)
        self.timeout = Adw.ComboRow(title=self.t("settings.timeout"), subtitle=self.t("settings.timeout_sub"))
        self.timeout.set_model(Gtk.StringList.new([self._duration_label(x) for x in TIMEOUT_VALUES]))
        self.timeout.set_selected(self._nearest_index(TIMEOUT_VALUES, int(self.cfg.get("transcription_timeout_sec", 90))))
        safety_group.add(self.timeout)
        self.max_recording = Adw.ComboRow(title=self.t("settings.max_recording"), subtitle=self.t("settings.max_recording_sub"))
        self.max_recording.set_model(Gtk.StringList.new([self._duration_label(x) for x in RECORD_VALUES]))
        self.max_recording.set_selected(self._nearest_index(RECORD_VALUES, int(self.cfg.get("max_recording_sec", 120))))
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
        logs_row = Adw.ActionRow(title=self.t("settings.open_logs"), subtitle=self.t("settings.open_logs_sub"))
        logs_btn = Gtk.Button(label=self.t("settings.open_logs"), valign=Gtk.Align.CENTER)
        logs_btn.connect("clicked", self._show_logs_hint)
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

    def _sync_model_ui(self):
        if not hasattr(self, "model"):
            return
        preset = self._selected_model_preset()
        self.model.set_subtitle(preset_subtitle(preset, self.ui_lang))
        is_custom = str(preset["id"]) == "__custom__"
        self.custom_model.set_visible(self._selected_engine() == "faster-whisper" and is_custom)
        forced = preset.get("language")
        if self._selected_engine() == "faster-whisper" and forced:
            self.language.set_selected(self._idx(LANGUAGES, str(forced)))
            self.language.set_sensitive(False)
        else:
            self.language.set_sensitive(True)

    def _on_engine_selected(self, *_args):
        self._update_engine_visibility()
        self._sync_model_ui()
        self._poll_engine_settings()

    def _selected_engine(self):
        return ENGINE_IDS[self.engine.get_selected()]

    def _update_engine_visibility(self):
        engine = self._selected_engine() if hasattr(self, "engine") else self.cfg.get("engine", "faster-whisper")
        fw = engine == "faster-whisper"
        cpp = engine == "whisper-cpp"
        for row in (self.model, self.device, self.vad):
            row.set_visible(fw)
        if hasattr(self, "custom_model"):
            self.custom_model.set_visible(fw and str(self._selected_model_preset()["id"]) == "__custom__")
        for row in (self.cpp_binary, self.cpp_model, self.cpp_gpu):
            row.set_visible(cpp)
        if hasattr(self, "engine_setup_btn"):
            self.engine_setup_btn.set_visible(fw)

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
        if self._selected_engine() == "faster-whisper" and str(preset["id"]) == "__custom__" and not self.custom_model.get_text().strip():
            self.toast.add_toast(Adw.Toast(title=self.t("toast.custom_model")))
            return
        model_id = self._selected_model_id()
        forced = forced_language(model_id)
        language = forced or LANGUAGES[self.language.get_selected()]
        new_ui_setting = UI_LANGUAGE_IDS[self.ui_language.get_selected()]
        cfg = load_config()
        cfg.update({
            "engine": self._selected_engine(),
            "model": model_id,
            "custom_model": self.custom_model.get_text().strip(),
            "language": language,
            "device": DEVICES[self.device.get_selected()],
            "vad_filter": self.vad.get_active(),
            "auto_punctuation": self.auto_punct.get_active(),
            "spoken_punctuation": self.spoken.get_active(),
            "append_space": self.append_space.get_active(),
            "paste_mode": PASTE_MODES[self.paste.get_selected()],
            "notify": self.notifications.get_active(),
            "shortcut": self._shortcut_binding,
            "whisper_cpp_binary": self.cpp_binary.get_text().strip(),
            "whisper_cpp_model": self.cpp_model.get_text().strip(),
            "whisper_cpp_gpu": self.cpp_gpu.get_active(),
            "transcription_timeout_sec": TIMEOUT_VALUES[self.timeout.get_selected()],
            "max_recording_sec": RECORD_VALUES[self.max_recording.get_selected()],
            "ui_language": new_ui_setting,
        })
        save_config(cfg)
        self.cfg = cfg
        ok, msg = apply_shortcut(self._shortcut_binding)
        if cfg.get("engine") == "faster-whisper":
            st = engine_status(cfg)
            if st.get("state") in {"missing", "error"}:
                request_faster_setup()
        if new_ui_setting != self.ui_lang_setting:
            # Restart the UI so that the new interface language is applied.
            # The settings binary path is resolved explicitly and passed as
            # $0, so the restart never depends on a login-shell PATH.
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
        self._poll_engine_settings()

    def _setup_engine(self, *_args):
        cfg = load_config()
        cfg["engine"] = "faster-whisper"
        save_config(cfg)
        request_faster_setup()
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
        engine = str(cfg.get("engine", "faster-whisper"))
        self.engine_card.set_text(ENGINE_LABELS.get(engine, engine))
        self.model_card.set_text(display_name(str(cfg.get("model", "small"))) if engine == "faster-whisper" else "whisper.cpp")
        self.paste_card.set_text({"standard": "Ctrl+V", "terminal": "Ctrl+Shift+V", "copy": self.t("paste.clipboard_short")}.get(str(cfg.get("paste_mode")), "—"))

    def _set_state_style(self, state):
        for css in ("recording", "busy", "ready"):
            self.status_pill.remove_css_class(css)
            self.mic_button.remove_css_class(css)
        if state in {"recording", "busy", "ready"}:
            self.status_pill.add_css_class(state)
        if state in {"recording", "busy"}:
            self.mic_button.add_css_class(state)

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
            try:
                subprocess.Popen(["systemctl", "--user", "restart", "wayvoice.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return GLib.SOURCE_CONTINUE

        engine = reply.get("engine") or {}
        est = str(engine.get("state") or "missing")
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
            limit = int(load_config().get("transcription_timeout_sec", 90))
            self.mic_button.set_sensitive(True)
            self.status_pill.set_text(self.t("status.transcribing"))
            self.hero_state.set_text(self.t("hero.transcribing"))
            self.hero_caption.set_text(f"{self.t('hero.cancel_hint')}  ·  {elapsed}/{limit}s")
            self.mic_icon.set_from_icon_name("process-stop-symbolic")
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
        if error:
            self.health_summary.set_text(self.t("health.error"))
            self.health_detail.set_text(error)
            self.health_detail.remove_css_class("warning-text")
            self.health_detail.add_css_class("error-text")
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

        if text:
            self.last_text.remove_css_class("muted")
            self.last_text.set_text(text)
            self.transcript_meta.set_text(self.t("transcript.last"))
        else:
            self.transcript_meta.set_text("")
        return GLib.SOURCE_CONTINUE

    def _poll_engine_settings(self):
        cfg = load_config()
        cfg["engine"] = self._selected_engine()
        cfg["whisper_cpp_binary"] = self.cpp_binary.get_text().strip()
        cfg["whisper_cpp_model"] = self.cpp_model.get_text().strip()
        cfg["whisper_cpp_gpu"] = self.cpp_gpu.get_active()
        st = engine_status(cfg)
        state = str(st.get("state") or "")
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
            self.engine_setup_btn.set_visible(self._selected_engine() == "faster-whisper")
            self.engine_setup_btn.set_sensitive(True)
            self.engine_setup_btn.set_label(self.t("settings.repair") if state == "error" else self.t("settings.prepare"))
        return GLib.SOURCE_CONTINUE

    def _diagnostics_text(self) -> str:
        status = request("status", timeout=0.35)
        cfg = load_config()
        engine = status.get("engine") if isinstance(status, dict) else {}
        lines = [
            f"WayVoice {__version__}",
            f"OS: {platform.platform()}",
            f"Python: {platform.python_version()}",
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', self.t('diagnostics.not_available'))}",
            f"Session: {os.environ.get('XDG_SESSION_TYPE', self.t('diagnostics.not_available'))}",
            f"Engine: {cfg.get('engine')} / {cfg.get('model')}",
            f"Engine state: {(engine or {}).get('state', 'unknown') if isinstance(engine, dict) else 'unknown'}",
            f"Device: {cfg.get('device')}",
            f"Recognition language: {cfg.get('language')}",
            f"Timeout: {cfg.get('transcription_timeout_sec')}s",
            f"Max recording: {cfg.get('max_recording_sec')}s",
            f"Shortcut: {label_for(str(cfg.get('shortcut', '')))}",
        ]
        if isinstance(status, dict) and status.get("last_error"):
            lines.append(f"Last error: {status.get('last_error')}")
        if isinstance(status, dict) and status.get("last_warning"):
            lines.append(f"Last warning: {status.get('last_warning')}")
        return "\n".join(lines)

    def _copy_diagnostics(self, *_args):
        display = Gdk.Display.get_default()
        if display:
            display.get_clipboard().set_text(self._diagnostics_text())
        if hasattr(self, "toast"):
            self.toast.add_toast(Adw.Toast(title=self.t("toast.diagnostics_copied")))

    def _show_logs_hint(self, *_args):
        self.toast.add_toast(Adw.Toast(title=self.t("toast.logs"), timeout=5))

    def _show_about(self, *_args):
        about = Adw.AboutWindow(transient_for=self, modal=True)
        about.set_application_name("WayVoice")
        about.set_application_icon("io.github.stepan.WayVoice")
        about.set_version(__version__)
        about.set_comments(self.t("about.comments"))
        about.set_developer_name(self.t("about.developer"))
        about.set_developers([self.t("about.developer")])
        about.set_license_type(Gtk.License.GPL_3_0)
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
