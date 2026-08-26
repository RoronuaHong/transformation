import { redirect } from "next/navigation";
import { isLocale, type Locale } from "@/lib/locales";

/** Legacy URL — tool lives on /{locale}. */
export default async function TryRedirectPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) redirect("/zh");
  const locale = raw as Locale;
  redirect(`/${locale}`);
}
