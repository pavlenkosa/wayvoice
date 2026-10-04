# Changelog

## 0.6.0 — 2026-10-04

- Recognition languages: all 100 that Whisper supports, each under its own name, with automatic detection as the default instead of a pinned language.
- Spoken punctuation commands in English as well as Russian, applied per language.
- Engine registry: engines are declared once and the window, the daemon and the CLI all ask it, instead of three places comparing engine ids.
- External command engine is now reachable from the settings window.
- Model management: size on disk, free space, the whole shared cache, and deletion that keeps files another model still uses.
- Missing dependencies are detected, explained and can be installed from the settings.
- Daemon, engine and hot key now start without systemd, so the Flatpak build works.
- Faster-Whisper model is kept warm between dictations, and is loaded into memory when the daemon starts so the first dictation of a session is as fast as the ones after it.
- Model downloads are a visible, cancellable step with progress in bytes, instead of a silent wait inside the first recognition.
- Choosing a model asks before the download: a model that is already on disk is only loaded, one that is not is fetched after the user says so, and the question names the model and its size.
- Preparing a model that is already on disk loads it into the warm worker, so the first dictation after choosing it is as fast as the ones after that, and the settings window says so instead of showing nothing.
- A missing model can be fetched from the settings window itself: the model row grows a Download button, instead of the only way to fetch one being to pick a different model first.
- The hot key no longer starts a model download on its own. Pressing it is not agreeing to spend the bandwidth, so it now says which model is missing and where it can be fetched.
- The warm-up message no longer promises a time: a large model on a slow disk takes minutes to load, and the row says it is waiting instead of guessing.
- Installation paths are prefix-independent.
- New application icon, and the desktop entry declares one main category, so the app appears once in the menu.
- A model given as a local path is dictatable again: it was being reported as "missing", which made the daemon refuse every hot key press and offer a download that cannot succeed.
- Downloading a model no longer raises errors from inside `huggingface_hub` on the xet-served path, and the download progress is still exact.
- Stopping the daemon now stops the recording with it. Only a signal used to end the process outright, skipping the cleanup, so the recorder - which runs in a session of its own - stayed behind holding the microphone with nobody left to stop it. That is what systemd sends on stop, and what a forced stop sends to a daemon that ignored `quit`.
- The daemon keeps answering while it works: a slow notification or a slow command no longer make the settings window decide the daemon has died and the hot key do nothing.
- The warm-up reports what actually happened: it no longer gives up and calls a model "not loaded" while it is still being read, and a model being loaded counts as work, so the worker is no longer timed out and exits mid-load.
- Ownership is decided before anything is prepared, so a second daemon that is going to be refused no longer stops the running daemon's worker.
- Every message WayVoice writes for the user is translated, including download failures, cancellation and the refusals the command line prints. Messages from `huggingface_hub` stay in the library's own language.
- Two file handles per dictation are closed on the way out instead of whenever the collector gets round to it.
- Flatpak build asks for network access, without which the model download cannot work inside the sandbox at all.
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
