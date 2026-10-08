"""HomePage constructs and owns its widgets."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk

from ...engine import engine_label
from ...models import display_name
from ...shortcut import label_for
from ..widgets.labels import make_label


class HomePage:
    def __init__(self, context):
        self.ctx = context
        context.home = self
        self.root = self._build_home()

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
        text.append(make_label("WAYVOICE", "kicker"))
        text.append(make_label(self.ctx.state.t("home.title"), "hero-title"))
        text.append(make_label(self.ctx.state.t("home.subtitle"), "hero-subtitle"))
        intro.append(text)
        self.status_pill = make_label(self.ctx.state.t("status.starting"), "status-pill")
        self.status_pill.set_valign(Gtk.Align.CENTER)
        intro.append(self.status_pill)
        outer.append(intro)

        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
        hero.add_css_class("hero-card")
        self.mic_button = Gtk.Button()
        self.mic_button.add_css_class("mic-button")
        self.mic_button.set_halign(Gtk.Align.CENTER)
        self.mic_button.set_sensitive(False)
        self.mic_button.connect("clicked", self.ctx.status._toggle)
        self.mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
        self.mic_icon.set_pixel_size(40)
        self.mic_button.set_child(self.mic_icon)
        hero.append(self.mic_button)
        self.hero_state = make_label(self.ctx.state.t("hero.starting"), "hero-title", xalign=0.5)
        self.hero_state.set_halign(Gtk.Align.CENTER)
        hero.append(self.hero_state)
        self.hero_caption = make_label("", "hero-subtitle", wrap=True, xalign=0.5)
        self.hero_caption.set_justify(Gtk.Justification.CENTER)
        self.hero_caption.set_halign(Gtk.Align.CENTER)
        self.hero_caption.set_max_width_chars(58)
        hero.append(self.hero_caption)
        shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        shortcut_box.set_halign(Gtk.Align.CENTER)
        shortcut_box.append(make_label(self.ctx.state.t("shortcut.global"), "muted"))
        self.hotkey_label = make_label(label_for(self.ctx.state.shortcut_binding), "hotkey-pill")
        shortcut_box.append(self.hotkey_label)
        hero.append(shortcut_box)
        outer.append(hero)

        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.set_column_homogeneous(True)
        self.engine_card = self._metric_card(grid, 0, self.ctx.state.t("card.engine"), engine_label(self.ctx.state.cfg.get("engine")) or "—", "applications-engineering-symbolic")
        self.model_card = self._metric_card(grid, 1, self.ctx.state.t("card.model"), display_name(str(self.ctx.state.cfg.get("model", "small"))), "applications-system-symbolic")
        self.paste_card = self._metric_card(grid, 2, self.ctx.state.t("card.paste"), "Ctrl+V", "edit-paste-symbolic")
        outer.append(grid)

        health = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        health.add_css_class("health-card")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(make_label(self.ctx.state.t("health.title"), "section-title"))
        self.health_summary = make_label(self.ctx.state.t("health.checking"), "muted")
        self.health_summary.set_hexpand(True)
        self.health_summary.set_halign(Gtk.Align.END)
        row.append(self.health_summary)
        health.append(row)
        self.health_detail = make_label("", "muted", wrap=True)
        health.append(self.health_detail)
        outer.append(health)

        transcript = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        transcript.add_css_class("transcript-card")
        h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        h.append(make_label(self.ctx.state.t("transcript.title"), "section-title"))
        self.transcript_meta = make_label("", "muted")
        self.transcript_meta.set_hexpand(True)
        self.transcript_meta.set_halign(Gtk.Align.END)
        h.append(self.transcript_meta)
        transcript.append(h)
        self.last_text = make_label(self.ctx.state.t("transcript.empty"), "muted", wrap=True)
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
        top.append(make_label(title, "muted"))
        card.append(top)
        label = make_label(value, "metric-value")
        card.append(label)
        grid.attach(card, column, 0, 1, 1)
        return label
