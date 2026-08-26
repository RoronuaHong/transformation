"""Language packs and display names for the subtitle pipeline.

Default pack is ``site`` (16 codes, synced with ``site/lib/locales.ts``).
``all`` = TranslateGemma WMT24++ verified set (dialect-collapsed).
Translation goes through ``translategemma:4b``; chat/polish via ``gemma4:e2b``.
"""

from __future__ import annotations

from typing import Iterable

# Internal code -> English name used in prompts
LANG_NAME: dict[str, str] = {
    "zh": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ru": "Russian",
    "pl": "Polish",
    "nl": "Dutch",
    "sv": "Swedish",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "el": "Greek",
    "cs": "Czech",
    "ro": "Romanian",
    "hu": "Hungarian",
    "ar": "Arabic",
    "fa": "Persian",
    "he": "Hebrew",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "ur": "Urdu",
    "si": "Sinhala",
    "ne": "Nepali",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "fil": "Filipino",
    "my": "Burmese",
    "km": "Khmer",
    "lo": "Lao",
    "sw": "Swahili",
    "am": "Amharic",
    "ha": "Hausa",
    # TranslateGemma WMT24++ extras (beyond former world pack)
    "bg": "Bulgarian",
    "ca": "Catalan",
    "da": "Danish",
    "et": "Estonian",
    "fi": "Finnish",
    "gu": "Gujarati",
    "hr": "Croatian",
    "is": "Icelandic",
    "kn": "Kannada",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "ml": "Malayalam",
    "mr": "Marathi",
    "no": "Norwegian",
    "pa": "Punjabi",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sr": "Serbian",
    "zu": "Zulu",
}

# Whisper / ASR short codes
WHISPER_LANG: dict[str, str] = {
    "zh": "zh",
    "zh-Hant": "zh",
    "zh-Hans": "zh",
}

ALIASES: dict[str, str] = {
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-Hans": "zh",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh-Hant": "zh-Hant",
    "tl": "fil",
    "fil": "fil",
    "pt-br": "pt",
    "pt-pt": "pt",
    "iw": "he",
    # WMT24++ locale tags → pipeline codes
    "ar_eg": "ar",
    "ar_sa": "ar",
    "zh_cn": "zh",
    "zh_tw": "zh-Hant",
    "es_mx": "es",
    "fr_ca": "fr",
    "fr_fr": "fr",
    "pt_br": "pt",
    "pt_pt": "pt",
    "sw_ke": "sw",
    "sw_tz": "sw",
}

PACKS: dict[str, tuple[str, ...]] = {
    # Site pack (16). Keep in sync with site/lib/locales.ts (nav order differs).
    "site": (
        "zh",
        "zh-Hant",
        "en",
        "ja",
        "ko",
        "es",
        "fr",
        "de",
        "pt",
        "ru",
        "ar",
        "hi",
        "id",
        "vi",
        "th",
        "tr",
    ),
    "core": (
        "zh",
        "zh-Hant",
        "en",
        "ja",
        "ko",
        "es",
        "fr",
        "de",
        "pt",
        "ru",
        "ar",
        "hi",
        "id",
        "vi",
        "th",
    ),
    "world": (
        "zh",
        "zh-Hant",
        "ja",
        "ko",
        "vi",
        "th",
        "id",
        "ms",
        "fil",
        "my",
        "km",
        "lo",
        "hi",
        "bn",
        "ta",
        "te",
        "ur",
        "si",
        "ne",
        "en",
        "es",
        "fr",
        "de",
        "it",
        "pt",
        "ru",
        "pl",
        "nl",
        "sv",
        "uk",
        "tr",
        "el",
        "cs",
        "ro",
        "hu",
        "ar",
        "fa",
        "he",
        "sw",
        "am",
        "ha",
    ),
    # TranslateGemma official WMT24++ set (55 pairs → dialect-collapsed codes + en)
    # Dialects ar_EG/ar_SA→ar, zh_CN→zh, zh_TW→zh-Hant, fr_*/pt_*/sw_* collapsed.
    "all": (
        "zh",
        "zh-Hant",
        "en",
        "ja",
        "ko",
        "ar",
        "bg",
        "bn",
        "ca",
        "cs",
        "da",
        "de",
        "el",
        "es",
        "et",
        "fa",
        "fi",
        "fil",
        "fr",
        "gu",
        "he",
        "hi",
        "hr",
        "hu",
        "id",
        "is",
        "it",
        "kn",
        "lt",
        "lv",
        "ml",
        "mr",
        "nl",
        "no",
        "pa",
        "pl",
        "pt",
        "ro",
        "ru",
        "sk",
        "sl",
        "sr",
        "sv",
        "sw",
        "ta",
        "te",
        "th",
        "tr",
        "uk",
        "ur",
        "vi",
        "zu",
    ),
}


def normalize_lang(code: str) -> str:
    raw = (code or "").strip()
    if not raw:
        raise ValueError("empty language code")
    if raw.lower() == "auto":
        return "auto"
    key = raw.strip()
    lower = key.lower().replace("-", "_")
    # try locale-style alias first (ar_eg, zh_cn)
    if lower in ALIASES:
        return ALIASES[lower]
    lower_hyphen = key.lower()
    if lower_hyphen in ALIASES:
        return ALIASES[lower_hyphen]
    if key in LANG_NAME:
        return key
    if key.lower() in LANG_NAME:
        return key.lower()
    # unknown: keep lowercase token so user can still try the model
    return key.lower()


def coalesce_source_lang(code: str, fallback: str = "en") -> str:
    """Map Whisper/file tags like ``src`` / ``auto`` to a real language code."""
    try:
        n = normalize_lang(code)
    except ValueError:
        return normalize_lang(fallback)
    if n in {"src", "auto", "source"} or n not in LANG_NAME:
        return normalize_lang(fallback)
    return n


def lang_name(code: str) -> str:
    n = normalize_lang(code)
    return LANG_NAME.get(n, n)


def whisper_lang(code: str) -> str | None:
    n = normalize_lang(code)
    if n == "auto":
        return None
    return WHISPER_LANG.get(n, n)


def same_language(a: str, b: str) -> bool:
    return normalize_lang(a) == normalize_lang(b)


def resolve_targets(spec: str, source_lang: str) -> list[str]:
    """Expand ``all`` / ``world`` / ``core`` / ``site`` / comma list; drop the source language."""
    codes = expand_pack_codes(spec)
    src = normalize_lang(source_lang)
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        c = normalize_lang(c)
        if c == "auto" or same_language(c, src) or c in seen:
            continue
        if c not in LANG_NAME:
            print(f"[langs] unknown code {c!r}, still sending to translate model")
        seen.add(c)
        out.append(c)
    return out


def expand_pack_codes(spec: str | None) -> list[str]:
    """Expand ``site`` / ``all`` / ``world`` / ``core`` / comma list (keep source if listed)."""
    text = (spec or "site").strip()
    key = text.lower()
    if key in PACKS:
        return list(PACKS[key])
    out: list[str] = []
    seen: set[str] = set()
    for part in text.replace(";", ",").split(","):
        c = normalize_lang(part.strip())
        if not c or c == "auto" or c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out or list(PACKS["site"])


def file_tag(code: str) -> str:
    """Filename suffix: zh, zh-Hant, en, ..."""
    return normalize_lang(code)


def pack_size(name: str) -> int:
    return len(PACKS.get(name.lower(), ()))
