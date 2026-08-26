import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { TryForm } from "@/components/TryForm";
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
  const copy = t(locale).try;
  const origin = siteOrigin();
  const languages = Object.fromEntries(locales.map((l) => [l, `${origin}/${l}`]));
  languages["x-default"] = `${origin}/zh`;
  return {
    title: copy.headline,
    description: copy.lede,
    alternates: {
      canonical: `${origin}/${locale}`,
      languages,
    },
  };
}

export default async function ToolHomePage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  const locale = raw as Locale;
  const copy = t(locale).try;

  return (
    <main className="home-page">
      <header className="home-intro try-intro">
        <p className="kicker">{copy.kicker}</p>
        <h1 className="hero-line">{copy.headline}</h1>
        <p className="lede">{copy.lede}</p>
      </header>
      <TryForm locale={locale} copy={copy} />
    </main>
  );
}
