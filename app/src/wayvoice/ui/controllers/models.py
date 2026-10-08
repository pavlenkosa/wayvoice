"""ModelsController owns its operations; pages own widgets."""

import json
import sys
import threading

from gi.repository import GLib

from ... import model_store
from ...models import MODEL_PRESETS, display_name, preset_subtitle
from ...cli import request
from ...engine import stop_worker, worker_info
from ..model_presentation import model_state_text


class ModelsController:
    def __init__(self, context):
        self.ctx = context
        self._model_entry = {}
        self._download_report = {}
        self._model_refresh_busy = False
        self._model_refresh_pending = False
        self._download_confirmation_for = None
        self._model_deleting = False
        self._prepare_request_seq = 0


    def _selected_model_preset(self):
        idx = int(self.ctx.settings.model.get_selected()) if hasattr(self.ctx.settings, "model") else 0
        if idx < 0 or idx >= len(MODEL_PRESETS):
            idx = 0
        return MODEL_PRESETS[idx]

    def _selected_model_id(self) -> str:
        preset = self._selected_model_preset()
        model_id = str(preset["id"])
        if model_id == "__custom__":
            value = self.ctx.settings.custom_model.get_text().strip() if hasattr(self.ctx.settings, "custom_model") else ""
            return value or str(self.ctx.state.cfg.get("custom_model", "")).strip() or "small"
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
        if entry.get("inference_ready", entry.get("downloaded")):
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
        from ..dialogs.confirmations import download_confirmation
        engine_id = self.ctx.preferences._selected_engine_object().id
        confirmed = lambda button, win: self._download_confirmed(button, win, model_id, engine_id)
        download_confirmation(self.ctx.window, model_id, size_bytes, self.ctx.state.t, self.ctx.state.ui_lang, confirmed)

    def _download_confirmed(self, _button, win, model_id, engine_id) -> None:
        win.close()
        self._ask_daemon_to_prepare_model(model_id, engine_id)

    def _ask_daemon_to_prepare_model(self, model_id=None, engine_id=None) -> None:
        """Prepare a captured draft target without saving it or replacing the worker."""
        model_id = model_id or self._selected_model_id()
        engine_id = engine_id or self.ctx.preferences._selected_engine_object().id
        self._prepare_request_seq += 1
        sequence = self._prepare_request_seq
        command = "prepare-model " + json.dumps({"engine": engine_id, "model": model_id})
        self.ctx.tasks.run(
            lambda: request(command, timeout=5.0),
            lambda reply: self._handle_prepare_model_reply(reply, model_id, engine_id, sequence),
            lambda exc: self._handle_prepare_model_reply({"ok": False, "error": str(exc)}, model_id, engine_id, sequence),
        )

    def _handle_prepare_model_reply(self, reply: dict, model_id=None, engine_id=None, sequence=None) -> None:
        """Say what the press did, the moment the daemon answers.

        The status poll can take another interval to report the new state, and a
        network fetch reports its first bytes even later: a press that starts a
        download therefore looked like a press that did nothing. An accepted
        request is confirmed at once - a toast and an optimistic row, which the
        first state report then corrects into the real progress.
        A refusal the window swallowed looked exactly like a button that does
        nothing: the row kept its old state and no word explained the press.
        One refusal stays silent on purpose: ``not_applicable`` is the daemon's
        word for a value that is not a Hub repository at all, and the model row
        already says that about a local folder - repeating it as a toast on
        every selection would be noise.
        """
        if sequence is not None and sequence != self._prepare_request_seq:
            return
        if not isinstance(reply, dict):
            return
        if not reply.get("ok"):
            if str(reply.get("state") or "") == "not_applicable":
                return
            error = str(reply.get("error") or "")
            if error:
                self.ctx.window._toast(error)
            return
        model_id = str(model_id or reply.get("model") or "")
        if not model_id:
            return
        name = display_name(model_id)
        self.ctx.window._toast(self.ctx.state.t("toast.prepare_started", model=name))
        # Optimistic row: the daemon is now fetching or loading the selected
        # model, but the poll that would say so may be up to an interval away.
        # Painting the accepted state at once is the difference between a
        # button that visibly started something and one the user presses
        # again because nothing seemed to happen.
        if engine_id and engine_id != self.ctx.preferences._selected_engine_object().id:
            return
        phase = str(reply.get("state") or "ready")
        if phase not in {"downloading", "warming"}:
            self._refresh_model_state()
            return
        self._download_report = {
            "model": model_id,
            "download": {
                "state": phase,
                "model": model_id,
                "done_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "warming": phase == "warming",
            },
        }
        if model_id == self._selected_model_id():
            self._apply_download_state(self._download_report)

    def _sync_model_ui(self):
        if not hasattr(self.ctx.settings, "model"):
            return
        preset = self._selected_model_preset()
        self.ctx.settings.model.set_subtitle(preset_subtitle(preset, self.ctx.state.ui_lang))
        is_custom = str(preset["id"]) == "__custom__"
        uses_models = self.ctx.preferences._selected_engine_uses_models()
        self.ctx.settings.custom_model.set_visible(uses_models and is_custom)
        self.ctx.settings.model_state_row.set_visible(uses_models)
        self.ctx.settings.model_disk_row.set_visible(uses_models)
        forced = preset.get("language")
        if uses_models and forced:
            # The model decides the language; show which one, but do not let it
            # be edited into something the model was not trained for.
            self.ctx.settings.language.select_code(str(forced))
            self.ctx.settings.language.set_sensitive(False)
        else:
            self.ctx.settings.language.set_sensitive(True)

    # ------------------------------------------------------------------
    # Model files on disk
    # ------------------------------------------------------------------

    def _apply_model_state(self, entry: dict, free_bytes: int, total_bytes: int, held: dict, cache_bytes: int = 0) -> None:
        """Paint the row from a result computed off the UI thread."""
        self._model_entry = dict(entry) if isinstance(entry, dict) else {}
        text, deletable = model_state_text(self.ctx, entry)
        # The whole hub is shown next to our own total: the cache is shared with other
        # applications, and a number that explains the folder beats one that looks
        # like it should.
        self.ctx.settings.model_disk_row.set_subtitle(self.ctx.state.t(
            "store.disk",
            size=model_store.human_size(total_bytes, self.ctx.state.ui_lang),
            cache=model_store.human_size(cache_bytes, self.ctx.state.ui_lang),
            free=model_store.human_size(free_bytes, self.ctx.state.ui_lang),
        ))
        # A model held in the warm worker cannot go away while the worker keeps it: the
        # next dictation would claim a model that is not on disk. The button explains
        # that instead of silently doing nothing.
        if deletable and held.get("running") and str(held.get("model") or "") == str(entry.get("id") or ""):
            deletable = False
            text = f"{text} · {self.ctx.state.t('store.delete_busy')}"
        self.ctx.settings.model_state_row.set_subtitle(text)
        self.ctx.settings.model_delete_btn.set_sensitive(deletable)
        # A model that is not there has no button to show; a local folder is greyed out
        # rather than hidden, so "you cannot delete this" is visible instead of looking
        # like a missing feature.
        self.ctx.settings.model_delete_btn.set_visible(str(entry.get("kind") or "") != "custom")
        self._refresh_fetch_button()

    def _refresh_fetch_button(self) -> None:
        """Show the download button exactly when it is the right thing to press.

        A hub model that is not on disk, with nothing being done about it yet. While a
        download or a warm-up runs, the row below already shows the progress and the way to
        stop it, so a second button would be a third answer to the same question.
        """
        if not hasattr(self.ctx.settings, "model_fetch_btn"):
            return
        entry = self._model_entry if isinstance(self._model_entry, dict) else {}
        report = self._download_report if isinstance(self._download_report, dict) else {}
        download = report.get("download") if isinstance(report.get("download"), dict) else {}
        phase = str(download.get("state") or "idle")
        model_id = str(entry.get("id") or "")
        # Check if the model is a repo model (hub model) and not downloaded
        is_repo_model = str(entry.get("kind") or "") == model_store.KIND_REPO
        is_not_downloaded = not entry.get("inference_ready", entry.get("downloaded"))
        # Determine if button should be visible
        wanted = (
            is_repo_model
            and is_not_downloaded
            and phase in {"idle", "error", "ready"}
            and str(download.get("model") or "") in {"", model_id}
        )
        self.ctx.settings.model_fetch_btn.set_visible(wanted)

    def _ask_to_fetch_the_model(self, *_args) -> None:
        """The row's own way in: the same question a new choice asks."""
        entry = self._model_entry if isinstance(self._model_entry, dict) else {}
        model_id = str(entry.get("id") or "")
        if not model_id:
            return
        if str(entry.get("kind") or "") != model_store.KIND_REPO:
            # A local path cannot be fetched and its row says so in its own subtitle; a
            # button here would report success and change nothing.
            self.ctx.window._toast(self.ctx.state.t("store.refuse_local"))
            return
        self._ask_about_download(model_id, int(entry.get("size_bytes") or 0))

    def _apply_download_state(self, report: dict | None) -> None:
        """Paint the download row from the daemon's model report.

        The bar follows what the daemon says rather than anything the window measures: the
        download runs in the daemon, and a window watching the cache directory would be
        reporting a different thing from the one the user is waiting for.
        """
        if not hasattr(self.ctx.settings, "model_download_row"):
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
            self.ctx.settings.model_download_row.set_visible(False)
            return
        if state == "warming":
            # No download: the model is on disk and is being read into memory so that the
            # first dictation is as fast as the rest.
            self.ctx.settings.model_download_cancel_btn.set_sensitive(False)
            self.ctx.settings.model_download_bar.pulse()
            self.ctx.settings.model_download_row.set_title(
                self.ctx.state.t("store.warming", model=display_name(model_id) if model_id else "")
            )
            self.ctx.settings.model_download_row.set_subtitle(self.ctx.state.t("store.warming_sub"))
            self.ctx.settings.model_download_row.set_visible(True)
            return
        if state == "downloading":
            done = int(download.get("done_bytes") or 0)
            total = int(download.get("total_bytes") or 0)
            name = display_name(model_id) if model_id else ""
            self.ctx.settings.model_download_cancel_btn.set_sensitive(True)
            if download.get("warming"):
                # The weights are down and the model is going into memory: for a moment
                # there is nothing to measure, and it would look like a download that
                # stopped.
                self.ctx.settings.model_download_bar.pulse()
                self.ctx.settings.model_download_row.set_title(self.ctx.state.t("store.warming", model=name))
                self.ctx.settings.model_download_row.set_subtitle(self.ctx.state.t("store.warming_sub"))
                # A load that cannot be interrupted: offering to stop it would be a button
                # that reports success and changes nothing.
                self.ctx.settings.model_download_cancel_btn.set_sensitive(False)
            elif total > 0:
                if done > 0:
                    self.ctx.settings.model_download_bar.set_fraction(min(1.0, done / total))
                else:
                    # Nothing has arrived yet but the size is known: a fixed fraction of
                    # zero would look stuck.
                    self.ctx.settings.model_download_bar.pulse()
                self.ctx.settings.model_download_row.set_title(self.ctx.state.t("store.downloading", model=name))
                self.ctx.settings.model_download_row.set_subtitle(self.ctx.state.t(
                    "store.download_progress",
                    done=model_store.human_size(done, self.ctx.state.ui_lang),
                    total=model_store.human_size(total, self.ctx.state.ui_lang),
                    percent=int(min(100, done * 100 / total)),
                ))
            else:
                self.ctx.settings.model_download_bar.pulse()
                self.ctx.settings.model_download_row.set_title(self.ctx.state.t("store.downloading", model=name))
                self.ctx.settings.model_download_row.set_subtitle(self.ctx.state.t("store.download_unknown"))
            self.ctx.settings.model_download_row.set_visible(True)
            return
        if state == "error" and str(download.get("error") or ""):
            self.ctx.settings.model_download_bar.set_fraction(0.0)
            self.ctx.settings.model_download_row.set_title(self.ctx.state.t("store.download_failed"))
            self.ctx.settings.model_download_row.set_subtitle(str(download.get("error")))
            self.ctx.settings.model_download_row.set_visible(True)
            return
        self.ctx.settings.model_download_row.set_visible(False)

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



    def _refresh_model_state(self) -> None:
        """Recompute the model row, off the GTK main loop.

        Walking the cache follows every snapshot symlink into every blob and stats the
        results - ~6 ms warm here, slower on the first pass after a cold start - so it runs
        in a worker thread and comes back through ``GLib.idle_add``. A run already in flight
        is not joined by another one; the request is remembered and re-run when the first
        finishes, so the row cannot show a state older than the last change.
        """
        if not hasattr(self.ctx.settings, "model_state_row"):
            return
        if self._model_refresh_busy:
            self._model_refresh_pending = True
            return
        self._model_refresh_busy = True
        self._model_refresh_pending = False
        model_id = self._selected_model_id()
        self.ctx.tasks.run(lambda: self._model_state_worker(model_id), lambda _: None,
                           self._model_refresh_failed)

    def _model_refresh_failed(self, exc):
        self._model_refresh_busy = False
        self._model_refresh_pending = False
        self.ctx.window._toast(str(exc))

    def _model_state_worker(self, model_id: str) -> None:
        try:
            entry = model_store.describe(model_id)
            try:
                model_store.inference_dir(model_id)
                entry["inference_ready"] = True
            except RuntimeError:
                entry["inference_ready"] = False
            free_bytes = model_store.disk_free()
            total_bytes = model_store.total_size()
            cache_bytes = model_store.hub_size()
            # The worker ping has a timeout of its own, so it belongs here and not on the
            # main loop between two frames.
            held = worker_info()
        except Exception as exc:  # never let a worker kill the process
            entry = {"id": model_id, "kind": "unknown", "downloaded": False, "size_bytes": 0}
            free_bytes = total_bytes = cache_bytes = 0
            held = {"running": False, "model": ""}
            print(f"WayVoice: model state refresh failed: {exc}", file=sys.stderr)
        self.ctx.tasks.idle(self._model_state_ready, entry, free_bytes, total_bytes, held, cache_bytes)

    def _model_state_ready(self, entry, free_bytes, total_bytes, held, cache_bytes=0):
        self._model_refresh_busy = False
        self._apply_model_state(entry, free_bytes, total_bytes, held, cache_bytes)
        self._decide_what_to_do_about_the_selected_model(entry)
        if self._model_refresh_pending:
            self._refresh_model_state()
        return GLib.SOURCE_REMOVE

    def _ask_delete_model(self, *_args) -> None:
        """Confirm, then delete the selected model."""
        if not self.ctx.settings.model_delete_btn.get_sensitive() or self._model_deleting:
            return
        model_id = self._selected_model_id()
        if model_store.repo_dir_name(model_id) is None:
            self.ctx.window._toast(self.ctx.state.t("store.refuse_local"))
            return
        from ..dialogs.confirmations import delete_confirmation
        delete_confirmation(self.ctx.window, model_id, display_name(model_id), self.ctx.state.t, self._delete_model_confirmed)

    def _delete_model_confirmed(self, _button, win, model_id: str) -> None:
        win.close()
        if self._model_deleting:
            return
        self._model_deleting = True
        self.ctx.settings.model_delete_btn.set_sensitive(False)
        self.ctx.settings.model_delete_btn.set_label(self.ctx.state.t("store.deleting"))
        # rmtree of half a gigabyte plus a full rescan of the hub: seconds, not
        # milliseconds, so it must not run where the main loop draws.
        self.ctx.tasks.run(lambda: self._delete_model_worker(model_id), lambda _: None,
                           lambda exc: self._delete_model_ready({
                               "ok": False, "error_key": "store.delete_failed", "detail": str(exc)}))

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
        self.ctx.tasks.idle(self._delete_model_ready, result)

    def _delete_model_ready(self, result: dict) -> None:
        self._model_deleting = False
        self.ctx.settings.model_delete_btn.set_label(self.ctx.state.t("common.delete"))
        if not result.get("ok"):
            # A refusal carries a translation key rather than a sentence, so the reason
            # is localized here and never leaks an English-only string into the UI.
            reason = model_store.refusal_message(
                str(result.get("error_key") or "store.delete_failed"),
                str(result.get("detail") or ""),
                self.ctx.state.ui_lang,
            )
            self.ctx.window._toast(self.ctx.state.t("store.delete_refused", reason=reason), timeout=6)
            self._refresh_model_state()
            return GLib.SOURCE_REMOVE
        message_key = str(result.get("message_key") or "store.deleted")
        freed = int(result.get("freed_bytes") or 0)
        kept = int(result.get("kept_bytes") or 0)
        if message_key == "store.deleted" and kept > 0:
            # Some files had to stay: another model links to them. Saying only that the
            # model was deleted would hide files that are still on disk by design.
            message_key = "store.deleted_shared"
        self.ctx.window._toast(self.ctx.state.t(
            message_key,
            size=model_store.human_size(freed, self.ctx.state.ui_lang),
            freed=model_store.human_size(freed, self.ctx.state.ui_lang),
            kept=model_store.human_size(kept, self.ctx.state.ui_lang),
        ))
        self._refresh_model_state()
        return GLib.SOURCE_REMOVE
