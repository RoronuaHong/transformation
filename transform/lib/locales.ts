/**
 * 站点 UI 路由语言(16 语),与 subtitle_pipeline/langs.py PACKS["site"] 同步。
 * 字幕/翻译产物走 fununion20(20 语),见下方 subtitleLangs —— 两者刻意解耦:
 * 不为多出的 it/nl/ms/pl 制造没有本地化文案的空壳路由。
 * 一致性校验见 materials_hub/tests/test_multilang.py::test_lang_packs_single_source。
 */
export const locales = [
  "zh",
  "en",
  "ru",
  "ja",
  "ko",
  "pt",
  "de",
  "zh-Hant",
  "es",
  "fr",
  "ar",
  "hi",
  "id",
  "vi",
  "th",
  "tr",
] as const;

export type Locale = (typeof locales)[number];
export const defaultLocale: Locale = "zh";

/** 字幕/翻译产物默认包 fununion20(20 语),与 langs.py PACKS["fununion20"] 严格同步。 */
export const subtitleLangs = [
  "zh", "zh-Hant", "en", "ja", "ko", "es", "pt", "fr", "de",
  "ru", "ar", "hi", "id", "vi", "th", "tr", "it", "nl", "ms", "pl",
] as const;

/** 字幕/翻译产物的语言类型(fununion20 的 20 语,含 it/nl/ms/pl,它们不是站点 UI 路由 Locale)。 */
export type SubtitleLocale = (typeof subtitleLangs)[number];

/** 字幕 20 语的中文/本地名(含 it/nl/ms/pl,不进入站点 Locale 路由)。 */
export const subtitleLangNames: Record<SubtitleLocale, string> = {
  zh: "中文",
  "zh-Hant": "繁中",
  en: "English",
  ja: "日本語",
  ko: "한국어",
  es: "Español",
  pt: "巴西葡语",
  fr: "Français",
  de: "Deutsch",
  ru: "Русский",
  ar: "العربية",
  hi: "हिन्दी",
  id: "Indonesia",
  vi: "Tiếng Việt",
  th: "ไทย",
  tr: "Türkçe",
  it: "Italiano",
  nl: "Nederlands",
  ms: "Melayu",
  pl: "Polski",
};

/** 安全取字幕语言名:未知 code 原样返回(避免 it/nl/ms/pl 越界报错)。 */
export function subtitleLangName(code: string): string {
  return (subtitleLangNames as Record<string, string>)[code] ?? code;
}

export const localeNames: Record<Locale, string> = {
  zh: "中文",
  en: "English",
  ru: "Русский",
  ja: "日本語",
  ko: "한국어",
  pt: "巴西葡语",
  de: "Deutsch",
  "zh-Hant": "繁中",
  es: "Español",
  fr: "Français",
  ar: "العربية",
  hi: "हिन्दी",
  id: "Indonesia",
  vi: "Tiếng Việt",
  th: "ไทย",
  tr: "Türkçe",
};

export function isLocale(v: string): v is Locale {
  return (locales as readonly string[]).includes(v);
}

/** Build `/{locale}/…` without duplicating an existing locale prefix in `path`. */
export function localePath(locale: Locale, path: string): string {
  let p = path.startsWith("/") ? path : `/${path}`;
  for (const loc of locales) {
    const prefix = `/${loc}`;
    if (p === prefix) return `/${locale}`;
    if (p.startsWith(`${prefix}/`)) {
      p = p.slice(prefix.length);
      break;
    }
  }
  return `/${locale}${p}`;
}

export function recordAll(
  partial: Partial<Record<Locale, string>>,
  fallback: string
): Record<Locale, string> {
  const src = partial.zh || partial.en || fallback;
  const out = {} as Record<Locale, string>;
  for (const loc of locales) {
    out[loc] = partial[loc] || src;
  }
  return out;
}
