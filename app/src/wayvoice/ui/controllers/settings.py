"""SettingsController owns its operations; pages own widgets."""

import subprocess


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib

from ... import languages
from ...config import load_config, save_config
from ...engine import (
    DEFAULT_ENGINE,
    engine_from_config,
    engine_ids,
    engine_status,
    get_engine,
    request_engine_setup,
)
from ...models import forced_language
from ...paths import command_path
from ...shortcut import apply_shortcut, label_for
from ..settings_values import DEVICES, PASTE_MODES, RECORD_VALUES, TIMEOUT_VALUES, UI_LANGUAGE_IDS


class SettingsController:
    def __init__(self, context):
        self.ctx = context
        self._saving = False
        self._poll_running = False
        self._setup_running = False
        self._mutations = []
        self._restart_command = None
        self._baseline_draft = None
        self._saved_draft = None
        self._leave_after_save = None
        self._leave_dialog_open = False


    def _queue_mutation(self, work, done, failed):
        # Both Save and engine preparation write config.json. Preserve click order
        # and keep the atomic writer's temporary file owned by one job at a time.
        self._mutations.append((work, done, failed))
        if len(self._mutations) == 1:
            self._start_mutation()

    def _start_mutation(self):
        work, done, failed = self._mutations[0]
        self.ctx.tasks.run(work,
                           lambda value: self._mutation_finished(done, value),
                           lambda exc: self._mutation_finished(failed, exc))

    def _mutation_finished(self, callback, value):
        try:
            callback(value)
        finally:
            self._mutations.pop(0)
            if self._mutations:
                self._start_mutation()
            elif self._restart_command:
                self._restart_ui()
            else:
                self.ctx.window._mutations_finished()

    def _on_engine_selected(self, *_args):
        self._update_engine_visibility()
        self.ctx.models._sync_model_ui()
        self.ctx.models._refresh_model_state()
        self._poll_engine_settings()

    def _selected_engine(self):
        return engine_ids()[self.ctx.settings.engine.get_selected()]

    def _selected_engine_object(self):
        """The selected engine, or ``None`` while the list is not built yet."""
        if hasattr(self.ctx.settings, "engine"):
            return get_engine(self._selected_engine())
        return get_engine(self.ctx.state.cfg.get("engine", DEFAULT_ENGINE))

    def _selected_engine_uses_models(self) -> bool:
        engine = self._selected_engine_object()
        return bool(engine and engine.uses_models)

    def _update_engine_visibility(self):
        engine = self._selected_engine_object()
        owned = set(engine.settings) if engine else set()
        for key, row in self.ctx.settings._engine_rows:
            row.set_visible(key in owned)
        if hasattr(self.ctx.settings, "engine_setup_btn"):
            self.ctx.settings.engine_setup_btn.set_visible(bool(engine and engine.needs_setup))

    def _draft_values(self):
        model_id = self.ctx.models._selected_model_id()
        forced = forced_language(model_id)
        language = languages.normalize(forced or self.ctx.settings.language.selected_code())
        new_ui_setting = UI_LANGUAGE_IDS[self.ctx.settings.ui_language.get_selected()]
        return {
            "engine": self._selected_engine(),
            "model": model_id,
            "custom_model": self.ctx.settings.custom_model.get_text().strip(),
            "language": language,
            "device": DEVICES[self.ctx.settings.device.get_selected()],
            "vad_filter": self.ctx.settings.vad.get_active(),
            "engine_worker": self.ctx.settings.worker.get_active(),
            "auto_punctuation": self.ctx.settings.auto_punct.get_active(),
            "spoken_punctuation": self.ctx.settings.spoken.get_active(),
            "append_space": self.ctx.settings.append_space.get_active(),
            "paste_mode": PASTE_MODES[self.ctx.settings.paste.get_selected()],
            "notify": self.ctx.settings.notifications.get_active(),
            "notify_transcript": self.ctx.settings.notification_text.get_active(),
            "shortcut": self.ctx.state.shortcut_binding,
            "whisper_cpp_binary": self.ctx.settings.cpp_binary.get_text().strip(),
            "whisper_cpp_model": self.ctx.settings.cpp_model.get_text().strip(),
            "whisper_cpp_gpu": self.ctx.settings.cpp_gpu.get_active(),
            "custom_command": self.ctx.settings.custom_command.get_text().strip(),
            "transcription_timeout_sec": TIMEOUT_VALUES[self.ctx.settings.timeout.get_selected()],
            "max_recording_sec": RECORD_VALUES[self.ctx.settings.max_recording.get_selected()],
            "ui_language": new_ui_setting,
        }

    def remember_draft(self):
        self._baseline_draft = self._draft_values()

    def has_unsaved_changes(self):
        return self._baseline_draft is not None and self._draft_values() != self._baseline_draft

    def confirm_leaving(self, proceed):
        if self._leave_dialog_open or self._leave_after_save:
            return
        from ..dialogs.confirmations import unsaved_confirmation
        self._leave_dialog_open = True

        def response(choice):
            self._leave_dialog_open = False
            if choice == "leave":
                proceed()
            elif choice == "save":
                self._leave_after_save = proceed
                if not self._save():
                    self._leave_after_save = None

        unsaved_confirmation(self.ctx.window, self.ctx.state.t, response)

    def _save(self, *_args):
        if self._saving:
            return
        preset = self.ctx.models._selected_model_preset()
        if self._selected_engine_uses_models() and str(preset["id"]) == "__custom__" and not self.ctx.settings.custom_model.get_text().strip():
            self.ctx.window.toast.add_toast(Adw.Toast(title=self.ctx.state.t("toast.custom_model")))
            return
        updates = self._draft_values()
        self._saved_draft = dict(updates)
        self._saving = True
        if hasattr(self.ctx.window, "save_button"):
            self.ctx.window.save_button.set_sensitive(False)

        def work():
            cfg = load_config()
            cfg.update(updates)
            save_config(cfg)
            ok, msg = apply_shortcut(str(updates["shortcut"]))
            self._prepare_selected_engine(cfg)
            return cfg, ok, msg

        self._queue_mutation(work, self._save_finished, self._save_failed)
        return True

    def _save_failed(self, exc):
        self._leave_after_save = None
        self._saving = False
        if hasattr(self.ctx.window, "save_button"):
            self.ctx.window.save_button.set_sensitive(True)
        self.ctx.window._toast(str(exc))

    def _save_finished(self, result):
        cfg, ok, msg = result
        self._saving = False
        if hasattr(self.ctx.window, "save_button"):
            self.ctx.window.save_button.set_sensitive(True)
        self.ctx.state.cfg = cfg
        if self._saved_draft is not None:
            self._baseline_draft = dict(self._saved_draft)
        proceed, self._leave_after_save = self._leave_after_save, None
        new_ui_setting = cfg["ui_language"]
        language_changed = new_ui_setting != self.ctx.state.ui_lang_setting
        if language_changed and not self.has_unsaved_changes():
            # Restart the UI so the new interface language is applied. The settings
            # binary path is resolved explicitly and passed as $0, so the restart never
            # depends on a login-shell PATH.
            self._restart_command = command_path("wayvoice-settings")
            return
        self.ctx.home.hotkey_label.set_text(label_for(str(cfg["shortcut"])))
        self.ctx.status._update_cards()
        self.ctx.window.toast.add_toast(Adw.Toast(title=self.ctx.state.t("settings.language_pending" if language_changed else "settings.saved") if ok else msg))
        self.ctx.models._refresh_model_state()
        self._poll_engine_settings()
        if proceed and not self.has_unsaved_changes():
            proceed()

    def _restart_ui(self):
        restart_cmd, self._restart_command = self._restart_command, None
        subprocess.Popen(
            ["/bin/sh", "-c", 'sleep 0.35; exec "$0"', restart_cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.ctx.window._quit()

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
        if self._setup_running:
            return
        self._setup_running = True
        engine_id = self._selected_engine()

        def work():
            cfg = load_config()
            cfg["engine"] = engine_id
            save_config(cfg)
            return request_engine_setup(engine_from_config(cfg))

        self._queue_mutation(work, lambda started: self._setup_finished(engine_id, started), self._setup_failed)

    def _setup_failed(self, exc):
        self._setup_running = False
        self.ctx.window._toast(str(exc))
        self._poll_engine_settings()

    def _setup_finished(self, engine_id, started):
        self._setup_running = False
        if started and engine_id == self._selected_engine():
            self.ctx.settings.engine_status_row.set_subtitle(self.ctx.state.t("settings.preparing"))
            self.ctx.settings.engine_setup_btn.set_visible(False)
            self.ctx.settings.engine_spinner.set_visible(True)
            self.ctx.settings.engine_spinner.start()

    def _pending_engine_config(self) -> dict:
        """The config as this window currently shows it, for a status probe.

        Every engine-specific row is taken from its widget, so switching to an engine reports
        that engine's state without saving anything first.
        """
        cfg = dict(self.ctx.state.cfg)
        cfg["engine"] = self._selected_engine()
        for key, row in self.ctx.settings._engine_rows:
            if isinstance(row, Adw.EntryRow):
                cfg[key] = row.get_text().strip()
            elif isinstance(row, Adw.SwitchRow):
                cfg[key] = bool(row.get_active())
        return cfg

    def _poll_engine_settings(self):
        if not self._poll_running:
            self._poll_running = True
            cfg = self._pending_engine_config()
            self.ctx.tasks.run(lambda: engine_status(cfg),
                               lambda st: self._engine_status_ready(cfg, st),
                               lambda exc: self._engine_status_ready(cfg, {"state": "error", "message": str(exc)}))
        return GLib.SOURCE_CONTINUE

    def _engine_status_ready(self, cfg, st):
        self._poll_running = False
        if cfg != self._pending_engine_config():
            self._poll_engine_settings()
            return GLib.SOURCE_REMOVE
        state = str(st.get("state") or "")
        engine = self._selected_engine_object()
        if state == "ready":
            self.ctx.settings.engine_status_row.set_subtitle(self.ctx.state.t("status.ready"))
            self.ctx.settings.engine_setup_btn.set_visible(False)
            self.ctx.settings.engine_spinner.stop()
            self.ctx.settings.engine_spinner.set_visible(False)
        elif state == "installing":
            self.ctx.settings.engine_status_row.set_subtitle(self.ctx.state.t("settings.preparing"))
            self.ctx.settings.engine_setup_btn.set_visible(False)
            self.ctx.settings.engine_spinner.set_visible(True)
            self.ctx.settings.engine_spinner.start()
        else:
            self.ctx.settings.engine_status_row.set_subtitle(str(st.get("message") or self.ctx.state.t("health.not_ready")))
            self.ctx.settings.engine_spinner.stop()
            self.ctx.settings.engine_spinner.set_visible(False)
            self.ctx.settings.engine_setup_btn.set_visible(bool(engine and engine.needs_setup))
            self.ctx.settings.engine_setup_btn.set_sensitive(True)
            self.ctx.settings.engine_setup_btn.set_label(self.ctx.state.t("settings.repair") if state == "error" else self.ctx.state.t("settings.prepare"))
        return GLib.SOURCE_CONTINUE
