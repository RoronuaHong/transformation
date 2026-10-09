"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { HubLink } from "@/components/HubLink";
import { LangSwitch } from "@/components/LangSwitch";
import { ThemeToggle } from "@/components/ThemeToggle";
import type { NavCopy } from "@/lib/copy";
import type { Locale } from "@/lib/locales";

/**
 * 站点导航(客户端):桌面端与原布局一致;移动端收进汉堡菜单。
 * 对齐 WAI-ARIA 惯例:aria-expanded/aria-controls、Esc 关闭并归还焦点、
 * 路由变化自动收起、面板内链接点击即收起。
 */
export function SiteNav({ locale, nav }: { locale: Locale; nav: NavCopy }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const burgerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        burgerRef.current?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <>
      <button
        type="button"
        ref={burgerRef}
        className="nav-burger"
        aria-expanded={open}
        aria-controls="site-menu"
        aria-label={nav.menu}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="nav-burger-box" aria-hidden="true">
          <span />
          <span />
          <span />
        </span>
      </button>
      <nav
        id="site-menu"
        className={open ? "nav-links open" : "nav-links"}
        aria-label={nav.brand}
      >
        <Link href={`/${locale}`} onClick={() => setOpen(false)}>
          {nav.workbench}
        </Link>
        <Link href={`/${locale}/feed`} onClick={() => setOpen(false)}>
          {nav.feed}
        </Link>
        <Link href={`/${locale}/ask`} onClick={() => setOpen(false)}>
          {nav.ask}
        </Link>
        <HubLink label={nav.hub} />
        <ThemeToggle locale={locale} />
        <LangSwitch locale={locale} />
      </nav>
    </>
  );
}
