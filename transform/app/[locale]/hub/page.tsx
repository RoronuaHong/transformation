import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { MaterialsHub } from "@/components/MaterialsHub";
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
  const copy = t(locale).hub;
  const origin = siteOrigin();
  return {
    title: copy.headline,
    description: copy.lede,
    alternates: {
      canonical: `${origin}/${locale}/hub`,
      languages: Object.fromEntries(
        locales.map((l) => [l, `${origin}/${l}/hub`]),
      ),
    },
  };
}

export default async function HubPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  const locale = raw as Locale;
  const copy = t(locale).hub;
  return (
    <main className="home-page">
      <MaterialsHub copy={copy} />
    </main>
  );
}
