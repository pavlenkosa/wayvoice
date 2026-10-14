# Engines, models and languages

[WayVoice](../README.md)

### Recognition engines

**Faster-Whisper** is the default. Choose a built-in model, a compatible Hugging Face repository, or a local CTranslate2 model directory. Keeping the model warm speeds up repeated dictation at the cost of idle memory use.

**whisper.cpp** uses your local `whisper-cli` binary and a compatible model file.

**Parakeet v3 (experimental CPU)** runs `parakeet-tdt-0.6b-v3` locally through the official NeMo-Speech.cpp CLI, once per dictation. It is not kept warm. The default engine remains Faster-Whisper.

Select Parakeet in Settings and click **Download CPU runtime and model (~690 MiB)** to prepare the pinned runtime and Q8_0 weights explicitly. Leave its path fields empty to use the defaults. Saving settings and pressing the recording shortcut never download anything. Custom executable/model paths are advanced overrides and are not replaced by preparation.

For preparation from a source checkout:

```bash
PYTHONPATH=app/src python3 -m wayvoice.parakeet_setup
```

Preparation verifies SHA256 and preserves existing files on failure. Downloads trust NVIDIA's GitHub release and Hugging Face model repository; SHA256 checks integrity against the pinned artifacts. Runtime: [NeMo-Speech.cpp v0.2.0](https://github.com/NVIDIA/NeMo-Speech.cpp/releases/tag/v0.2.0), Apache-2.0 plus bundled third-party notices. Weights: [Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), CC-BY-4.0, revision `541d1f99c6b0c3cd0b11a95167540bb8edefd82b`. Default files live under `$XDG_DATA_HOME/wayvoice/parakeet` (normally `~/.local/share/wayvoice/parakeet`). Flatpak uses its private data directory and bundled CPU runtime.

Language detection is automatic; this integration does not force a language or use Whisper's beam, VAD, device or worker settings. Supported model languages: Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French, German, Greek, Hungarian, Italian, Latvian, Lithuanian, Maltese, Polish, Portuguese, Romanian, Russian, Slovak, Slovenian, Spanish, Swedish and Ukrainian. User RU/EN quality and latency comparison remains pending; no speed or accuracy advantage is claimed.

**External command** passes the recording to another program and reads its standard output as text. For example:

```text
vosk-transcriber -i {audio}
```

The template is executed through `/bin/sh`; `{audio}` is replaced with the recording path. Only use commands you trust.

## Supported languages

**Interface (10):** English (`en`), Russian (`ru`), Spanish (`es`), Portuguese (`pt`), French (`fr`), German (`de`), Simplified Chinese (`zh`), Japanese (`ja`), Arabic (`ar`) and Hindi (`hi`). Automatic selection follows the system language. Arabic uses a right-to-left layout. Translations are bundled and work offline.

**Speech recognition:** multilingual Faster-Whisper and whisper.cpp models use the following language choices, with automatic detection or a fixed language. Actual availability and accuracy depend on the selected engine and model; English-only models recognize English. External commands define their own language support.

<details>
<summary>Show all 100 recognition language choices</summary>

Afrikaans (`af`), Albanian (`sq`), Amharic (`am`), Arabic (`ar`), Armenian (`hy`), Assamese (`as`), Azerbaijani (`az`), Bashkir (`ba`), Basque (`eu`), Belarusian (`be`), Bengali (`bn`), Bosnian (`bs`), Breton (`br`), Bulgarian (`bg`), Cantonese (`yue`), Catalan (`ca`), Chinese (`zh`), Croatian (`hr`), Czech (`cs`), Danish (`da`), Dutch (`nl`), English (`en`), Estonian (`et`), Faroese (`fo`), Finnish (`fi`), French (`fr`), Galician (`gl`), Georgian (`ka`), German (`de`), Greek (`el`), Gujarati (`gu`), Haitian creole (`ht`), Hausa (`ha`), Hawaiian (`haw`), Hebrew (`he`), Hindi (`hi`), Hungarian (`hu`), Icelandic (`is`), Indonesian (`id`), Italian (`it`), Japanese (`ja`), Javanese (`jw`), Kannada (`kn`), Kazakh (`kk`), Khmer (`km`), Korean (`ko`), Lao (`lo`), Latin (`la`), Latvian (`lv`), Lingala (`ln`), Lithuanian (`lt`), Luxembourgish (`lb`), Macedonian (`mk`), Malagasy (`mg`), Malay (`ms`), Malayalam (`ml`), Maltese (`mt`), Maori (`mi`), Marathi (`mr`), Mongolian (`mn`), Myanmar (`my`), Nepali (`ne`), Norwegian (`no`), Nynorsk (`nn`), Occitan (`oc`), Pashto (`ps`), Persian (`fa`), Polish (`pl`), Portuguese (`pt`), Punjabi (`pa`), Romanian (`ro`), Russian (`ru`), Sanskrit (`sa`), Serbian (`sr`), Shona (`sn`), Sindhi (`sd`), Sinhala (`si`), Slovak (`sk`), Slovenian (`sl`), Somali (`so`), Spanish (`es`), Sundanese (`su`), Swahili (`sw`), Swedish (`sv`), Tagalog (`tl`), Tajik (`tg`), Tamil (`ta`), Tatar (`tt`), Telugu (`te`), Thai (`th`), Tibetan (`bo`), Turkish (`tr`), Turkmen (`tk`), Ukrainian (`uk`), Urdu (`ur`), Uzbek (`uz`), Vietnamese (`vi`), Welsh (`cy`), Yiddish (`yi`), Yoruba (`yo`).

</details>

The interface language and recognition language are separate settings.

No streaming or cloud transcription is implied by this page.
