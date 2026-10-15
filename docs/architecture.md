# Architecture

[WayVoice](../README.md)

The GTK client sends commands to a local daemon. The daemon owns recording, cancellation and dictation lifecycle; recognition runs outside GTK callbacks. Successful text passes through punctuation and optional replacements into clipboard delivery and optional paste.

| Owner | Location |
| --- | --- |
| GTK pages, dialogs, controllers and async UI state | `app/src/wayvoice/ui/` |
| Recording and dictation state machine | `daemon.py`, `audio.py` |
| Engine registry and pipeline coordination | `engine.py` |
| Process lifetime, worker transport and model preparation | `runtime/` |
| Faster-Whisper worker and one-shot runner | `fw_worker.py`, `fw_runner.py` |
| Model catalogue, download and cache semantics | `models.py`, `model_fetch.py`, `model_store.py` |
| Transactional settings | `config.py` |
| Clipboard and optional virtual-keyboard paste | `injector.py` |
| Native/direct service and desktop integration | `service.py`, `shortcut.py`, `pkgsys.py` |

Local engines do not require network during dictation. Model/runtime downloads are explicit operations. Custom external recognition commands execute through `/bin/sh` and define their own trust and network behavior.

GTK work belongs on the GLib main loop. Background work must publish results safely and preserve unsaved drafts. Configuration uses interprocess locking, conflict detection and atomic writes. Recording/recognition must unwind busy state, cancellation, timers, temporary WAVs and owned subprocesses on every exit path. Process cleanup must never target an unrelated process merely because a PID was reused.

Prefer focused changes with behavior tests. Split large modules only when ownership and maintenance improve; file size alone does not justify rewriting recording, transport or localization.
