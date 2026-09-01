"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { FeedRow } from "@/lib/feed-rows";
import { fillCopy, type FeedCopy } from "@/lib/copy";

const PAGE_SIZES = [5, 10, 20] as const;

function normalizeQuery(raw: string) {
  return raw.normalize("NFKC").trim().toLowerCase();
}

export function FeedTable({
  rows,
  copy,
}: {
  rows: FeedRow[];
  copy: FeedCopy;
}) {
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(10);

  const filtered = useMemo(() => {
    const q = normalizeQuery(query);
    if (!q) return rows;
    return rows.filter((row) => row.searchText.includes(q));
  }, [rows, query]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));

  useEffect(() => {
    setPage(1);
  }, [query, pageSize]);

  useEffect(() => {
    if (page > pageCount) setPage(pageCount);
  }, [page, pageCount]);

  const slice = useMemo(() => {
    const start = (page - 1) * pageSize;
    return filtered.slice(start, start + pageSize);
  }, [filtered, page, pageSize]);

  if (!rows.length) {
    return <p className="empty">{copy.empty}</p>;
  }

  const from = filtered.length ? (page - 1) * pageSize + 1 : 0;
  const to = Math.min(page * pageSize, filtered.length);

  return (
    <div className="feed-table-wrap">
      <div className="feed-table-toolbar">
        <label className="feed-table-search">
          <span className="sr-only">{copy.searchPlaceholder}</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={copy.searchPlaceholder}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label className="feed-table-pagesize">
          <span>{copy.perPage}</span>
          <select
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number])}
          >
            {PAGE_SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>

      {filtered.length === 0 ? (
        <p className="empty feed-table-empty">{copy.noResults}</p>
      ) : (
        <>
          <div className="feed-table-scroll">
            <table className="feed-table">
              <thead>
                <tr>
                  <th scope="col">{copy.colTitle}</th>
                  <th scope="col">{copy.colPlatform}</th>
                  <th scope="col">{copy.colTopic}</th>
                  <th scope="col">{copy.colHighlights}</th>
                  <th scope="col" className="feed-table-num">
                    {copy.colPoints}
                  </th>
                  <th scope="col">{copy.colAction}</th>
                </tr>
              </thead>
              <tbody>
                {slice.map((row) => (
                  <tr key={`${row.topic}-${row.slug}`}>
                    <td className="feed-table-title">
                      <Link href={row.href}>{row.title}</Link>
                    </td>
                    <td>
                      <span className="feed-table-platform">{row.platformLabel}</span>
                    </td>
                    <td className="feed-table-topic">{row.topicLabel}</td>
                    <td className="feed-table-highlights">{row.highlights || "—"}</td>
                    <td className="feed-table-num">{row.pointCount}</td>
                    <td>
                      <Link className="feed-table-action" href={row.href}>
                        {copy.cta}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <nav className="feed-table-pager" aria-label="Pagination">
            <p className="feed-table-pager-status">
              {fillCopy(copy.pageStatus, { from, to, total: filtered.length })}
            </p>
            <div className="feed-table-pager-btns">
              <button
                type="button"
                className="watch-btn secondary"
                disabled={page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                {copy.pagePrev}
              </button>
              <span className="feed-table-pager-page" aria-current="page">
                {page} / {pageCount}
              </span>
              <button
                type="button"
                className="watch-btn secondary"
                disabled={page >= pageCount}
                onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
              >
                {copy.pageNext}
              </button>
            </div>
          </nav>
        </>
      )}
    </div>
  );
}
