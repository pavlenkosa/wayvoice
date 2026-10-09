<p align="center">
  <img src="data/icons/hicolor/scalable/apps/io.github.stepan.WayVoice.svg" width="112" alt="WayVoice — offline voice typing for Linux">
</p>

<h1 align="center">WayVoice</h1>

<p align="center">
  <strong>Offline voice typing and speech-to-text for Linux, GNOME and Wayland.</strong><br>
  Press a hotkey, speak naturally, and WayVoice turns your speech into text in the app you're already using.
</p>

<p align="center">
  <a href="https://github.com/stepan-pavlenko/wayvoice/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/stepan-pavlenko/wayvoice/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/stepan-pavlenko/wayvoice/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/stepan-pavlenko/wayvoice?display_name=tag&sort=semver"></a>
  <img alt="Linux" src="https://img.shields.io/badge/Linux-Wayland-ffbc00?logo=linux&logoColor=black">
  <img alt="GTK 4" src="https://img.shields.io/badge/GTK-4-4a86cf?logo=gtk&logoColor=white">
  <img alt="License" src="https://img.shields.io/github/license/stepan-pavlenko/wayvoice">
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#recognition-engines">Recognition engines</a> ·
  <a href="#command-line">CLI</a> ·
  <a href="#privacy">Privacy</a>
</p>

## Voice typing for Linux that stays out of the way

WayVoice is an open-source **Linux dictation app** for system-wide voice input. Instead of opening a separate transcription window, you put the cursor where you want the text, press a global hotkey, speak, and continue working.

Speech recognition can run locally with **Faster-Whisper** or **whisper.cpp**, so your recordings do not need to leave your computer. WayVoice is designed around **GNOME, GTK4/libadwaita and Wayland**, while keeping a simple clipboard fallback when automatic paste is not available.

If you are looking for a Linux alternative to built-in voice typing on other desktop operating systems — especially one based on local Whisper speech recognition rather than a cloud service — that is the problem WayVoice is trying to solve.

## Why WayVoice?

- **Voice typing across applications** — browsers, editors, chats, IDEs and other places where you can paste text.
- **Local speech-to-text** — use Faster-Whisper or whisper.cpp without sending recordings to a transcription API.
- **Built for Wayland** — records through PipeWire tools, writes to the Wayland clipboard and can paste automatically with `ydotool`.
- **100 Whisper languages** — use automatic language detection or pin a language manually.
- **Spoken punctuation** — commands such as “comma”, “question mark”, “точка” and “запятая”.
- **Fast repeat dictation** — Faster-Whisper can keep the selected model warm in memory between recordings.
- **Model management in the UI** — download models, see disk usage and free space, cancel downloads and remove models you no longer need.
- **GNOME-style interface** — GTK4 + libadwaita, with Russian and English UI.
- **No forced downloads from the hotkey** — if a model is missing, WayVoice tells you instead of silently downloading gigabytes.
- **Useful without automatic paste** — recognized text stays in the clipboard if simulated `Ctrl+V` is unavailable.

## Install

### Debian / Ubuntu

Download the latest `.deb` package from [GitHub Releases](https://github.com/stepan-pavlenko/wayvoice/releases), then install it with APT:

```bash
sudo apt install ./wayvoice_*_amd64.deb
```

Launch **WayVoice** from the application grid, or run:

```bash
wayvoice-settings
```

The recognition runtime and model weights are downloaded only when they are needed and you choose to download them.

### Flatpak and other distributions

The repository includes a Flatpak manifest at [`io.github.stepan.WayVoice.json`](io.github.stepan.WayVoice.json). WayVoice can run without systemd, which keeps the application usable in sandboxed environments as well.

Prebuilt release assets are currently published on the [Releases](https://github.com/stepan-pavlenko/wayvoice/releases) page.

## How it works

1. Open WayVoice and choose a speech-recognition engine and model.
2. Set the global dictation hotkey.
3. Put the cursor into any text field.
4. Press the hotkey and speak.
5. Press it again to stop recording.

WayVoice transcribes the recording and places the result into the active workflow. When automatic paste is available, the text is inserted for you. Otherwise, it remains in the Wayland clipboard so you can paste it normally.

A model that is not yet installed is never downloaded just because you pressed the hotkey. Download it from Settings or from the command line:

```bash
wayvoice model --download
```

## Features at a glance

| Area | What WayVoice provides |
| --- | --- |
| Dictation | Global hotkey, start/stop recording, cancellation and recognition timeout |
| Speech recognition | Faster-Whisper, whisper.cpp or any external command |
| Languages | All 100 languages supported by Whisper, with automatic detection |
| Punctuation | Automatic punctuation plus spoken punctuation commands |
| Wayland | PipeWire recording, Wayland clipboard and optional automatic paste |
| Models | Download progress, cancellation, disk usage, free space and deletion |
| Performance | Optional warm Faster-Whisper worker between dictations |
| Desktop | GTK4, libadwaita, GNOME-style UI, RU/EN localization |
| Privacy | Local engines keep recognition and recordings on your machine |

## Recognition engines

### Faster-Whisper

The recommended default. It supports multilingual Whisper models, English-only models and compatible CTranslate2 models.

For faster repeated dictation, WayVoice can keep the model loaded between recordings. This uses more memory but avoids reloading the model every time. You can disable the warm worker in Settings if you prefer lower idle memory usage.

On one test machine, the resident worker used roughly **0.6 GB** with the `small` model and **1.6 GB** with `medium`. Larger models require more memory.

### whisper.cpp

Use a local `whisper-cli` binary with a compatible GGML/GGUF model. This is useful if you already have a whisper.cpp setup or prefer its runtime.

### External command

WayVoice can hand the recorded audio to another program and use whatever that command prints to stdout as the recognized text.

Example:

```text
vosk-transcriber -i {audio}
```

The command template runs through `/bin/sh`, and `{audio}` is replaced with the path to the recorded audio file.

## Languages and spoken punctuation

Automatic language detection is the default. You can also pin one of the languages supported by Whisper when you know exactly what you will be speaking.

Spoken punctuation is applied in the active recognition language, including English and Russian commands such as:

- “comma”, “period”, “question mark”
- “запятая”, “точка”, “вопросительный знак”

This makes WayVoice useful for longer Linux voice dictation, not only short search queries or commands.

## Models and storage

Faster-Whisper model files live in the Hugging Face cache, which may be shared with other applications.

WayVoice shows:

- whether the selected model is available locally;
- the model's size on disk;
- total space used by WayVoice models;
- total shared cache size;
- free disk space;
- download progress and cancellation.

Deleting a model from WayVoice does not blindly erase shared files that another cached model still uses.

You can select a model from the built-in catalogue, enter a full Hugging Face repository id such as `Systran/faster-whisper-large-v3`, or point WayVoice at a local model directory.

## Wayland clipboard and automatic paste

Wayland intentionally prevents normal applications from pretending to be your keyboard. Because of that, WayVoice separates **recognition** from **automatic paste**.

Core runtime tools include:

- `pw-record` for microphone recording;
- `wl-copy` for the Wayland clipboard;
- `notify-send` for desktop notifications;
- `ydotool` for optional automatic `Ctrl+V`.

If `ydotool` is unavailable, dictation still works — the recognized text is copied to the clipboard.

The Debian package also includes a bundled `ydotool` fallback for systems that do not provide a suitable package. A system-installed copy is preferred when available.

## Privacy

With a local recognition engine selected, WayVoice does **not** send your recordings to a cloud transcription service.

Network access is used to download the speech-recognition runtime and model files when you explicitly request them. Recognition itself runs locally.

That makes WayVoice suitable for people who want **private offline speech-to-text on Linux** without routing everyday dictation through a third-party API.

## Command line

The GUI covers normal daily use, but WayVoice also provides a small CLI.

### Dictation and status

```bash
wayvoice toggle
wayvoice cancel
wayvoice status
wayvoice engine-status
wayvoice settings
```

### Model management

```bash
wayvoice model
wayvoice model --download
wayvoice model --cancel
```

### Service log

```bash
journalctl --user -u wayvoice -f
```

## Troubleshooting

**The hotkey works, but text is not pasted automatically**

Check the clipboard first. If the recognized text is there, speech recognition succeeded and only automatic paste is unavailable. Check whether `ydotool` is installed and usable on your system.

**WayVoice says the model is missing**

Open Settings and download the selected model, or run:

```bash
wayvoice model --download
```

**Recognition takes a long time on the first use**

The model may still need to be downloaded or loaded into memory. Keeping the Faster-Whisper worker warm makes later dictations faster.

**Need more detail?**

See the [changelog](CHANGELOG.md), open an [issue](https://github.com/stepan-pavlenko/wayvoice/issues), or inspect the service log shown above.

## Development and contributing

Contributions, bug reports and testing on different Linux/Wayland setups are welcome.

- [Contributing guide](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)
- [Releases](https://github.com/stepan-pavlenko/wayvoice/releases)

## License

WayVoice is licensed under the **GNU Affero General Public License v3.0 or later** (`AGPL-3.0-or-later`).

See [LICENSE](LICENSE) and [third_party/ydotool](third_party/ydotool/README.wayvoice.md) for details.


Faster-Whisper selects its compute type automatically: `int8` on CPU and `float16`
on CUDA, in both worker and one-shot modes. Legacy `compute_type_cpu` and
`compute_type_cuda` configuration keys never controlled recognition; they are now
ignored when reading old files and omitted on the next explicit settings save.
Reading a configuration does not rewrite it.
