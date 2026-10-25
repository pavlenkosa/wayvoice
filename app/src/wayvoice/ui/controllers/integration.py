"""IntegrationController owns its operations; pages own widgets."""

from ..health_presentation import show_operation_error

from ... import deps as deps_mod
from ... import pkgsys
from ... import service
from ...integration_health import collect_health
from ...cli import _run_setup_user
from ..widgets.labels import clip_subtitle

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk
import sys
import time


class IntegrationController:
    def __init__(self, context):
        self.ctx = context
        self._dep_rows = {}
        self._dep_installing = set()
        self._dep_last_refresh = 0.0
        self._refresh_running = False
        self._refresh_pending = False
        self._integration_running = False
        self._integration_pending = False
        self.health = None
        self._health_running = False
        self._health_pending = False
        self._health_checked = None
        self._health_key = None


    def attach_health(self, group):
        self.health_row = Adw.ActionRow(title=self.ctx.state.t('delivery.title'),
                                       subtitle=self.ctx.state.t('delivery.unknown'))
        retry = Gtk.Button(icon_name='view-refresh-symbolic', valign=Gtk.Align.CENTER,
                           tooltip_text=self.ctx.state.t('health.retry'))
        retry.connect('clicked', lambda *_: self.refresh_health(force=True))
        self.health_row.add_suffix(retry)
        group.add(self.health_row)

    @staticmethod
    def _config_key(cfg):
        return tuple(cfg.get(key) for key in ('paste_mode', 'shortcut'))

    def refresh_health(self, *, force=False):
        cfg = dict(self.ctx.state.cfg)
        key = self._config_key(cfg)
        if key != self._health_key and self.health is not None:
            self.health = None
            self._paint_delivery()
        if self._health_running:
            self._health_pending = self._health_pending or force or key != self._health_started_key
            return
        if not force and key == self._health_key and self._health_checked is not None and time.monotonic()-self._health_checked < 10:
            return
        self._health_running = True
        self._health_started_key = key
        def finished(result):
            self._health_running = False
            if key == self._config_key(self.ctx.state.cfg):
                self.health = result
                self._health_checked = time.monotonic()
                self._health_key = key
                self._paint_delivery()
            else:
                self._health_pending = True
            pending, self._health_pending = self._health_pending, False
            if pending:
                self.refresh_health(force=True)
        if not self.ctx.tasks.run(lambda: collect_health(cfg), finished,
                                  lambda _exc: finished({'delivery': 'unknown', 'reason': 'unknown'})):
            self._health_running = False

    def _paint_delivery(self):
        snapshot = self.health or {}
        reason = snapshot.get('reason', 'unknown')
        text = self.ctx.state.t('delivery.' + reason)
        if hasattr(self, 'health_row'):
            self.health_row.set_subtitle(text)
        if self.ctx.home is not None and hasattr(self.ctx.home, 'delivery_label'):
            self.ctx.home.delivery_label.set_text(text)

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
        if self._integration_running:
            self._integration_pending = True
            return
        self._integration_running = True
        if not self.ctx.tasks.run(self._apply_desktop_integration_worker,
                           self._desktop_integration_finished,
                           lambda exc: self._desktop_integration_finished(str(exc))):
            self._integration_running = False
            self._integration_pending = False

    def _apply_desktop_integration_worker(self) -> str:
        problems = []
        if not service.start_daemon():
            problems.append("WayVoice: the background daemon did not come up.")
        if service.systemd_available():
            ok, msg = _run_setup_user()
        else:
            ok, msg = service.apply_shortcut_now()
        if not ok:
            problems.append(f"WayVoice: desktop integration failed: {msg}")
        detail = "\n".join(problems)
        if detail:
            print(detail, file=sys.stderr)
        return detail

    def _desktop_integration_finished(self, detail):
        self.ctx.status._integration_error = str(detail or "")
        self._integration_running = False
        pending = self._integration_pending
        self._integration_pending = False
        try:
            if detail:
                show_operation_error(self.ctx, detail)
        finally:
            if pending:
                self._apply_desktop_integration()

    def _background_start(self):
        self._apply_desktop_integration()
        return GLib.SOURCE_REMOVE

    def _build_dependencies_group(self, page):
        """Build the dependencies group in the settings page."""
        group = Adw.PreferencesGroup(
            title=self.ctx.state.t("settings.dependencies"),
            description=self.ctx.state.t("settings.deps_sub"),
        )
        page.add(group)
        self.ctx.settings.dependencies = group
        for dep in deps_mod.dependencies():
            row = Adw.ActionRow(title=dep.label)
            button = Gtk.Button(label=self.ctx.state.t("settings.install"), valign=Gtk.Align.CENTER)
            button.connect("clicked", self._install_dependency, dep.id)
            row.add_suffix(button)
            spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
            spinner.set_visible(False)
            row.add_suffix(spinner)
            group.add(row)
            self._dep_rows[dep.id] = (row, button, spinner)

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
        if self._refresh_running:
            self._refresh_pending = True
            return
        self._refresh_running = True

        def probe():
            manager = pkgsys.detect_manager()
            can_install = bool(manager and (not pkgsys.requires_privilege() or pkgsys.pkexec_path()))
            rows = deps_mod.status_all()
            packages = {str(row["id"]): pkgsys.resolve_packages(str(row["id"]), manager) for row in rows}
            return manager, can_install, rows, packages

        self.ctx.tasks.run(probe, self._dependency_rows_ready, self._dependency_rows_failed)

    def _dependency_rows_failed(self, exc):
        self._refresh_running = False
        self._refresh_pending = False
        show_operation_error(self.ctx, exc)

    def _dependency_rows_ready(self, snapshot):
        self._refresh_running = False
        manager, can_install, rows, packages_by_id = snapshot
        for row_data in rows:
            dep_id = str(row_data["id"])
            entry = self._dep_rows.get(dep_id)
            if entry is None:
                continue
            row, button, spinner = entry
            missing = tuple(row_data.get("missing") or ())
            if not missing:
                row.set_subtitle(self.ctx.state.t("settings.deps_ok"))
                button.set_visible(False)
                spinner.stop()
                spinner.set_visible(False)
                continue
            purpose = self.ctx.state.t(str(row_data.get("purpose_key") or ""))
            subtitle = self.ctx.state.t("settings.deps_missing", binaries=", ".join(missing), purpose=purpose)
            if dep_id in self._dep_installing:
                spinner.start()
                spinner.set_visible(True)
                button.set_sensitive(False)
                subtitle = self.ctx.state.t("settings.installing")
            else:
                spinner.stop()
                spinner.set_visible(False)
                button.set_sensitive(True)
                packages = packages_by_id.get(dep_id)
                if manager is None:
                    # No package manager at all: nothing we could run.
                    button.set_visible(False)
                    subtitle = self.ctx.state.t("settings.deps_no_manager", programs=dep_id)
                elif not can_install:
                    # Manager found but no way to become root (no pkexec).
                    button.set_visible(False)
                    subtitle = self.ctx.state.t("settings.deps_manual")
                elif packages is None:
                    # The package name for this manager is not known with certainty; never
                    # invent one.
                    button.set_visible(False)
                    subtitle = self.ctx.state.t("settings.deps_no_name")
                else:
                    button.set_visible(True)
            row.set_subtitle(clip_subtitle(subtitle))

        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh_dependency_rows(force=True)

    def _install_dependency(self, _button, dep_id: str):
        """Explicit user action: install one dependency via the system manager."""
        if dep_id in self._dep_installing:
            return
        dep = deps_mod.get(dep_id)
        if dep is None:
            return
        manager = pkgsys.detect_manager()
        if manager is None:
            self.ctx.window._toast(self.ctx.state.t("settings.deps_no_manager", programs=dep_id))
            return
        packages = pkgsys.resolve_packages(dep, manager)
        if packages is None:
            self.ctx.window._toast(self.ctx.state.t("settings.deps_no_name"))
            return
        if pkgsys.requires_privilege() and pkgsys.pkexec_path() is None:
            self.ctx.window._toast(self.ctx.state.t("settings.deps_manual"))
            return

        self._dep_installing.add(dep_id)
        self._refresh_dependency_rows(force=True)
        # The package manager blocks and pkexec shows an authorization dialog, so the
        # work runs off the UI thread and comes back through GLib.idle_add.
        self.ctx.tasks.run(lambda: pkgsys.install_packages(packages, language=self.ctx.state.ui_lang),
                           lambda result: self._install_dependency_done(dep_id, *result),
                           lambda exc: self._install_dependency_done(dep_id, False, str(exc)))

    def _install_dependency_done(self, dep_id: str, ok: bool, message: str):
        self._dep_installing.discard(dep_id)
        self._refresh_dependency_rows(force=True)
        if ok:
            # Installing a package is not enough on its own: setup-user applies the global
            # shortcut and enables the ydotoold unit, and postinst runs it only once.
            # Re-run it so a dependency installed later really starts working.
            self._apply_desktop_integration()
            self.ctx.window.toast.add_toast(Adw.Toast(title=self.ctx.state.t("toast.deps_installed")))
            return GLib.SOURCE_REMOVE
        row = self._dep_rows.get(dep_id)
        if row is not None:
            row[0].set_subtitle(clip_subtitle(message))
        self.ctx.window.toast.add_toast(Adw.Toast(title=self.ctx.state.t("toast.deps_failed"), timeout=6))
        return GLib.SOURCE_REMOVE

    def _missing_required(self):
        """Blocking dependencies that are currently unusable."""
        return [
            dep for dep in deps_mod.dependencies()
            if dep.required and not deps_mod.status_of(dep)["ok"]
        ]
