import type { Metadata } from "next";
import { notFound } from "next/navigation";
import Watchlist from "@/components/Watchlist";
import { siteOrigin } from "@/lib/content";
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
  return {
    title: "观察列表",
    description:
      "10 大热门币观察：定时记录 1h / 4h / 1d / 1w 指标，比对方向变化与动量排名，生成方向预测并统计胜率。",
    alternates: {
      canonical: `${origin}/${locale}/watchlist`,
      languages: Object.fromEntries(
        locales.map((l) => [l, `${origin}/${l}/watchlist`]),
      ),
    },
  };
}

export default async function WatchlistPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  return (
    <main className="home-page">
      <Watchlist />
    </main>
  );
}
