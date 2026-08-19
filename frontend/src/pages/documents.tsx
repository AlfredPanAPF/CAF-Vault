import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import {
  getDocumentSources,
  getDocuments,
  type DocumentRow,
} from "@/lib/api";
import { fmtDate, fmtNum } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";

const PAGE_SIZE = 50;

// ─── Row ─────────────────────────────────────────────────────

function Dot() {
  return <span className="text-muted-foreground/50">·</span>;
}

/** The badges a document carries in the list (spec v5 §6). */
function StatusBadges({ doc }: { doc: DocumentRow }) {
  return (
    <>
      {(doc.status === "pending" || doc.status === "extracting") && (
        <Badge tone="idle">Pending</Badge>
      )}
      {doc.status === "failed" && <Badge tone="fail">Failed</Badge>}
      {doc.status === "duplicate" && <Badge tone="idle">Duplicate</Badge>}
      {doc.thin && <Badge tone="outline">Headline only</Badge>}
    </>
  );
}

function DocumentItem({ doc }: { doc: DocumentRow }) {
  const dated = doc.published_at ?? doc.fetched_at;
  return (
    <article className="border-b border-border px-6 py-3 last:border-0">
      <Link
        to={`/document/${doc.event_id}`}
        className="block truncate font-medium text-foreground hover:text-accent hover:underline"
      >
        {doc.title}
      </Link>

      {doc.summary && (
        <p className="mt-1 line-clamp-2 leading-relaxed text-muted-foreground">
          {doc.summary}
        </p>
      )}

      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
        <span>{doc.source.name}</span>
        <Dot />
        <span>{doc.type}</span>
        <Dot />
        <span>{fmtDate(dated)}</span>
        <Dot />
        <span>
          {fmtNum(doc.claims)} {doc.claims === 1 ? "claim" : "claims"}
        </span>
        <Dot />
        <span>
          {fmtNum(doc.entities)} {doc.entities === 1 ? "company" : "companies"}
        </span>
        <StatusBadges doc={doc} />
      </div>
    </article>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function DocumentsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";
  const source = searchParams.get("source") ?? "";
  const type = searchParams.get("type") ?? "";
  const status = searchParams.get("status") ?? "";
  const days = searchParams.get("days") ?? "";

  const setParam = (key: string, value: string) => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );
  };

  // Debounced search input, kept in sync with the URL (back/forward).
  const [search, setSearch] = useState(q);
  const lastPushed = useRef(q);
  useEffect(() => {
    if (q !== lastPushed.current) {
      setSearch(q);
      lastPushed.current = q;
    }
  }, [q]);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (search !== q) {
        lastPushed.current = search;
        setParam("q", search);
      }
    }, 300);
    return () => clearTimeout(timer);
    // only re-arm the debounce when the input changes
  }, [search]);

  const query = useInfiniteQuery({
    queryKey: ["documents", { q, source, type, status, days }],
    queryFn: ({ pageParam }) =>
      getDocuments({
        q,
        source,
        type,
        status,
        days,
        limit: PAGE_SIZE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((n, page) => n + page.documents.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });

  const documents = useMemo(
    () => query.data?.pages.flatMap((page) => page.documents) ?? [],
    [query.data],
  );
  const total = query.data?.pages[0]?.total ?? 0;

  const { data: sources } = useQuery({
    queryKey: ["document-sources"],
    queryFn: getDocumentSources,
    staleTime: 60_000,
  });

  return (
    <div>
      {/* Sticky filter bar */}
      <div className="sticky top-0 z-10 border-b border-border bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-6 py-3">
          <Input
            placeholder="Search documents"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-56"
          />
          <Select value={source} onChange={(e) => setParam("source", e.target.value)}>
            <option value="">All sources</option>
            {(sources ?? []).map((row) => (
              <option key={row.source_id} value={row.source_id}>
                {row.name}
              </option>
            ))}
            {source && !(sources ?? []).some((row) => row.source_id === source) && (
              // a deep link to a source the facet does not list (or the
              // facet still loading): keep the select from painting blank
              <option value={source}>{sources ? "Unknown source" : "Loading"}</option>
            )}
          </Select>
          <Select value={type} onChange={(e) => setParam("type", e.target.value)}>
            <option value="">All types</option>
            <option value="filing">Filings</option>
            <option value="article">Articles</option>
            <option value="podcast">Podcasts</option>
            <option value="video">Videos</option>
            <option value="x">X posts</option>
            <option value="bridge">Feed items</option>
            <option value="upload">Uploads</option>
          </Select>
          <Select value={status} onChange={(e) => setParam("status", e.target.value)}>
            <option value="">Any status</option>
            <option value="extracted">Extracted</option>
            <option value="pending">Pending</option>
            <option value="failed">Failed</option>
            <option value="duplicate">Duplicates</option>
          </Select>
          <Select value={days} onChange={(e) => setParam("days", e.target.value)}>
            <option value="1">Today</option>
            <option value="7">7 days</option>
            <option value="30">30 days</option>
            <option value="">All</option>
          </Select>
        </div>
      </div>

      <div className="mx-auto max-w-5xl">
        {query.isPending && (
          <div className="flex justify-center py-24">
            <Spinner />
          </div>
        )}

        {query.isError && (
          <div className="px-6 py-6">
            <Empty>Could not load documents.</Empty>
          </div>
        )}

        {!query.isPending && !query.isError && documents.length === 0 && (
          <div className="px-6 py-6">
            <Empty>No documents match the filters.</Empty>
          </div>
        )}

        {documents.length > 0 && (
          <>
            <p className="px-6 pt-3 text-xs text-muted-foreground">
              {fmtNum(total)} {total === 1 ? "document" : "documents"}
            </p>
            <div>
              {documents.map((doc) => (
                <DocumentItem key={doc.event_id} doc={doc} />
              ))}
            </div>
            {query.hasNextPage && (
              <div className="flex justify-center py-4">
                <Button
                  disabled={query.isFetchingNextPage}
                  onClick={() => void query.fetchNextPage()}
                >
                  {query.isFetchingNextPage && <Spinner className="size-3.5" />}
                  Load more
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
