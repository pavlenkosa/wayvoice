<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg" width="112" alt="WayVoice icon">
</p>

<h1 align="center">WayVoice</h1>

<p align="center">
  Local speech-to-text dictation for <b>GNOME + Wayland</b>.<br>
  Press a global shortcut, speak, and get text in the application you are already using.
</p>

<p align="center">
  <img alt="Version" src="https://img.shields.io/badge/version-0.5.0-3584e4">
  <img alt="Platform" src="https://img.shields.io/badge/platform-Debian%20%7C%20GNOME-4a86cf?logo=debian&logoColor=white">
  <img alt="Wayland" src="https://img.shields.io/badge/Wayland-native-ffbc00">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="GTK" src="https://img.shields.io/badge/GTK-4-4a86cf?logo=gtk&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="CI" src="https://img.shields.io/badge/CI-GitHub%20Actions-2088FF?logo=githubactions&logoColor=white">
</p>

## What it does

WayVoice runs as a small user service in the background. A global hotkey starts recording through PipeWire; pressing it again stops recording, transcribes the audio locally, fixes punctuation and pastes the resulting text into the focused application.

- **Local-first** — audio is processed on your machine by default.
- **Wayland-native workflow** — PipeWire recording, Wayland clipboard and optional `ydotool` paste.
- **Multiple engines** — Faster-Whisper and whisper.cpp.
- **Model catalog** — multilingual, English-only and community language-specific models.
- **GNOME UI** — GTK4 + libadwaita, dark/light theme, GNOME-style app and symbolic icons.
- **Global shortcut** — capture any convenient combination from the settings UI.
- **RU / EN interface** — system language detection with an explicit override.
- **Safety limits** — configurable maximum recording duration and recognition timeout.
- **Cancel transcription** — press the main button again if a model gets stuck or the input was just noise/humming.
- **Diagnostics** — copy environment, engine and service state in one click.

## Install on Debian 13

Download the latest `.deb`, then:

```bash
sudo apt install ./wayvoice_0.5.0_all.deb
```

Open **WayVoice** from the application grid or run:

```bash
wayvoice-settings
```

The Faster-Whisper runtime is isolated under `~/.local/share/wayvoice/runtime`. Model weights are downloaded on first use. `ydotool` is recommended for automatic paste; without it, recognized text remains available in the Wayland clipboard.

## CLI

```bash
wayvoice toggle       # start / stop recording, or cancel recognition
wayvoice start
wayvoice stop
wayvoice cancel
wayvoice status
wayvoice engine-status
wayvoice settings
```

Logs:

```bash
journalctl --user -u wayvoice -f
journalctl --user -u wayvoice-engine-setup -f
```

## Recognition engines

### Faster-Whisper

Default engine. WayVoice creates a dedicated Python runtime and supports built-in Whisper aliases plus compatible CTranslate2 repositories.

### whisper.cpp

Point WayVoice to `whisper-cli` and a local GGML/GGUF model. GPU use can be toggled in Settings.

## Privacy

WayVoice itself does not upload recorded audio. Network access is needed when Faster-Whisper dependencies or model files have to be downloaded. If you configure an external engine yourself, its privacy properties are outside WayVoice's control.

## Development

```bash
git clone <your-repository-url>
cd wayvoice
make test
make deb
```

Useful targets:

```bash
make lint
make test
make deb
make clean
```

The built Debian package appears in `dist/`.

## Repository layout

```text
app/                 Python application
  src/wayvoice/
data/                desktop file, AppStream metadata and icons
systemd/             user services
packaging/            Debian maintainer scripts
scripts/              launchers and build helpers
tests/                unit tests
.github/workflows/    CI and tagged-release automation
```

## Release flow

Every push to `main` and every pull request runs CI, executes the test suite, validates the desktop/AppStream metadata, builds a Debian package, and publishes the resulting `.deb` as a GitHub Actions artifact.

Official releases are tag-driven:

1. Update the project version in `app/src/wayvoice/__init__.py`, `app/pyproject.toml`, and the newest AppStream release entry.
2. Update `CHANGELOG.md`.
3. Verify locally with `make version-check && make test && make deb`.
4. Commit and push the changes.
5. Create and push a matching tag, for example:

```bash
git tag v0.5.0
git push origin v0.5.0
```

The Release workflow refuses to publish when the Git tag and project version differ. A successful tagged build creates a GitHub Release and attaches:

- `wayvoice_<version>_all.deb`
- the per-package SHA-256 file
- `wayvoice-<version>-source.zip`
- `SHA256SUMS`

GitHub-generated release notes are grouped by feature, fix, UI/UX, localization, maintenance, and other changes.

## Contributing

Bug reports and focused pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Security-related reports should follow [SECURITY.md](SECURITY.md).

## License

MIT © 2026 WayVoice contributors.
