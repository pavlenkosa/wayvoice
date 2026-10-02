from __future__ import annotations

from typing import Any

MODEL_PRESETS: list[dict[str, Any]] = [
    {"id": "tiny", "label": "Tiny", "subtitle_ru": "Самая лёгкая · многоязычная", "subtitle_en": "Lightest · multilingual", "language": None},
    {"id": "base", "label": "Base", "subtitle_ru": "Быстрая · многоязычная", "subtitle_en": "Fast · multilingual", "language": None},
    {"id": "small", "label": "Small", "subtitle_ru": "Баланс скорости и точности", "subtitle_en": "Balanced speed and accuracy", "language": None},
    {"id": "medium", "label": "Medium", "subtitle_ru": "Точнее, но тяжелее", "subtitle_en": "More accurate, heavier", "language": None},
    {"id": "large-v3", "label": "Large v3", "subtitle_ru": "Максимальная точность · многоязычная", "subtitle_en": "Highest accuracy · multilingual", "language": None},
    {"id": "turbo", "label": "Turbo", "subtitle_ru": "Большая модель с упором на скорость", "subtitle_en": "Large model optimized for speed", "language": None},
    {"id": "tiny.en", "label": "Tiny.en · English", "subtitle_ru": "Английская · очень лёгкая", "subtitle_en": "English-only · very light", "language": "en"},
    {"id": "base.en", "label": "Base.en · English", "subtitle_ru": "Английская · быстрая", "subtitle_en": "English-only · fast", "language": "en"},
    {"id": "small.en", "label": "Small.en · English", "subtitle_ru": "Английская · хороший баланс", "subtitle_en": "English-only · balanced", "language": "en"},
    {"id": "medium.en", "label": "Medium.en · English", "subtitle_ru": "Английская · высокая точность", "subtitle_en": "English-only · high accuracy", "language": "en"},
    {"id": "distil-large-v3", "label": "Distil Large v3 · English", "subtitle_ru": "Английская · ускоренная distilled-модель", "subtitle_en": "English-only · distilled and faster", "language": "en"},
    {"id": "bzikst/faster-whisper-large-v3-russian-int8", "label": "Large v3 Russian INT8 · Русский", "subtitle_ru": "Дообучена на русской речи · community", "subtitle_en": "Fine-tuned for Russian · community", "language": "ru"},
    {"id": "tnfru/whisper-large-v3-german-ct2", "label": "Large v3 German · Deutsch", "subtitle_ru": "Дообучена на немецкой речи · community", "subtitle_en": "Fine-tuned for German · community", "language": "de"},
    {"id": "nekusu/faster-whisper-large-v3-turbo-latam-int8-ct2", "label": "Turbo LATAM INT8 · Español", "subtitle_ru": "Латиноамериканский испанский · community", "subtitle_en": "Latin American Spanish · community", "language": "es"},
    {"id": "ele-sage/whisper-large-v3-turbo-fr-quebecois-ct2", "label": "Turbo Québec · Français", "subtitle_ru": "Французский Квебека · community", "subtitle_en": "Québec French · community", "language": "fr"},
    {"id": "LocalAI-io/whisper-large-v3-it-yodas-only-ct2-int8", "label": "Large v3 Italian INT8 · Italiano", "subtitle_ru": "Дообучена на итальянской речи · community", "subtitle_en": "Fine-tuned for Italian · community", "language": "it"},
    {"id": "__custom__", "label": "Custom CTranslate2", "subtitle_ru": "Hugging Face repo ID или локальный путь", "subtitle_en": "Hugging Face repo ID or local path", "language": None},
]

PRESET_IDS = [str(item["id"]) for item in MODEL_PRESETS]
PRESET_LABELS = [str(item["label"]) for item in MODEL_PRESETS]


def preset_for(model_id: str) -> dict[str, Any] | None:
    for item in MODEL_PRESETS:
        if item["id"] == model_id:
            return item
    return None


def display_name(model_id: str) -> str:
    item = preset_for(model_id)
    if item is not None:
        return str(item["label"]).split(" · ", 1)[0]
    if not model_id:
        return "—"
    return model_id.rsplit("/", 1)[-1]


def forced_language(model_id: str) -> str | None:
    item = preset_for(model_id)
    if item is None:
        return None
    value = item.get("language")
    return str(value) if value else None


def preset_index(model_id: str) -> int:
    try:
        return PRESET_IDS.index(model_id)
    except ValueError:
        return PRESET_IDS.index("__custom__")


def preset_subtitle(item: dict[str, Any], ui_language: str) -> str:
    return str(item.get("subtitle_ru" if ui_language == "ru" else "subtitle_en") or "")
