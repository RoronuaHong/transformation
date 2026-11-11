"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";

/** In-app materials hub (Next route), not the legacy :8000 HTML shell. */
export function HubLink({ label }: { label: string }) {
  const params = useParams();
  const pathname = usePathname();
  const locale = typeof params?.locale === "string" ? params.locale : "zh";
  const base = `/${locale}/hub`;
  const active =
    pathname === base ||
    (pathname.startsWith(base + "/") && pathname !== `${base}/crawler`);
  return (
    <Link href={base} aria-current={active ? "page" : undefined}>
      {label}
    </Link>
  );
}
