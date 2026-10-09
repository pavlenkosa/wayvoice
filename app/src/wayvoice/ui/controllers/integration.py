"""IntegrationController owns its operations; pages own widgets."""

from ... import deps as deps_mod
from ... import pkgsys
from ... import service
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
        self._integration_running = False
        pending = self._integration_pending
        self._integration_pending = False
        try:
            if detail:
                self.ctx.window._toast(detail)
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
        self.ctx.window._toast(str(exc))

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
        self.ctx.tasks.run(lambda: self._install_dependency_worker(dep_id, packages),
                           lambda _: None,
                           lambda exc: self._install_dependency_done(dep_id, False, str(exc)))

    def _install_dependency_worker(self, dep_id: str, packages: list[str]):
        try:
            ok, message = pkgsys.install_packages(packages, language=self.ctx.state.ui_lang)
        except Exception as exc:  # never let a worker kill the process
            ok, message = False, str(exc)
        self.ctx.tasks.idle(self._install_dependency_done, dep_id, ok, message)

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
