"""Language packs and display names for the subtitle pipeline.

Default pack is ``fununion20`` (20 codes, the approved short-video subtitle pack).
``site`` (16 codes) stays for the website UI routes and explicit legacy requests;
``all`` = TranslateGemma WMT24++ verified set (dialect-collapsed).
Translation goes through ``translategemma:4b``; chat/polish via ``gemma4:e2b``.
"""

from __future__ import annotations

from typing import Iterable

# 单一真源:所有字幕/翻译产出的默认语言包。改这里要同步
# materials_hub/core.py HUB_LANGS 与 transform/lib/locales.ts subtitleLangs。
DEFAULT_PACK = "fununion20"

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
    # FunUnion 短视频字幕：审核通过自动生成的 20 语包（站本体 + 全球广覆盖 + 东南亚/中东 + 欧洲补全）
    "fununion20": (
        "zh",
        "zh-Hant",
        "en",
        "ja",
        "ko",
        "es",
        "pt",
        "fr",
        "de",
        "ru",
        "ar",
        "hi",
        "id",
        "vi",
        "th",
        "tr",
        "it",
        "nl",
        "ms",
        "pl",
    ),
    # Site pack (16). Keep in sync with transform/lib/locales.ts (nav order differs).
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
    """Expand ``fununion20`` / ``site`` / ``all`` / ``world`` / ``core`` / comma list."""
    text = (spec or DEFAULT_PACK).strip()
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
    return out or list(PACKS[DEFAULT_PACK])


def file_tag(code: str) -> str:
    """Filename suffix: zh, zh-Hant, en, ..."""
    return normalize_lang(code)


def pack_size(name: str) -> int:
    return len(PACKS.get(name.lower(), ()))


# zh → zh-Hant 走确定性逐字转换，不经 LLM：避免同一片源的简/繁轨被模型
# 自由改写（曾出现简轨「延绵」/繁轨「延綿不絕」、繁轨丢「的/犹」）。
# 本环境全离线，未装 opencc/zhconv；表覆盖产线字幕高频字，缺字由
# translation_integrity.qa_translation_integrity 的等长检查暴露出来再补。
ZH_HANT_MAP: dict[str, str] = {
    "国": "國", "户": "戶", "还": "還", "仅": "僅", "经": "經", "爱": "愛",
    "过": "過", "个": "個", "为": "為", "发": "發", "证": "證", "号": "號",
    "么": "麼", "说": "說", "来": "來", "没": "沒", "欢": "歡", "对": "對",
    "许": "許", "时": "時", "会": "會", "谁": "誰", "绝": "絕", "无": "無",
    "风": "風", "区": "區", "张": "張", "宝": "寶", "犹": "猶", "绵": "綿",
    "敌": "敵", "们": "們", "这": "這", "后": "後", "见": "見", "觉": "覺",
    "学": "學", "习": "習", "电": "電", "话": "話", "视": "視", "频": "頻",
    "网": "網", "络": "絡", "账": "帳", "关": "關", "点": "點", "赞": "讚",
    "评": "評", "论": "論", "简": "簡", "体": "體", "转": "轉", "换": "換",
    "语": "語", "译": "譯", "错": "錯", "误": "誤", "检": "檢", "复": "復",
    "测": "測", "试": "試", "数": "數", "据": "據", "显": "顯", "长": "長",
    "开": "開", "终": "終", "进": "進", "画": "畫", "声": "聲", "轨": "軌",
    "辑": "輯", "导": "導", "软": "軟", "设": "設", "备": "備", "统": "統",
    "档": "檔", "夹": "夾", "执": "執", "环": "環", "变": "變", "认": "認",
    "类": "類", "参": "參", "状": "狀", "态": "態", "记": "記", "录": "錄",
    "报": "報", "质": "質", "标": "標", "阈": "閾", "盖": "蓋", "范": "範",
    "围": "圍", "监": "監", "识": "識", "词": "詞", "汇": "匯", "术": "術",
    "热": "熱", "页": "頁", "链": "鏈", "库": "庫", "键": "鍵", "删": "刪",
    "补": "補", "验": "驗", "结": "結", "败": "敗", "单": "單", "双": "雙",
    "际": "際", "确": "確", "务": "務", "实": "實", "义": "義", "议": "議", "题": "題",
    "应": "應", "该": "該", "当": "當", "东": "東", "车": "車",
    "马": "馬", "鸟": "鳥", "鱼": "魚", "书": "書", "读": "讀", "写": "寫",
    "听": "聽", "观": "觀", "众": "眾", "传": "傳", "领": "領",
    "组": "組", "织": "織", "联": "聯",
}


def to_traditional(text: str) -> str:
    """简体 → 繁体逐字映射（不增删、不改写，字数与源文一致）。"""
    return "".join(ZH_HANT_MAP.get(ch, ch) for ch in (text or ""))
