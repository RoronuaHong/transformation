"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

/** In-app materials hub (Next route), not the legacy :8000 HTML shell. */
export function HubLink({ label }: { label: string }) {
  const params = useParams();
  const locale = typeof params?.locale === "string" ? params.locale : "zh";
  return <Link href={`/${locale}/hub`}>{label}</Link>;
}
