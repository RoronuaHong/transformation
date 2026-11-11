import type { Metadata } from "next";
import { notFound } from "next/navigation";
import CrawlerConsole from "@/components/CrawlerConsole";
import { isLocale, type Locale } from "@/lib/locales";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ locale: string }>;
}): Promise<Metadata> {
  const { locale: raw } = await params;
  if (!isLocale(raw)) return {};
  return { title: "素材采集 · Vitual" };
}

export default async function CrawlerPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  const _locale = raw as Locale;
  void _locale;
  return <CrawlerConsole />;
}
