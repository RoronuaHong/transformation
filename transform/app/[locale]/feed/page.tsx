import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { FeedTable } from "@/components/FeedTable";
import { feedArticles, siteOrigin } from "@/lib/content";
import { t } from "@/lib/copy";
import { buildFeedRows } from "@/lib/feed-rows";
import { isLocale, locales, type Locale } from "@/lib/locales";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: string }>;
}): Promise<Metadata> {
  const { locale: raw } = await params;
  if (!isLocale(raw)) return {};
  const locale = raw as Locale;
  const origin = siteOrigin();
  const copy = t(locale).feed;
  const languages = Object.fromEntries(locales.map((l) => [l, `${origin}/${l}/feed`]));
  languages["x-default"] = `${origin}/zh/feed`;
  return {
    title: copy.headline,
    description: copy.lede,
    alternates: {
      canonical: `${origin}/${locale}/feed`,
      languages,
    },
  };
}

export default async function FeedPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  const locale = raw as Locale;
  const copy = t(locale).feed;
  const articles = feedArticles();

  return (
    <main className="home-page">
      <header className="home-intro">
        <p className="kicker">{copy.kicker}</p>
        <h1 className="hero-line">{copy.headline}</h1>
        <p className="lede">{copy.lede}</p>
      </header>
      <FeedTable rows={buildFeedRows(articles, locale)} copy={copy} />
    </main>
  );
}
