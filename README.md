<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg" width="112" alt="WayVoice icon">
</p>

<h1 align="center">WayVoice</h1>

<p align="center">
  Local voice typing for GNOME and Wayland.<br>
  Press a hotkey, speak, and get text in the app you're already using.
</p>

<p align="center">
  <a href="https://github.com/stepan-pavlenko/wayvoice/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/stepan-pavlenko/wayvoice/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/stepan-pavlenko/wayvoice/releases"><img alt="Release" src="https://img.shields.io/github/v/release/stepan-pavlenko/wayvoice?display_name=tag&sort=semver"></a>
  <img alt="Wayland" src="https://img.shields.io/badge/Wayland-native-ffbc00">
  <img alt="GTK" src="https://img.shields.io/badge/GTK-4-4a86cf?logo=gtk&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/license-GPL--3.0-green">
</p>

## What is WayVoice?

WayVoice is a background voice-input utility for Linux. Start recording with a global hotkey, speak naturally, and WayVoice transcribes the audio locally and inserts the result into the focused application.

- Local speech recognition with **Faster-Whisper** or **whisper.cpp**
- Works with **GNOME + Wayland**
- Automatic punctuation and spoken punctuation commands
- Multilingual and language-specific models
- Configurable global hotkey
- Recognition timeout and cancellation
- GTK4 + libadwaita interface
- RU / EN interface
- Audio stays on your machine when using local engines

## Install

Download the latest `.deb` from **Releases**, then install it:

```bash
sudo apt install ./wayvoice_*_all.deb
```

Open **WayVoice** from the GNOME application grid or run:

```bash
wayvoice-settings
```

The Faster-Whisper runtime is installed into the user's WayVoice directory and model weights are downloaded on first use.

## Usage

1. Choose the recognition engine and model.
2. Set a global hotkey.
3. Put the cursor into any text field.
4. Press the hotkey and speak.
5. Press it again to stop recording.

WayVoice transcribes the recording and inserts the text into the active application. If automatic paste is unavailable, the result remains in the Wayland clipboard.

## Recognition engines

### Faster-Whisper

The default option. Supports multilingual Whisper models, English-only models and compatible CTranslate2 community models.

### whisper.cpp

Use a local `whisper-cli` binary together with a GGML/GGUF model.

## Useful commands

```bash
wayvoice toggle
wayvoice cancel
wayvoice status
wayvoice engine-status
wayvoice settings
```

Service log:

```bash
journalctl --user -u wayvoice -f
```

## Privacy

WayVoice does not send recordings to a cloud service when a local recognition engine is selected. Network access is used to download runtime dependencies and model files when required.

## License

WayVoice is licensed under the **GNU General Public License v3.0 only** (SPDX: `GPL-3.0-only`). See [LICENSE](LICENSE).
