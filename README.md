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

- Local speech recognition with **Faster-Whisper**, **whisper.cpp** or an external command
- Works with **GNOME + Wayland**
- Any of the **100 languages Whisper supports**, detected automatically or pinned
- Automatic punctuation and spoken punctuation commands
- Multilingual and language-specific models
- The model stays warm between dictations, so only the first one pays for loading it
- Model files on disk: size, free space and deletion
- Missing dependencies are detected and can be installed from the settings
- Runs from **Debian/Ubuntu packages** and as a **Flatpak**, with or without systemd
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

The model is kept in memory between dictations, so the second recording starts without loading it again. Turn the warm worker off in the settings to free that memory between recordings.

### whisper.cpp

Use a local `whisper-cli` binary together with a GGML/GGUF model.

### External command

Hand the recording to any command that prints the recognized text to stdout. The template runs through `/bin/sh`, and `{audio}` is replaced with the path of the recording:

```
vosk-transcriber -i {audio}
```

## Languages

The recognition language defaults to automatic detection, which is what the engines do best: a pinned language does not fail loudly, it silently applies the wrong grammar to foreign speech.

Every language Whisper knows is in the settings, under the name speakers of that language use for it. Spoken punctuation commands ("comma", "question mark", "точка", "запятая") are recognised in the language being spoken.

## Models on disk

Model weights live in the Hugging Face cache, which is shared with other programs. The settings show what the selected model occupies, what WayVoice's models occupy in total, and how much space the whole cache takes — plus how much is free on disk. A downloaded model can be deleted from the same row.

Deleting a model never removes files another model is using: the weights are shared between models, and only what nothing points at any more is freed. A model provided as a local path is yours and is never deleted.

## Missing dependencies

WayVoice needs `pw-record`, `wl-copy` and `notify-send` at runtime, and `ydotool` for automatic pasting. The settings list what is missing and can install it through the system package manager — nothing is installed unless you ask.

Without `ydotool`, dictation still works: the recognized text goes to the Wayland clipboard and you paste it yourself.

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
