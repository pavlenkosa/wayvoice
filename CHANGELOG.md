# Changelog

## Unreleased

- Recognition languages: all 100 that Whisper supports, each under its own name, with automatic detection as the default instead of a pinned language.
- Spoken punctuation commands in English as well as Russian, applied per language.
- Engine registry: engines are declared once and the window, the daemon and the CLI all ask it, instead of three places comparing engine ids.
- External command engine is now reachable from the settings window.
- Model management: size on disk, free space, the whole shared cache, and deletion that keeps files another model still uses.
- Missing dependencies are detected, explained and can be installed from the settings.
- Daemon, engine and hot key now start without systemd, so the Flatpak build works.
- Faster-Whisper model is kept warm between dictations.
- Installation paths are prefix-independent.
- New application icon.
- Robustness: the daemon survives a silent or oversized client, and a second daemon can no longer take its socket over; a failed notification no longer aborts a recording or throws away a finished transcript; the clipboard copy and the recorder have deadlines and clean up after themselves; a cancelled dictation is no longer typed anyway; a config that cannot be parsed is reported instead of silently replaced by the defaults; a hung package install no longer leaves `dpkg` locked; `setup-user` reports a hotkey it could not apply.

## 0.5.1 — 2026-10-02

- License metadata is now consistently GPL-3.0-only: About dialog, packaging metadata and AppStream data.
- Release workflow runs end-to-end on version tags.

## 0.5.0 — 2026-10-02

- RU/EN UI localization with system-language auto selection.
- Added About window and one-click diagnostics.
- Added configurable recognition timeout and recording duration limit.
- Recognition can now be cancelled while a model is running.
- Main UI shows elapsed recording / recognition time.
- Faster-Whisper gets stricter VAD/no-speech handling.
- Added GNOME-style full-color and symbolic icons.
- Added AppStream metadata.
- Added GitHub Actions CI, tagged release workflow, Dependabot, tests and project templates.

## 0.4.0

- Added multilingual and language-specific Faster-Whisper model catalog.
- Fixed Wayland clipboard ownership behavior.
