# Shortcuts and paste

[WayVoice](../README.md)

### Shortcuts and paste on Wayland

| Desktop | Shortcut setup |
| --- | --- |
| GNOME | Configure the shortcut in WayVoice Settings. |
| KDE Plasma | Click **Configure in KDE** to assign the action through the GlobalShortcuts portal. If unavailable, bind `wayvoice toggle` in Plasma's shortcut settings. |
| Other Wayland desktops | Bind the command shown in WayVoice Settings using the desktop or compositor's shortcut configuration. |

For Flatpak, use the command shown by the app rather than the native command above. Remove an old manual binding before assigning the same key through the KDE portal.

Automatic paste requires `ydotool` and access to `/dev/uinput`. Native packages include a fallback helper and prefer a system-installed copy when available. If the helper cannot be used, recognized text still reaches the clipboard. Choose **Ctrl+Shift+V** for terminals such as Konsole; ordinary editors generally use **Ctrl+V**.



## Push-to-talk

Configure the global shortcut in Settings, then choose **Hold to talk (PTT)**. This mode uses
press and release events from the desktop shortcut portal, currently integrated
with KDE Plasma. Hold the key while speaking and release it to transcribe.
The microphone button continues to start and stop recording on each click.
If the shortcut session is lost, WayVoice cancels its held recording.

The GNOME shortcut backend does not provide release events, so its shortcut
continues to work as a toggle. On compositors that support separate press and
release bindings, assign these commands to the same key:

```bash
wayvoice ptt-start  # key press
wayvoice ptt-stop   # key release
```

The compositor must deliver both commands in order, including release when the
focus changes. WayVoice's recording time limit remains a safety stop. These
bindings do not register themselves; configure them in your desktop or compositor.
The commands acknowledge the gesture immediately; recording failures appear in the app’s status.
Use **Open indicator** on Home to show the compact recording status window.
