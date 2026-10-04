# Changelog

## 0.6.2 — 2026-10-05

- The Debian package now ships its own `ydotool`, built from vendored sources, so automatic pasting works on distributions that do not package it — Debian 13 has none, in any component. A system's own copy is always preferred, the bundled one is a fallback, and a package built without a compiler simply has none.
- Licence changed from GPL-3.0-only to **AGPL-3.0-or-later**, which is what allows the automatic-paste helper to be shipped with the application: `ydotool` is AGPL-3.0-or-later, and its source cannot be combined with GPL-3.0-only code. Updated accordingly: LICENSE, packaging metadata, AppStream data, the About dialog, and a machine-readable `copyright` file in the package (Debian policy 12.5).
- Missing dependencies are detected before anything is installed, and a package the system's repositories do not carry is reported as such: the settings no longer offer a button that ends in “Unable to locate package” after an authorization dialog and a password prompt. Debian 13 has no `ydotool` package in any component, and the query that finds this out is made in the C locale, because apt translates its output.
- Publishing is one step now: a push to `main` that changes the version publishes the release, and so does a push of the tag `v*` for whoever prefers tags. A version that is in the tree but was never released — the forgotten tag — is published by the next push, and a push that changes nothing stays quiet. The workflow no longer creates the tag itself — it used to push with `GITHUB_TOKEN`, and GitHub never starts workflows from such a push, so the tag appeared on the remote with nothing having run and a later `git push` of the same tag answered “Everything up-to-date”. A push to `main` that does not change the version does nothing at all.
- Automatic pasting works again after installing the package into a session that was already open. The `ydotool` helper unit is *enabled* at installation time, but a session that was up when the package arrived does not start a newly enabled unit until the next login — so dictation was recognized and then not pasted, with nothing in any log. The daemon now raises the helper itself when nothing answers on its socket, at most once a minute, and asks nobody to run `systemctl`.
- A failed paste now says why. `ydotool` reports its errors on **stdout**, which was sent to `/dev/null`, so the one line naming the cause — `failed to connect socket …: No such file or directory` — was discarded before anyone could read it and the user was left with “ydotool exited with an error”. The reason is kept, and every failure is written to the service log, so “it does not paste” is a line in `journalctl --user -u wayvoice` rather than something to be described from memory.
- Notifications no longer pile up: one dictation passes through three states (recording, transcribing, done) and each of them used to arrive as its own popup, so the notification center filled with three lines per dictation. Each dictation now owns one entry and the states update it in place, while the recognized text stays in the history until the next dictation starts — overwriting it would have been the other half of the same mistake.
- The helper is asked whether it is *listening*, not whether its socket file exists. `ydotoold` is killed without cleaning up, so a socket left behind by a dead helper looks exactly like a working one — the check that says “nothing answers” is now a `connect()` to the socket, which the kernel refuses for a socket nobody serves.

## 0.6.1 — 2026-10-04

- Never released. The number was prepared, then replaced by 0.6.2 before it reached a release, so there is no 0.6.1 on GitHub.

## 0.6.0 — 2026-10-04

- Recognition languages: all 100 that Whisper supports, each under its own name, with automatic detection as the default instead of a pinned language.
- Spoken punctuation commands in English as well as Russian, applied per language.
- Engine registry: engines are declared once and the window, the daemon and the CLI all ask it, instead of three places comparing engine ids.
- External command engine is now reachable from the settings window.
- Model management: size on disk, free space, the whole shared cache, and deletion that keeps files another model still uses.
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
