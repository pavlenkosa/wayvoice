from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="ru")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--vad", action="store_true")
    args = parser.parse_args()

    import ctranslate2
    from faster_whisper import WhisperModel

    device = args.device
    if device == "auto":
        try:
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(args.model, device=device, compute_type=compute_type)
    language = None if args.language in {"", "auto"} else args.language
    kwargs = {
        "language": language,
        "beam_size": max(1, args.beam_size),
        "vad_filter": args.vad,
        "condition_on_previous_text": False,
    }
    if args.vad:
        kwargs["vad_parameters"] = {
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 180,
        }
    segments, _ = model.transcribe(args.audio, **kwargs)

    # Conservative no-speech guard. It intentionally requires both a high
    # no-speech probability and a poor log probability so quiet real speech is
    # not discarded just because it is difficult to decode.
    accepted: list[str] = []
    for segment in segments:
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        avg_logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        if no_speech > 0.72 and avg_logprob < -1.0:
            continue
        text = str(getattr(segment, "text", "") or "").strip()
        if text:
            accepted.append(text)
    print(" ".join(accepted).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
