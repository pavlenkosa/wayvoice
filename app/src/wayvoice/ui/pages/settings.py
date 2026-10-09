"""SettingsPage constructs and owns its widgets."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from ... import languages
from ...config import number
from ...engine import DEFAULT_ENGINE
from ...models import MODEL_PRESETS, PRESET_LABELS, preset_index
from ...shortcut import label_for
from ..settings_values import DEVICES, DEVICE_NAMES, PASTE_MODES, RECORD_VALUES, TIMEOUT_VALUES, UI_LANGUAGE_IDS
from ..widgets.labels import nearest_index, index_or_zero
from ..widgets.language_picker import LanguagePicker, engine_choices, language_choices


class SettingsPage:
    def __init__(self, context):
        self.ctx = context
        context.settings = self
        self.root = self._build_settings()

    def focus_section(self, target):
        widget = self.engine if target == 'engine_status_row' else getattr(self, target, None)
        if target == 'model_state_row' and not self.model_state_row.get_visible():
            widget = self.engine
        if widget is not None:
            widget.set_focusable(True)
            widget.grab_focus()

    def _build_settings(self):
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page = Adw.PreferencesPage()
        page.set_halign(Gtk.Align.CENTER)
        clamp = Adw.Clamp(maximum_size=780, tightening_threshold=600)
        clamp.set_child(page)
        scroller.set_child(clamp)

        engine_group = Adw.PreferencesGroup(title=self.ctx.state.t("settings.recognition"), description=self.ctx.state.t("settings.draft_hint"))
        page.add(engine_group)
        engine_choice_ids, engine_choice_labels = engine_choices()
        self.engine = Adw.ComboRow(title=self.ctx.state.t("settings.engine"))
        self.engine.set_model(Gtk.StringList.new(engine_choice_labels))
        self.engine.set_selected(index_or_zero(engine_choice_ids, self.ctx.state.cfg.get("engine", DEFAULT_ENGINE)))
        self.engine.connect("notify::selected", self.ctx.preferences._on_engine_selected)
        engine_group.add(self.engine)

        self.engine_status_row = Adw.ActionRow(title=self.ctx.state.t("settings.engine_state"), subtitle=self.ctx.state.t("health.checking"))
        self.engine_setup_btn = Gtk.Button(label=self.ctx.state.t("settings.prepare"), valign=Gtk.Align.CENTER)
        self.engine_setup_btn.connect("clicked", self.ctx.preferences._setup_engine)
        self.engine_status_row.add_suffix(self.engine_setup_btn)
        self.engine_spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self.engine_spinner.set_visible(False)
        self.engine_status_row.add_suffix(self.engine_spinner)
        engine_group.add(self.engine_status_row)

        self.model = Adw.ComboRow(title=self.ctx.state.t("settings.model"))
        self.model.set_model(Gtk.StringList.new(PRESET_LABELS))
        self.model.set_selected(preset_index(str(self.ctx.state.cfg.get("model", "small"))))
        self.model.connect("notify::selected", self.ctx.models._on_model_selected)
        engine_group.add(self.model)

        self.custom_model = Adw.EntryRow(title=self.ctx.state.t("settings.custom_model"))
        current_model = str(self.ctx.state.cfg.get("model", "small"))
        custom_value = str(self.ctx.state.cfg.get("custom_model", ""))
        if preset_index(current_model) == len(MODEL_PRESETS) - 1 and current_model != "__custom__":
            custom_value = current_model
        self.custom_model.set_text(custom_value)
        # Typing in the custom field fires this per keystroke, so it goes through the
        # same coalescing as everything else instead of scanning per character.
        self.custom_model.connect("changed", lambda *_: self.ctx.models._refresh_model_state())
        engine_group.add(self.custom_model)

        self.model_state_row = Adw.ActionRow(title=self.ctx.state.t("store.state"), subtitle=self.ctx.state.t("health.checking"))
        # The download has to be reachable without changing the model: a model that is
        # not on disk used to be fetched as a side effect of choosing a different one,
        # which is a place a user goes to only by accident.
        self.model_fetch_btn = Gtk.Button(
            label=self.ctx.state.t("store.download_now"),
            valign=Gtk.Align.CENTER,
        )
        self.model_fetch_btn.connect("clicked", self.ctx.models._ask_to_fetch_the_model)
        self.model_fetch_btn.set_visible(False)
        self.model_state_row.add_suffix(self.model_fetch_btn)
        #: The last state the window heard about, so the button can be shown or hidden
        #: from either side: the cache walk says what is on disk, the daemon says what
        #: is being done about it.
        self.model_delete_btn = Gtk.Button(
            label=self.ctx.state.t("common.delete"),
            valign=Gtk.Align.CENTER,
            sensitive=False,
        )
        self.model_delete_btn.add_css_class("destructive-action")
        self.model_delete_btn.connect("clicked", self.ctx.models._ask_delete_model)
        self.model_state_row.add_suffix(self.model_delete_btn)
        engine_group.add(self.model_state_row)

        # Downloading a model is the one thing here that takes minutes, so it gets its
        # own row with a real bar and a cancel button instead of a subtitle that would
        # have to be re-read to change.
        self.model_download_row = Adw.ActionRow(title=self.ctx.state.t("store.download"))
        self.model_download_bar = Gtk.ProgressBar(
            valign=Gtk.Align.CENTER,
            hexpand=True,
            show_text=False,
        )
        self.model_download_row.add_suffix(self.model_download_bar)
        self.model_download_cancel_btn = Gtk.Button(
            label=self.ctx.state.t("common.cancel"),
            valign=Gtk.Align.CENTER,
        )
        self.model_download_cancel_btn.connect("clicked", self.ctx.models._cancel_model_download)
        self.model_download_row.add_suffix(self.model_download_cancel_btn)
        self.model_download_row.set_visible(False)
        engine_group.add(self.model_download_row)
        self.model_error_expander = Gtk.Expander(label=self.ctx.state.t("health.details"))
        self.model_error_detail = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.model_error_expander.set_child(self.model_error_detail)
        self.model_error_expander.set_visible(False)
        engine_group.add(self.model_error_expander)

        self.model_disk_row = Adw.ActionRow(
            title=self.ctx.state.t("store.storage"),
            subtitle=self.ctx.state.t("store.disk", size="…", cache="…", free="…"),
        )
        engine_group.add(self.model_disk_row)

        self.device = Adw.ComboRow(title=self.ctx.state.t("settings.device"))
        self.device.set_model(Gtk.StringList.new(DEVICE_NAMES))
        self.device.set_selected(index_or_zero(DEVICES, self.ctx.state.cfg.get("device", "auto")))
        engine_group.add(self.device)
        self.vad = Adw.SwitchRow(title=self.ctx.state.t("settings.vad"))
        self.vad.set_active(bool(self.ctx.state.cfg.get("vad_filter", True)))
        engine_group.add(self.vad)
        self.worker = Adw.SwitchRow(title=self.ctx.state.t("settings.worker"), subtitle=self.ctx.state.t("settings.worker_sub"))
        self.worker.set_active(bool(self.ctx.state.cfg.get("engine_worker", True)))
        engine_group.add(self.worker)

        self.cpp_binary = Adw.EntryRow(title=self.ctx.state.t("settings.cpp_binary"))
        self.cpp_binary.set_text(str(self.ctx.state.cfg.get("whisper_cpp_binary", "")))
        engine_group.add(self.cpp_binary)
        self.cpp_model = Adw.EntryRow(title=self.ctx.state.t("settings.cpp_model"))
        self.cpp_model.set_text(str(self.ctx.state.cfg.get("whisper_cpp_model", "")))
        engine_group.add(self.cpp_model)
        self.cpp_gpu = Adw.SwitchRow(title=self.ctx.state.t("settings.cpp_gpu"))
        self.cpp_gpu.set_active(bool(self.ctx.state.cfg.get("whisper_cpp_gpu", True)))
        engine_group.add(self.cpp_gpu)

        self.custom_command = Adw.EntryRow(title=self.ctx.state.t("settings.custom_command"))
        self.custom_command.set_text(str(self.ctx.state.cfg.get("custom_command", "")))
        self.custom_command.set_tooltip_text(self.ctx.state.t("settings.custom_command_sub"))
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

        text_group = Adw.PreferencesGroup(title=self.ctx.state.t("settings.text"))
        page.add(text_group)
        language_codes, language_labels = language_choices(self.ctx.state.ui_lang)
        self.language = LanguagePicker(
            self.ctx.state.t("settings.language"),
            language_codes,
            language_labels,
            # Old config.json files may hold anything at all here, including a
            # language Whisper no longer knows; normalize() turns all of that
            # into a code the list actually has.
            index_or_zero(language_codes, languages.normalize(self.ctx.state.cfg.get("language"))),
        )
        text_group.add(self.language.row)
        self.auto_punct = Adw.SwitchRow(title=self.ctx.state.t("settings.punctuation"))
        self.auto_punct.set_active(bool(self.ctx.state.cfg.get("auto_punctuation", True)))
        text_group.add(self.auto_punct)
        self.spoken = Adw.SwitchRow(title=self.ctx.state.t("settings.spoken"), subtitle=self.ctx.state.t("settings.spoken_sub"))
        self.spoken.set_active(bool(self.ctx.state.cfg.get("spoken_punctuation", True)))
        text_group.add(self.spoken)
        self.append_space = Adw.SwitchRow(title=self.ctx.state.t("settings.append_space"))
        self.append_space.set_active(bool(self.ctx.state.cfg.get("append_space", True)))
        text_group.add(self.append_space)

        control_group = Adw.PreferencesGroup(title=self.ctx.state.t("settings.control"))
        page.add(control_group)
        self.shortcut_row = Adw.ActionRow(title=self.ctx.state.t("settings.shortcut"), subtitle=label_for(self.ctx.state.shortcut_binding))
        shortcut_btn = Gtk.Button(label=self.ctx.state.t("settings.change"), valign=Gtk.Align.CENTER)
        shortcut_btn.connect("clicked", self.ctx.shortcut._open_shortcut_capture)
        self.shortcut_row.add_suffix(shortcut_btn)
        control_group.add(self.shortcut_row)
        paste_labels = ["Ctrl+V", "Ctrl+Shift+V", self.ctx.state.t("paste.clipboard")]
        self.paste = Adw.ComboRow(title=self.ctx.state.t("settings.paste"))
        self.paste.set_model(Gtk.StringList.new(paste_labels))
        self.paste.set_selected(index_or_zero(PASTE_MODES, self.ctx.state.cfg.get("paste_mode", "standard")))
        control_group.add(self.paste)
        self.notifications = Adw.SwitchRow(title=self.ctx.state.t("settings.notifications"))
        self.notifications.set_active(bool(self.ctx.state.cfg.get("notify", True)))
        control_group.add(self.notifications)
        self.notification_text = Adw.SwitchRow(
            title=self.ctx.state.t("settings.notification_text"),
            subtitle=self.ctx.state.t("settings.notification_text_sub"))
        self.notification_text.set_active(self.ctx.state.cfg.get("notify_transcript") is True)
        control_group.add(self.notification_text)

        self.ctx.integration._build_dependencies_group(page)

        safety_group = Adw.PreferencesGroup(title=self.ctx.state.t("settings.safety"))
        page.add(safety_group)
        self.timeout = Adw.ComboRow(title=self.ctx.state.t("settings.timeout"), subtitle=self.ctx.state.t("settings.timeout_sub"))
        self.timeout.set_model(Gtk.StringList.new([self.ctx.state.duration_label(x) for x in TIMEOUT_VALUES]))
        self.timeout.set_selected(nearest_index(TIMEOUT_VALUES, number(self.ctx.state.cfg, "transcription_timeout_sec", 90)))
        safety_group.add(self.timeout)
        self.max_recording = Adw.ComboRow(title=self.ctx.state.t("settings.max_recording"), subtitle=self.ctx.state.t("settings.max_recording_sub"))
        self.max_recording.set_model(Gtk.StringList.new([self.ctx.state.duration_label(x) for x in RECORD_VALUES]))
        self.max_recording.set_selected(nearest_index(RECORD_VALUES, number(self.ctx.state.cfg, "max_recording_sec", 120)))
        safety_group.add(self.max_recording)

        interface_group = Adw.PreferencesGroup(title=self.ctx.state.t("settings.interface"))
        page.add(interface_group)
        self.ui_language = Adw.ComboRow(title=self.ctx.state.t("settings.ui_language"))
        self.ui_language.set_model(Gtk.StringList.new([self.ctx.state.t("ui.auto"), self.ctx.state.t("ui.russian"), self.ctx.state.t("ui.english")]))
        self.ui_language.set_selected(index_or_zero(UI_LANGUAGE_IDS, self.ctx.state.ui_lang_setting))
        interface_group.add(self.ui_language)

        diag_group = Adw.PreferencesGroup(title=self.ctx.state.t("settings.diagnostics"))
        page.add(diag_group)
        diag_row = Adw.ActionRow(title=self.ctx.state.t("settings.copy_diagnostics"), subtitle=self.ctx.state.t("settings.copy_diagnostics_sub"))
        diag_btn = Gtk.Button(label=self.ctx.state.t("settings.copy_diagnostics"), valign=Gtk.Align.CENTER)
        diag_btn.connect("clicked", self.ctx.status._copy_diagnostics)
        diag_row.add_suffix(diag_btn)
        diag_group.add(diag_row)
        logs_row = Adw.ActionRow(title=self.ctx.state.t("settings.copy_logs"), subtitle=self.ctx.state.t("settings.copy_logs_sub"))
        logs_btn = Gtk.Button(label=self.ctx.state.t("settings.copy_logs"), valign=Gtk.Align.CENTER)
        logs_btn.connect("clicked", self.ctx.status._copy_logs)
        logs_row.add_suffix(logs_btn)
        diag_group.add(logs_row)

        # Saving is in the header bar; nothing else belongs in this trailing
        # group and an empty one would paint a gap, so the page ends with the
        # diagnostics above.

        return scroller
