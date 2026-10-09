"""About dialog for WayVoice."""

from gi.repository import Adw, Gtk

from ... import __version__


def show_about(window):
    """Show the about dialog."""
    about = Adw.AboutWindow(transient_for=window, modal=True)
    about.set_application_name("WayVoice")
    about.set_application_icon("io.github.stepan.WayVoice")
    about.set_version(__version__)
    about.set_comments(window.t("about.comments"))
    about.set_developer_name(window.t("about.developer"))
    about.set_developers([window.t("about.developer")])
    about.set_license_type(Gtk.License.AGPL_3_0)
    about.set_copyright("© 2026 WayVoice contributors")
    about.set_website("https://github.com/pavlenkosa/wayvoice")
    about.set_issue_url("https://github.com/pavlenkosa/wayvoice/issues")
    about.present()