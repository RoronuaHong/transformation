import Link from "next/link";
import { interpolate, t, type UiCopy } from "@/lib/copy";
import { previewPoints, type SampleArticle } from "@/lib/content";
import type { Locale } from "@/lib/locales";

const TABS = ["var(--tab-saffron)", "var(--tab-mint)", "var(--tab-coral)"] as const;

export function FeedCard({
  article,
  locale,
  index,
  copy,
}: {
  article: SampleArticle;
  locale: Locale;
  index: number;
  copy: UiCopy;
}) {
  const href = `/${locale}/topics/${article.topic}/${article.slug}`;
  const preview = previewPoints(article, locale);
  const extra = Math.max(0, article.keyPoints[locale].length - preview.length);
  const tab = TABS[index % TABS.length];

  return (
    <article className="bin-card" style={{ ["--tab" as string]: tab }}>
      <p className="tape">
        {article.platform === "bilibili"
          ? "Bilibili"
          : article.platform === "douyin"
            ? "Douyin"
            : article.platform === "upload"
              ? "Upload"
              : "YouTube"}
      </p>
      <h2>
        <Link href={href}>{article.titles[locale]}</Link>
      </h2>
      {preview.length ? (
        <ol className="preview-points">
          {preview.map((kp) => (
            <li key={kp.title}>{kp.title}</li>
          ))}
        </ol>
      ) : (
        <p className="card-lede">{article.summaries[locale]}</p>
      )}
      <p className="card-foot">
        <Link href={href}>{copy.feed.cta}</Link>
        {extra > 0 ? <span>{interpolate(copy.article.morePoints, extra)}</span> : null}
      </p>
    </article>
  );
}

export function HomeFeed({
  articles,
  locale,
}: {
  articles: SampleArticle[];
  locale: Locale;
}) {
  const copy = t(locale);
  if (!articles.length) {
    return <p className="empty">{copy.feed.empty}</p>;
  }
  return (
    <div className="feed">
      {articles.map((article, index) => (
        <FeedCard
          key={`${article.topic}-${article.slug}`}
          article={article}
          locale={locale}
          index={index}
          copy={copy}
        />
      ))}
    </div>
  );
}
