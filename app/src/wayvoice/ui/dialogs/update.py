"""Window-owned presentation of an independently owned package transaction."""
import json
import shutil
import subprocess

from gi.repository import Adw, Gtk

from ... import __version__
from ...updater import check, package_system


class UpdateFailure(RuntimeError):
    def __init__(self, message, installed=False, version=None):
        super().__init__(message)
        self.installed = installed
        self.version = version


class UpdateDialog:
    def __init__(self, window):
        self.window = window
        self.t = window.state.t
        self.busy = False
        self.alive = True
        self.restart_only = False
        self.can_install = False
        self.retry_check = False
        self.target_version = None
        self.dialog = Adw.MessageDialog(transient_for=window, modal=True,
            heading=self.t('update.title'), body=self.t('update.checking'))
        self.dialog.add_response('close', self.t('common.close'))
        self.dialog.set_close_response('close')
        self.dialog.connect('close-request', lambda *_: self.busy)
        self.dialog.connect('response', self._closed)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.spinner = Gtk.Spinner()
        self.spinner.start()
        content.append(self.spinner)
        self.install_button = Gtk.Button(label=self.t('update.install'))
        self.install_button.add_css_class('suggested-action')
        self.install_button.set_visible(False)
        self.install_button.connect('clicked', self._install)
        content.append(self.install_button)
        self.details = Gtk.Label(wrap=True, selectable=True, xalign=0)
        self.details.set_direction(Gtk.TextDirection.LTR)
        scroll = Gtk.ScrolledWindow(min_content_height=100, max_content_height=180,
                                   propagate_natural_height=True)
        scroll.set_child(self.details)
        self.details_expander = Gtk.Expander(label=self.t('health.details'))
        self.details_expander.set_child(scroll)
        self.details_expander.set_visible(False)
        content.append(self.details_expander)
        self.dialog.set_extra_child(content)
        self.dialog.present()
        window.tasks.run(check, self._checked, self._failed)

    def _closed(self, *_):
        if not self.busy:
            self.alive = False
            self.window._update_dialog = None

    def _checked(self, result):
        if not self.alive:
            return
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.window.context.updates.checked(result)
        if not result['available']:
            self.dialog.set_body(self.t('update.current', version=__version__))
            return
        self.target_version = result['version']
        self.dialog.set_body(self.t('update.available', installed=__version__, available=result['version']))
        # Installed package ownership is a subprocess probe, never a GTK callback.
        self.window.tasks.run(package_system, self._supported, self._unsupported)

    def _supported(self, _kind):
        if self.alive:
            self.can_install = True
            self.install_button.set_visible(True)

    def _unsupported(self, _exc):
        if self.alive:
            self.dialog.set_body(self.t('update.external'))

    def _install(self, *_):
        if self.busy:
            return
        if self.retry_check:
            self.retry_check = False
            self.install_button.set_visible(False)
            self.details_expander.set_visible(False)
            self.spinner.set_visible(True)
            self.spinner.start()
            self.dialog.set_body(self.t('update.checking'))
            self.window.tasks.run(check, self._checked, self._failed)
            return
        preferences = self.window.context.preferences
        if preferences._mutations:
            self.window._toast(self.t('update.busy'))
            return
        if preferences.has_unsaved_changes():
            preferences.confirm_leaving(lambda: self.window.tasks.idle(self._start), parent=self.dialog)
        else:
            self._start()

    def _start(self):
        if not self.alive or self.busy:
            return
        if self.window.context.preferences._mutations:
            self.window._toast(self.t('update.busy'))
            return
        self.busy = self.window._update_busy = True
        self.details_expander.set_visible(False)
        self.install_button.set_visible(False)
        self.spinner.set_visible(True)
        self.spinner.start()
        self.dialog.set_response_enabled('close', False)
        self.dialog.set_body(self.t('update.stage.check'))
        self.window.tasks.run(self._transaction, self._finished, self._failed)

    def _transaction(self):
        launcher = shutil.which('wayvoice')
        if not launcher:
            raise RuntimeError(self.t('update.launcher_missing'))
        # The child owns package-manager lifetime and locks. Closing a parent
        # process must not kill an already authorized package transaction.
        command = ([launcher, 'update', '--gui-restart', self.target_version] if self.restart_only
                   else [launcher, 'update', '--gui-install'])
        with subprocess.Popen(command, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', start_new_session=True) as proc:
            final = None
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                self.window.tasks.idle(self._progress, event)
                if event.get('stage') in {'done', 'error'}:
                    final = event
            code = proc.wait()
        if final is None:
            raise RuntimeError(self.t('update.interrupted'))
        if code or final['stage'] == 'error':
            message = str(final.get('message') or self.t('update.interrupted'))
            if final.get('installed'):
                message = self.t('update.restart_failed') + '\n' + message
            raise UpdateFailure(message, bool(final.get("installed")), final.get("version"))
        return final

    def _progress(self, event):
        if self.alive and event.get('version'):
            self.target_version = event['version']
        if self.alive and event.get('stage') in {'check', 'download', 'verify', 'authorize', 'install', 'restart'}:
            self.dialog.set_body(self.t('update.stage.' + event['stage']))

    def _finished(self, _event):
        if not self.alive:
            return
        self.restart_only = True
        self.target_version = _event.get('version') or self.target_version
        self.dialog.set_body(self.t('update.stage.reopen'))
        try:
            from ..application import restart_installed_ui
            restart_installed_ui(self.window)
        except OSError as exc:
            self._failed(exc)

    def _failed(self, exc):
        if not self.alive:
            return
        self.target_version = getattr(exc, 'version', None) or self.target_version
        self.restart_only = self.restart_only or bool(getattr(exc, 'installed', False))
        if not self.busy and not self.can_install:
            self.retry_check = True
        self.busy = self.window._update_busy = False
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.dialog.set_response_enabled('close', True)
        self.details.set_text(str(exc))
        self.details_expander.set_visible(True)
        self.dialog.set_body(self.t('update.restart_failed' if self.restart_only else 'update.failed'))
        self.install_button.set_label(self.t('update.retry'))
        self.install_button.set_visible(self.can_install or self.restart_only or self.retry_check)


def show_update(window):
    current = getattr(window, '_update_dialog', None)
    if current is not None:
        current.dialog.present()
        return
    window._update_dialog = UpdateDialog(window)
