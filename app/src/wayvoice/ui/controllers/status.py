"""StatusController owns its operations; pages own widgets."""
import platform
import os
import subprocess
import threading
import time

from ..widgets.labels import clip_subtitle

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

from ..model_presentation import hero_preparation_caption, show_hero_preparation
from ... import __version__
from ... import deps as deps_mod
from ... import injector
from ... import languages
from ... import pkgsys
from ... import service
from ...cli import request
from ...config import load_config, number
from ...engine import engine_from_config, engine_label
from ...i18n import tr
from ...models import display_name
from ...shortcut import label_for


class StatusController:
    def __init__(self, context):
        self.ctx = context
        self._ui_busy = False
        self._restart_requested = False
        self._restart_attempts = 0
        self._restart_after = 0.0
        self._restart_needed = False
        self._restart_exhausted = False
        self._restart_confirmed = False
        self._logs_copying = False
        self._logs_button = None
        self._poll_running = False
        self._diagnostics_copying = False

    #: How much of the journal the button hands over. Enough for a bug report, small
    #: enough that the clipboard owner is not holding a megabyte of text.
    JOURNAL_LINES = 200
    JOURNAL_TIMEOUT = 5.0

    def _toggle(self, *_args):
        command = "cancel" if self._ui_busy else "toggle"
        self.ctx.tasks.run(
            lambda: request(command, timeout=0.8),
            self._toggle_finished,
            lambda exc: self._toggle_finished({"ok": False, "error": str(exc)}),
        )

    def _toggle_finished(self, reply):
        if not reply.get("ok"):
            self.ctx.window._toast(str(reply.get("error") or self.ctx.state.t("toast.dictation_failed")))

    def _update_cards(self, cfg=None):
        cfg = self.ctx.state.cfg if cfg is None else cfg
        engine = engine_from_config(cfg)
        self.ctx.home.engine_card.set_text(engine_label(cfg.get("engine")) or "—")
        # Only an engine with its own model list has a model to show; the others keep
        # theirs (or none) among their own settings.
        self.ctx.home.model_card.set_text(display_name(str(cfg.get("model", "small"))) if engine and engine.uses_models else "—")
        self.ctx.home.paste_card.set_text({"standard": "Ctrl+V", "terminal": "Ctrl+Shift+V", "copy": self.ctx.state.t("paste.clipboard_short")}.get(str(cfg.get("paste_mode")), "—"))

    def _set_state_style(self, state):
        for css in ("recording", "busy", "ready"):
            self.ctx.home.status_pill.remove_css_class(css)
            self.ctx.home.mic_button.remove_css_class(css)
        if state in {"recording", "busy", "ready"}:
            self.ctx.home.status_pill.add_css_class(state)
        if state in {"recording", "busy"}:
            self.ctx.home.mic_button.add_css_class(state)

    def _poll_status(self):
        if not self._poll_running:
            self._poll_running = True
            self.ctx.tasks.run(self._status_snapshot, self._apply_status_snapshot,
                               lambda exc: self._apply_status_snapshot((
                                   {"ok": False, "error": str(exc)}, dict(self.ctx.state.cfg), [])))
        return GLib.SOURCE_CONTINUE

    def _status_snapshot(self):
        return (request("status", timeout=0.12), load_config(),
                self.ctx.integration._missing_required())

    RESTART_LIMIT = 3
    RESTART_DELAY = 5.0

    def _maybe_restart(self, reply):
        self._restart_confirmed = bool(reply.get("ok")) and str(reply.get("version") or "") == __version__
        if reply.get("ok"):
            if self._restart_confirmed:
                if not self._restart_requested:
                    self._restart_needed = False
                    self._restart_attempts = 0
                    self._restart_after = 0.0
                    self._restart_exhausted = False
                return False
            self._restart_needed = True
        if not self._restart_needed or self._restart_requested:
            return False
        if reply.get("recording") or reply.get("busy"):
            return False
        if self._restart_attempts >= self.RESTART_LIMIT:
            if not self._restart_exhausted:
                self._restart_exhausted = True
                self.ctx.window._toast(self.ctx.state.t("toast.restart_exhausted"))
            return False
        if time.monotonic() < self._restart_after:
            return False
        self._restart_requested = True
        self._restart_attempts += 1
        # If the daemon disappeared, restore service without forcing a restart
        # of a process whose recording/busy state we cannot observe.
        work = service.restart_daemon if reply.get("ok") else service.start_daemon
        accepted = self.ctx.tasks.run(work, self._restart_finished,
                                      lambda exc: self._restart_finished(False, str(exc)))
        if not accepted:
            self._restart_requested = False
        return bool(accepted)

    def _restart_finished(self, ready, detail=""):
        self._restart_requested = False
        if self._restart_confirmed:
            self._restart_needed = False
            self._restart_attempts = 0
            self._restart_after = 0.0
            self._restart_exhausted = False
            return
        self._restart_after = time.monotonic() + self.RESTART_DELAY * self._restart_attempts
        # A successful ping does not prove the new version; the next status does.
        if not ready:
            message = self.ctx.state.t("toast.restart_failed")
            if detail:
                message += f": {detail}"
            self.ctx.window._toast(message)

    def _apply_status_snapshot(self, snapshot):
        self._poll_running = False
        reply, cfg, missing_deps = snapshot
        self._update_cards(cfg)
        restarting = self._maybe_restart(reply)
        if not reply.get("ok"):
            self._ui_busy = False
            self.ctx.home.mic_button.set_sensitive(False)
            self.ctx.home.status_pill.set_text(self.ctx.state.t("status.start"))
            self.ctx.home.hero_state.set_text(self.ctx.state.t("hero.starting"))
            self.ctx.home.hero_caption.set_text("")
            self.ctx.home.health_summary.set_text(self.ctx.state.t("status.waiting"))
            self.ctx.home.health_detail.set_text("")
            self._set_state_style("offline")
            return GLib.SOURCE_CONTINUE

        if restarting:
            return GLib.SOURCE_CONTINUE

        engine = reply.get("engine") or {}
        est = str(engine.get("state") or "missing")
        self.ctx.models._apply_download_state(reply.get("model"))
        recording = bool(reply.get("recording"))
        busy = bool(reply.get("busy"))
        self._ui_busy = busy
        error = str(reply.get("last_error") or "")
        warning = str(reply.get("last_warning") or "")
        text = str(reply.get("last_text") or "")
        shortcut = str(reply.get("shortcut") or label_for(self.ctx.state.shortcut_binding))
        self.ctx.home.hotkey_label.set_text(shortcut)

        if recording:
            state = "recording"
            elapsed = int(float(reply.get("recording_seconds") or 0))
            self.ctx.home.mic_button.set_sensitive(True)
            self.ctx.home.status_pill.set_text(self.ctx.state.t("status.recording"))
            self.ctx.home.hero_state.set_text(self.ctx.state.t("hero.recording"))
            self.ctx.home.hero_caption.set_text(f"{self.ctx.state.t('hero.recording_hint')}  ·  {elapsed}s")
            self.ctx.home.mic_icon.set_from_icon_name("media-playback-stop-symbolic")
        elif busy:
            state = "busy"
            elapsed = int(float(reply.get("busy_seconds") or 0))
            limit = number(cfg, "transcription_timeout_sec", 90)
            self.ctx.home.mic_button.set_sensitive(True)
            self.ctx.home.status_pill.set_text(self.ctx.state.t("status.transcribing"))
            self.ctx.home.hero_state.set_text(self.ctx.state.t("hero.transcribing"))
            self.ctx.home.hero_caption.set_text(f"{self.ctx.state.t('hero.cancel_hint')}  ·  {elapsed}/{limit}s")
            self.ctx.home.mic_icon.set_from_icon_name("process-stop-symbolic")
        else:
            preparation = hero_preparation_caption(self.ctx, reply.get("model"))
            if preparation is not None:
                # The daemon is fetching or loading the model. The settings window
                # paints its row, but this window used to keep saying "press and
                # speak" for the whole wait - an invitation the hot key cannot
                # honour yet, since starting now answers "model missing".
                state = "busy"
                self.ctx.home.mic_button.set_sensitive(True)
                show_hero_preparation(self.ctx, *preparation)
            elif est == "ready":
                state = "ready"
                self.ctx.home.mic_button.set_sensitive(True)
                self.ctx.home.status_pill.set_text(self.ctx.state.t("status.ready"))
                self.ctx.home.hero_state.set_text(self.ctx.state.t("hero.record"))
                self.ctx.home.hero_caption.set_text(f"{self.ctx.state.t('shortcut.global')}: {shortcut}")
                self.ctx.home.mic_icon.set_from_icon_name("audio-input-microphone-symbolic")
            else:
                state = "offline"
                self.ctx.home.mic_button.set_sensitive(False)
                self.ctx.home.status_pill.set_text(self.ctx.state.t("status.engine"))
                self.ctx.home.hero_state.set_text(self.ctx.state.t("hero.not_ready"))
                self.ctx.home.hero_caption.set_text(str(engine.get("message") or self.ctx.state.t("hero.open_settings")))
                self.ctx.home.mic_icon.set_from_icon_name("emblem-system-symbolic")

        self._set_state_style(state)
        # Missing blocking dependencies are reported as a warning, below a real error and
        # above a plain daemon warning: error > missing dependency > warning > engine
        # state.
        dep_warning = self.ctx.state.t("health.deps_missing", names=", ".join(d.label for d in missing_deps)) if missing_deps else ""
        config_broken = str(reply.get("config_error") or "")
        if config_broken:
            # Above a warning and below a real error: the daemon works, but on
            # settings the user did not choose, and they should know that before
            # they start wondering why their shortcut or model changed.
            self.ctx.home.health_summary.set_text(self.ctx.state.t("health.warning"))
            self.ctx.home.health_detail.set_text(clip_subtitle(config_broken))
            self.ctx.home.health_detail.remove_css_class("error-text")
            self.ctx.home.health_detail.add_css_class("warning-text")
        elif error:
            self.ctx.home.health_summary.set_text(self.ctx.state.t("health.error"))
            self.ctx.home.health_detail.set_text(error)
            self.ctx.home.health_detail.remove_css_class("warning-text")
            self.ctx.home.health_detail.add_css_class("error-text")
        elif dep_warning:
            self.ctx.home.health_summary.set_text(self.ctx.state.t("health.warning"))
            self.ctx.home.health_detail.set_text(dep_warning)
            self.ctx.home.health_detail.remove_css_class("error-text")
            self.ctx.home.health_detail.add_css_class("warning-text")
        elif warning:
            self.ctx.home.health_summary.set_text(self.ctx.state.t("health.warning"))
            self.ctx.home.health_detail.set_text(warning)
            self.ctx.home.health_detail.remove_css_class("error-text")
            self.ctx.home.health_detail.add_css_class("warning-text")
        else:
            self.ctx.home.health_summary.set_text(self.ctx.state.t("health.ready") if est == "ready" else self.ctx.state.t("health.not_ready"))
            self.ctx.home.health_detail.set_text("" if est == "ready" else str(engine.get("message") or ""))
            self.ctx.home.health_detail.remove_css_class("error-text")
            self.ctx.home.health_detail.remove_css_class("warning-text")

        if self.ctx.integration._dep_rows:
            self.ctx.integration._refresh_dependency_rows()

        if text:
            self.ctx.home.last_text.remove_css_class("muted")
            self.ctx.home.last_text.set_text(text)
            self.ctx.home.transcript_meta.set_text(self.ctx.state.t("transcript.last"))
        else:
            self.ctx.home.transcript_meta.set_text("")
        return GLib.SOURCE_CONTINUE

    def _language_label(self, value) -> str:
        """Recognition language as the user knows it, plus the raw code.

        The name makes the report understandable; the code makes it reproducible, and it is
        the only part a developer can paste.
        """
        code = languages.normalize(value)
        name = tr("language.auto", self.ctx.state.ui_lang) if code == languages.AUTO else languages.display_name(code, self.ctx.state.ui_lang)
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
            f"Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', self.ctx.state.t('diagnostics.not_available'))}",
            f"Session: {os.environ.get('XDG_SESSION_TYPE', self.ctx.state.t('diagnostics.not_available'))}",
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
        if self._diagnostics_copying:
            return
        self._diagnostics_copying = True
        language = self.ctx.state.ui_lang

        def work():
            injector.copy_to_clipboard(self._diagnostics_text(), language)

        self.ctx.tasks.run(work, lambda _: self._diagnostics_finished(None),
                           self._diagnostics_finished)

    def _diagnostics_finished(self, problem):
        self._diagnostics_copying = False
        if problem is not None:
            self.ctx.window._toast(self.ctx.state.t("toast.diagnostics_failed", detail=str(problem)))
        else:
            self.ctx.window._toast(self.ctx.state.t("toast.diagnostics_copied"))

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
                self.ctx.tasks.idle(self._copy_logs_finished, str(exc))
                return
            if not report.strip():
                self.ctx.tasks.idle(self._copy_logs_finished, None, True)
                return
            try:
                injector.copy_to_clipboard(report, self.ctx.state.ui_lang)
            except injector.InjectionError as exc:
                self.ctx.tasks.idle(self._copy_logs_finished, str(exc))
                return
            self.ctx.tasks.idle(self._copy_logs_finished, None)

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
            self.ctx.window._toast(self.ctx.state.t("toast.logs_failed", detail=str(exc)))

    def _copy_logs_finished(self, problem, empty: bool = False) -> bool:
        button, self._logs_button = self._logs_button, None
        self._logs_copying = False
        if button is not None:
            button.set_sensitive(True)
        if problem is not None:
            self.ctx.window._toast(self.ctx.state.t("toast.logs_failed", detail=problem))
        elif empty:
            self.ctx.window._toast(self.ctx.state.t("toast.logs_empty"))
        else:
            self.ctx.window._toast(self.ctx.state.t("toast.logs_copied"))
        return GLib.SOURCE_REMOVE

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