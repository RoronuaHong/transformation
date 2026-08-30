import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";
import AskPanel from "@/components/AskPanel";
import { siteOrigin } from "@/lib/content";
import { t } from "@/lib/copy";
import { isLocale, locales, type Locale } from "@/lib/locales";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: string }>;
}): Promise<Metadata> {
  const { locale: raw } = await params;
  if (!isLocale(raw)) return {};
  const locale = raw as Locale;
  const origin = siteOrigin();
  const languages = Object.fromEntries(
    locales.map((l) => [l, `${origin}/${l}/ask`])
  );
  languages["x-default"] = `${origin}/zh/ask`;
  return {
    title: t(locale).nav.ask,
    alternates: { canonical: `${origin}/${locale}/ask`, languages },
  };
}

export default async function AskPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  const locale = raw as Locale;
  const copy = t(locale);

  return (
    <main className="ask-page">
      <header className="ask-intro">
        <p className="kicker">{copy.nav.brand}</p>
        <h1 className="hero-line">{copy.nav.ask}</h1>
        <p className="lede">{copy.ask?.lede}</p>
      </header>
      <AskPanel placeholder={copy.ask?.placeholder} />
    </main>
  );
}
