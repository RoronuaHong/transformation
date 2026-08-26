import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Vitual 后台",
  description: "运营后台（非前台站点）",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh">
      <body>{children}</body>
    </html>
  );
}
