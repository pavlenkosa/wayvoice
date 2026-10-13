# Engines, models and languages

[WayVoice](../README.md)

### Recognition engines

**Faster-Whisper** is the default. Choose a built-in model, a compatible Hugging Face repository, or a local CTranslate2 model directory. Keeping the model warm speeds up repeated dictation at the cost of idle memory use.

**whisper.cpp** uses your local `whisper-cli` binary and a compatible model file.

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

Parakeet is not integrated in this version. No streaming or cloud transcription is implied by this page.
