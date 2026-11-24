"use client";

import dynamic from "next/dynamic";
import type { TryCopy } from "@/lib/copy";
import type { Locale } from "@/lib/locales";

const TryForm = dynamic(
  () => import("@/components/TryForm").then((m) => m.TryForm),
  {
    ssr: false,
    loading: () => <p className="try-hint" aria-busy="true">…</p>,
  }
);

export function TryFormLazy({ locale, copy }: { locale: Locale; copy: TryCopy }) {
  return <TryForm locale={locale} copy={copy} />;
}
