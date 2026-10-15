# Contributing

Focused fixes, translations, documentation and desktop acceptance reports are welcome. Open an issue before a large feature or architecture change so its user outcome and scope can be agreed.

## Workflow

Branch from `develop` into `feature/*` or `fix/*`, and normally target `develop` with a focused pull request. Release integration into `main` uses a separate reviewed PR. Never include internal plans, audits, local checkpoints, recordings, personal dictation or credentials. Public documentation is written in English under the allowlisted `docs/` pages.

Describe the problem, resulting behavior and validation. Distinguish automatic tests from manual acceptance and unavailable checks. Preserve unrelated user changes. Do not weaken an invariant merely to make a test green.

## Development and checks

Python 3.11+ is required. GTK UI checks need GTK4/libadwaita and PyGObject; a headless environment may skip them. See [development](docs/development.md) for setup and package commands and [architecture](docs/architecture.md) for ownership.

```bash
make lint
make test
python3 scripts/check-public-docs.py
python3 scripts/check-flatpak-manifest.py
```

CI also runs Ruff's initial correctness rules and critical-module branch coverage. Install the pinned `requirements-dev.txt` into a separate development environment if desired. No local tool installation is performed automatically. Tests must not download models, record a microphone, mutate real user configuration or restart working services.

Add regression or characterization tests for changed behavior. For concurrency, cover ownership, deadline, addressed cancellation and cleanup on failure/shutdown. Slow operations run outside GTK callbacks; publish GTK updates on the GLib main loop. Use the decomposed UI package rather than growing a single window class.

Add user-facing strings to RU/EN maps and all eight JSON catalogues under `app/src/wayvoice/locales/`. Preserve formatting parameters; translation tests check coverage.

## Manual acceptance

For relevant UI/input/package changes, record GNOME/Wayland or the actual target desktop, commit/package, engine/model/device, scenario, expected/actual outcome and diagnostic reference. Cover recording → recognition → clipboard/insertion, silence, cancellation and the next dictation after errors. Test editor, browser and terminal paste modes. Verify microphone loss does not silently select another input. Installed updates and global shortcuts need separate checks. Do not automatically store audio or dictated text in reports. Mark unavailable checks NOT TESTED.

No telemetry or audio uploads without an explicit opt-in design and privacy review. Package-manager commands do not use a shell; custom external recognition commands are the documented exception. Keep local recognition offline and model preparation explicit.

## Releases and security

A version bump never publishes by itself. See the [release process](docs/releasing.md). Do not push release tags, rewrite shared history or replace published assets casually. Report vulnerabilities through [SECURITY.md](SECURITY.md), and use [SUPPORT.md](SUPPORT.md) for ordinary problems.
