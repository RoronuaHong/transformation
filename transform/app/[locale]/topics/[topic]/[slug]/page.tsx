import { existsSync } from "node:fs";
import path from "node:path";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { DownloadCaptions } from "@/components/DownloadCaptions";
import { ArticleMediaPreview } from "@/components/ArticleMediaPreview";
import { DownloadClips } from "@/components/DownloadClips";
import { DownloadNotes } from "@/components/DownloadNotes";
import { NoteList } from "@/components/NoteList";
import { NotesModal } from "@/components/NotesModal";
import { RemixPlayer } from "@/components/RemixPlayer";
import { UnlockNotes } from "@/components/UnlockNotes";
import {
  NOTES_AD_GATE,
  getArticle,
  hasLockedNotes,
  loadArticles,
  siteOrigin,
  splitNotes,
} from "@/lib/content";
import { t } from "@/lib/copy";
import { isLocale, locales, type Locale } from "@/lib/locales";
import { gifRangeFrames, rangeVideoClips } from "@/lib/note-points";

type Params = { locale: string; topic: string; slug: string };

function derivedRemix(slug: string) {
  const dir = path.join(process.cwd(), "public", "derived", slug);
  if (!existsSync(path.join(dir, "remix.mp4"))) return null;
  const cues = path.join(dir, "remix_cues.json");
  const vtt = path.join(dir, "remix.vtt");
  return {
    video: `/derived/${slug}/remix.mp4`,
    cues: existsSync(cues) ? `/derived/${slug}/remix_cues.json` : undefined,
    vtt: existsSync(vtt) ? `/derived/${slug}/remix.vtt` : undefined,
  };
}

/** Try/export rewrites articles.json often — never bake a stale pack into HTML. */
export const dynamic = "force-dynamic";
export const revalidate = 0;

export function generateStaticParams() {
  const articles = loadArticles();
  return locales.flatMap((locale) =>
    articles.map((a) => ({
      locale,
      topic: a.topic,
      slug: a.slug,
    }))
  );
}

export async function generateMetadata({
  params,
}: {
  params: Promise<Params>;
}): Promise<Metadata> {
  const { locale: raw, topic, slug } = await params;
  if (!isLocale(raw)) return {};
  const article = getArticle(topic, slug);
  if (!article) return {};
  const locale = raw as Locale;
  const origin = siteOrigin();
  const path = `/topics/${topic}/${slug}`;
  const languages = Object.fromEntries(locales.map((l) => [l, `${origin}/${l}${path}`]));
  languages["x-default"] = `${origin}/zh${path}`;
  return {
    title: article.titles[locale],
    description: article.summaries[locale],
    alternates: {
      canonical: `${origin}/${locale}${path}`,
      languages,
    },
    openGraph: {
      title: article.titles[locale],
      description: article.summaries[locale],
      url: `${origin}/${locale}${path}`,
      type: "article",
    },
  };
}

export default async function TopicArticlePage({ params }: { params: Promise<Params> }) {
  const { locale: raw, topic, slug } = await params;
  if (!isLocale(raw)) notFound();
  const article = getArticle(topic, slug);
  if (!article) notFound();
  const locale = raw as Locale;
  const a = article;
  const chrome = t(locale).article;
  const notes = splitNotes(a, locale);
  const showGate = NOTES_AD_GATE && hasLockedNotes(a, locale);
  const watchLabel =
    a.platform === "bilibili"
      ? chrome.watchBilibili
      : a.platform === "douyin"
        ? chrome.watchDouyin
        : chrome.watchYoutube;
  const showWatch = a.platform !== "upload" && a.sourceUrl && !a.sourceUrl.startsWith("upload://");
  const overview = a.overviews[locale] || a.summaries[locale] || "";
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: a.titles[locale],
    description: a.summaries[locale],
    about: t(locale).feed.headline,
    mainEntityOfPage: `${siteOrigin()}/${locale}/topics/${topic}/${slug}`,
    citation: a.sourceUrl,
  };
  const captionLines = notes.cues
    .map((c) => ({
      start: c.start,
      end: c.end,
      text: (c.text[locale] || c.text.zh || Object.values(c.text)[0] || "").trim(),
    }))
    .filter((c) => c.text);

  const notesPayload = {
    title: a.titles[locale],
    one_liner: a.summaries[locale],
    summary: overview,
    focuses: notes.focuses,
    key_points: showGate ? notes.lockedPoints : a.keyPoints[locale],
    hard_points: notes.hardPoints,
  };
  const notesFilename = `${slug}-${locale}-notes.md`;

  const notesModal = (
    <NotesModal
      chrome={chrome}
      overview={overview}
      focuses={notes.focuses}
      keyPoints={showGate ? notes.lockedPoints : a.keyPoints[locale]}
      hardPoints={notes.hardPoints}
      clipLabel={chrome.downloadClipItem}
      downloadFilename={notesFilename}
      downloadNotes={notesPayload}
    />
  );

  const downloadPoints = a.keyPoints[locale];
  const previewGifs = gifRangeFrames(downloadPoints);
  const previewClips = rangeVideoClips(downloadPoints);
  const remix = derivedRemix(a.slug);

  return (
    <main className="article-page">
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
      <article className="article-sheet">
        <p className="crumb">
          <Link href={`/${locale}/feed`}>{chrome.feed}</Link>
        </p>
        <h1 className="hero-line">{a.titles[locale]}</h1>
        {a.summaries[locale] ? <p className="lede">{a.summaries[locale]}</p> : null}

        {remix ? (
          <RemixPlayer
            src={remix.video}
            cuesUrl={remix.cues}
            vttUrl={remix.vtt}
            label={chrome.remixPlay}
          />
        ) : null}

        {previewGifs.length || previewClips.length ? (
          <ArticleMediaPreview
            gifs={previewGifs}
            clips={previewClips}
            title={chrome.mediaPreview}
            gifKind={chrome.mediaGifKind}
            clipTabLabel={chrome.mediaTabClip}
            clipLabel={chrome.downloadClipItem}
            gifLabel={chrome.viewFrame}
            previewLabel={chrome.previewClip}
            closeLabel={chrome.closeNotes}
          />
        ) : null}

        <div className="action-bar">
          {showWatch ? (
            <a className="watch-btn" href={a.sourceUrl} rel="noopener noreferrer" target="_blank">
              {watchLabel}
            </a>
          ) : null}
          {showGate ? (
            <UnlockNotes storageKey={`unlock:${topic}:${slug}`} copy={chrome}>
              {notesModal}
            </UnlockNotes>
          ) : (
            notesModal
          )}
          {captionLines.length ? (
            <DownloadCaptions
              label={chrome.downloadCaptions}
              filename={`${slug}-${locale}.srt`}
              cues={captionLines}
            />
          ) : null}
          <DownloadNotes
            label={chrome.downloadNotes}
            filename={notesFilename}
            notes={notesPayload}
            headings={{
              overview: chrome.overview,
              focuses: chrome.focuses,
              keyPoints: chrome.keyPoints,
              hardPoints: chrome.hardPoints,
            }}
          />
          <DownloadClips
            label={chrome.downloadClips}
            itemLabel={chrome.downloadClipItem}
            emptyLabel={chrome.clipsEmpty}
            closeLabel={chrome.closeNotes}
            downloadLabel={chrome.downloadClipFile}
            previewLabel={chrome.previewClip}
            segmentCaptionsLabel={chrome.segmentCaptions}
            downloadSegmentCaptionsLabel={chrome.downloadSegmentCaptions}
            locale={locale}
            cues={a.cues}
            points={downloadPoints}
          />
        </div>

        {showGate && notes.freePoints.length ? (
          <section className="notes" aria-labelledby="free-points-heading">
            <h2 id="free-points-heading" className="section-title">
              {chrome.keyPoints}
            </h2>
            <NoteList
              heading=""
              items={notes.freePoints}
              viewFrameLabel={chrome.viewFrame}
              closeLabel={chrome.closeNotes}
            />
          </section>
        ) : null}

        <footer className="article-foot">
          <p className="meta">
            {chrome.source}:{" "}
            {showWatch ? (
              <a href={a.sourceUrl} rel="noopener noreferrer" target="_blank">
                {a.platform} · {a.videoId}
              </a>
            ) : (
              <span>
                {a.platform} · {a.videoId}
              </span>
            )}
          </p>
          <p className="disclaimer">{chrome.disclaimer}</p>
        </footer>
      </article>
    </main>
  );
}
