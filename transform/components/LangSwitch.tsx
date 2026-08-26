"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { t } from "@/lib/copy";
import { localeNames, locales, type Locale } from "@/lib/locales";

function pathRest(pathname: string): string {
  return pathname.replace(/^\/[^/]+/, "") || "";
}

/**
 * Shared layout + usePathname() can SSR with a stale path (e.g. article) while
 * the client hydrates on /try. Defer path-dependent hrefs until after mount so
 * server HTML and the first client render always match.
 */
export function LangSwitch({ locale }: { locale: Locale }) {
  const pathname = usePathname() || `/${locale}`;
  const [rest, setRest] = useState("");

  useEffect(() => {
    setRest(pathRest(pathname));
  }, [pathname]);

  return (
    <details className="lang-menu">
      <summary>{t(locale).nav.language}</summary>
      <nav className="lang-switch" aria-label={t(locale).nav.language}>
        {locales.map((l) => (
          <Link key={l} href={`/${l}${rest}`} aria-current={l === locale ? "page" : undefined}>
            {localeNames[l]}
          </Link>
        ))}
      </nav>
    </details>
  );
}
