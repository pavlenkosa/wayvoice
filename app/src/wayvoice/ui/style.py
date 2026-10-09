"""The settings window's stylesheet, extracted from the former monolith."""

CSS = r"""
.window-root { background: @window_bg_color; }
.content-wrap { padding: 18px 16px 24px 16px; }
.hero-card, .surface-card, .transcript-card, .health-card {
  background: alpha(@card_bg_color, 0.97);
  border: 1px solid alpha(@window_fg_color, 0.08);
  border-radius: 22px;
}
.hero-card { padding: 30px; }
.surface-card { padding: 18px; }
.transcript-card, .health-card { padding: 18px 20px; }
.hero-title { font-size: 1.5em; font-weight: 800; }
.hero-subtitle { font-size: 0.875em; color: @window_fg_color; }
.section-title { font-size: 1em; font-weight: 700; }
.muted { color: @window_fg_color; }
.metric-value { font-size: 0.9375em; font-weight: 700; }
.mic-button {
  min-width: 92px; min-height: 92px; border-radius: 999px; padding: 0;
  background: @accent_bg_color; color: @accent_fg_color; border: none;
}
.mic-button.recording { background: @error_bg_color; color: @error_fg_color; }
.mic-button.busy { background: @warning_bg_color; color: @warning_fg_color; }
.status-pill {
  padding: 6px 11px; border-radius: 999px;
  background: alpha(@window_fg_color, 0.07); color: @window_fg_color;
  font-size: 0.75em; font-weight: 700;
}
.status-pill.recording { background: alpha(@error_bg_color, 0.18); color: @error_color; }
.status-pill.busy { background: alpha(@warning_bg_color, 0.18); color: @warning_color; }
.status-pill.ready { background: alpha(@success_bg_color, 0.16); color: @success_color; }
.hotkey-pill { padding: 8px 12px; border-radius: 12px; background: alpha(@window_fg_color, 0.07); font-weight: 700; }
.kicker { font-size: 0.75em; font-weight: 800; letter-spacing: 0.08em; color: @accent_color; }
.warning-text { color: @warning_color; }
.error-text { color: @error_color; }
.capture-key { font-size: 1.5em; font-weight: 800; }
"""
