import Link from "next/link";
import { notFound } from "next/navigation";
import { LangSwitch } from "@/components/LangSwitch";
import { ThemeToggle } from "@/components/ThemeToggle";
import { isRtl, t } from "@/lib/copy";
import { locales, type Locale, isLocale } from "@/lib/locales";

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale: raw } = await params;
  if (!isLocale(raw)) notFound();
  const locale = raw as Locale;
  const nav = t(locale).nav;

  return (
    <div
      className="shell"
      lang={locale}
      dir={isRtl(locale) ? "rtl" : "ltr"}
      suppressHydrationWarning
    >
      <a className="skip-link" href="#main">
        跳到正文
      </a>
      <header className="nav" suppressHydrationWarning>
        <div className="brand">
          <Link href={`/${locale}`}>
            <span className="brand-name">{nav.brand}</span>
          </Link>
        </div>
        <nav className="nav-links" aria-label={nav.brand}>
          <Link href={`/${locale}/feed`}>{nav.feed}</Link>
          <Link href={`/${locale}/ask`}>{nav.ask}</Link>
          <ThemeToggle locale={locale} />
          <LangSwitch locale={locale} />
        </nav>
      </header>
      <div id="main">{children}</div>
    </div>
  );
}
