import type { MetadataRoute } from "next";
import { loadArticles, siteOrigin } from "@/lib/content";
import { locales } from "@/lib/locales";

export default function sitemap(): MetadataRoute.Sitemap {
  const origin = siteOrigin();
  const articles = loadArticles();
  const entries: MetadataRoute.Sitemap = [];
  for (const locale of locales) {
    entries.push({
      url: `${origin}/${locale}`,
      lastModified: new Date(),
      changeFrequency: "daily",
      priority: 1,
    });
    entries.push({
      url: `${origin}/${locale}/feed`,
      lastModified: new Date(),
      changeFrequency: "daily",
      priority: 0.9,
    });
    for (const article of articles) {
      entries.push({
        url: `${origin}/${locale}/topics/${article.topic}/${article.slug}`,
        lastModified: new Date(),
        changeFrequency: "weekly",
        priority: 0.8,
      });
    }
  }
  return entries;
}
