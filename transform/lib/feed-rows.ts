import { previewPoints, type SampleArticle } from "@/lib/content";
import type { Locale } from "@/lib/locales";
import { isRangeVideoClip } from "@/lib/note-points";

export type FeedRow = {
  slug: string;
  topic: string;
  topicLabel: string;
  platformLabel: string;
  title: string;
  highlights: string;
  pointCount: number;
  href: string;
  searchText: string;
};

const TOPIC_LABELS: Record<string, Record<string, string>> = {
  general: { zh: "综合", "zh-Hant": "綜合", en: "General" },
  home_tips: { zh: "生活技巧", "zh-Hant": "生活技巧", en: "Home tips" },
};

export function platformLabel(platform: SampleArticle["platform"]) {
  if (platform === "bilibili") return "Bilibili";
  if (platform === "douyin") return "Douyin";
  if (platform === "upload") return "Upload";
  return "YouTube";
}

export function topicLabel(topic: string, locale: Locale) {
  const row = TOPIC_LABELS[topic];
  if (row) return row[locale] || row.en || topic;
  return topic.replace(/_/g, " ");
}

export function buildFeedRows(articles: SampleArticle[], locale: Locale): FeedRow[] {
  return articles.map((article) => {
    const href = `/${locale}/topics/${article.topic}/${article.slug}`;
    const title = article.titles[locale] || article.titles.zh || article.slug;
    const summary = article.summaries[locale] || "";
    const preview = previewPoints(article, locale);
    const notePoints = (article.keyPoints[locale] || []).filter((kp) => !isRangeVideoClip(kp));
    const highlights = preview
      .filter((kp) => !isRangeVideoClip(kp))
      .map((kp) => kp.title)
      .join(" · ");
    const searchText = [
      title,
      summary,
      article.topic,
      topicLabel(article.topic, locale),
      platformLabel(article.platform),
      ...notePoints.map((kp) => `${kp.title} ${kp.detail}`),
    ]
      .join(" ")
      .normalize("NFKC")
      .toLowerCase();

    return {
      slug: article.slug,
      topic: article.topic,
      topicLabel: topicLabel(article.topic, locale),
      platformLabel: platformLabel(article.platform),
      title,
      highlights,
      pointCount: notePoints.length,
      href,
      searchText,
    };
  });
}
