# Getting started

[WayVoice](../README.md)

Open **WayVoice** from the application menu, or run:

```bash
wayvoice-settings
```

1. Choose the interface language.
2. Choose a model: **Fast**, **Balanced** or **More accurate**. Larger models need more memory and take longer to process speech.
3. Select **Download and prepare** to download the required components and model, or **Set up later** to postpone it.
4. Configure the shortcut and check the preparation guidance on Home.

Existing configurations open directly. Selecting a preset only changes the settings draft. Click **Save** to apply it; model downloads are separate actions. Pressing the dictation shortcut never downloads missing model weights.

## Daily use

1. Place the cursor where you want the text.
2. Press the shortcut to start recording.
3. Speak, then press the shortcut again to stop.
4. Wait for recognition. If the text is not pasted automatically, paste it from the clipboard.

The Home recording button copies the result to the clipboard so you can switch to the destination application and paste it yourself.

Choose **Open indicator** from the menu, or run:

```bash
wayvoice-settings --indicator
```

Drag the indicator by its status row. Its window menu exposes desktop actions; in KDE, use **Keep Above** to keep it over other windows. Closing Settings or the indicator does not stop the background daemon.

**Ctrl+Shift+C** copies the last dictation and **Ctrl+,** opens Settings.



### Microphone and recording sounds

In Settings → Audio, refresh the microphone list and choose an input, then save.
System default follows the input selected in your desktop's sound settings. A
missing explicitly selected microphone produces an error rather than switching
to another input.

**Check microphone** measures a short sample from the selected input, including
an unsaved selection. The bar shows its peak level; the sample is discarded and
never sent for recognition. Finish any dictation before running the check.
Optional recording sounds mark the start, stop and cancellation of a recording.
They are off by default.


### Personal dictionary

Enable text replacements in Settings and add one pair per line:

```text
way voice => WayVoice
step an => Stepan
```

Save to apply them to dictation. Preview lets you try the current draft without
recording or saving. Matching ignores case and uses whole phrases; longer matches
win. Replacements run once after punctuation processing and preserve the spelling
you enter. Links, email addresses, paths and technical tokens are left unchanged.

The dictionary stays on your computer and works with every recognition engine.
It corrects the resulting text; it does not train or prompt the speech model.
There is a limit of 100 rules and 16,384 characters. Disable replacements to keep
the rules without applying them.



## Command line

```bash
wayvoice toggle          # Start or stop recording
wayvoice start           # Start recording
wayvoice stop            # Stop and transcribe
wayvoice cancel          # Cancel the current dictation
wayvoice status          # Show daemon status
wayvoice engine-status   # Check the recognition runtime
wayvoice settings        # Open Settings
```

Model commands operate on the model saved in Settings:

```bash
wayvoice model             # Show model status
wayvoice model --download  # Prepare the saved model
wayvoice model --cancel    # Cancel its download
```

## Quick recognition selection

While the daemon is idle, Home offers prepared local engine/model choices and a recognition language. Select the combination and click **Save**. The list does not download models; use **Configure** to prepare a missing model in Settings. Custom paths, device and decoding options remain in Settings.

Model preparation, recording, recognition and application updates prevent switching. Saving starts an asynchronous offline warm-up when enabled; Preparing, Ready and warm-up errors are shown separately. Use the refresh button to reload effective choices after a conflict, or retry/select another prepared model after a warm-up error. Unsaved Settings drafts are preserved.

Each recording keeps a detached snapshot of its starting settings through recognition and text delivery. Later settings changes apply to the next recording.
