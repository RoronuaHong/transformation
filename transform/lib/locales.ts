/** Keep in sync with subtitle_pipeline/langs.py PACKS["site"]. Nav: 中英俄日韩葡德 first. */
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
