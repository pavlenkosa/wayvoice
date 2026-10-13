# Troubleshooting and privacy

[WayVoice](../README.md)

**Nothing happens when I press the shortcut.** Check the shortcut configuration and preparation rows on Home. On desktops that require a manual binding, use the command shown in Settings.

**Recognition works, but text is not inserted.** Try pasting manually. If the text is in the clipboard, check the paste mode and `ydotool` permissions. Terminals often require **Ctrl+Shift+V**.

**The model is missing.** Download it in Settings, or run `wayvoice model --download` after saving the selected model.

**The first transcription is slow.** A downloaded model still needs to load into memory. The warm recognizer avoids repeating that work, but uses more RAM.

For other problems, open **Diagnostics** from the menu and review the report before sharing it. On systemd installations, inspect the service log with:

```bash
journalctl --user -u wayvoice.service -f
```

## Privacy

Faster-Whisper and whisper.cpp process recordings locally. Network access is used for downloads you request, plus update checks and package downloads from GitHub. Optional automatic checks contact GitHub while the interface is open; they do not send recordings. A custom recognition command may have its own network behavior.

Temporary recordings are removed after processing or cancellation. Dictation text is hidden in desktop notifications by default. Clipboard managers and other applications may retain clipboard text according to your desktop settings.

For support channels and a safe report checklist, see [SUPPORT.md](../SUPPORT.md).
