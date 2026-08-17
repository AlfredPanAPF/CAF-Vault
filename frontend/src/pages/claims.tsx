import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import {
  apiFetch,
  type Claim,
  type ClaimObject,
  type ClaimsResponse,
  type SourcesResponse,
} from "@/lib/api";
import { fmtDate, fmtNum } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Empty } from "@/components/ui/empty";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip } from "@/components/ui/tooltip";

const PAGE_SIZE = 50;
const QUOTE_LIMIT = 200;

const CONFIDENCE_TIP =
  "How confident extraction was in reading the source. Not a truth score.";
const STANCE_TIP =
  "Stated: the source asserts it. Reported: the source cites someone else. Speculative: opinion or forecast.";

// ─── Row pieces ──────────────────────────────────────────────

function EntityRef({
  surface,
  entityId,
  name,
}: {
  surface: string | null | undefined;
  entityId: string | null | undefined;
  name: string | null | undefined;
}) {
  if (entityId) {
    return (
      <Link
        to={`/entity/${entityId}`}
        className="font-mono font-medium text-foreground hover:text-accent hover:underline"
      >
        {name ?? surface}
      </Link>
    );
  }
  return <span className="font-mono font-medium">{surface}</span>;
}

function prettyLiteral(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(prettyLiteral).join(", ");
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, v]) => `${key}: ${prettyLiteral(v)}`)
      .join(", ");
  }
  return String(value);
}

function ObjectRef({ object }: { object: ClaimObject }) {
  if (object.entity_id) {
    return (
      <EntityRef surface={object.surface} entityId={object.entity_id} name={object.name} />
    );
  }
  if (object.literal !== null && object.literal !== undefined) {
    return <span className="font-mono">{prettyLiteral(object.literal)}</span>;
  }
  return <span>{object.surface}</span>;
}

function Dot() {
  return <span className="text-muted-foreground/50">·</span>;
}

function stanceLabel(stance: string): string {
  return stance.charAt(0).toUpperCase() + stance.slice(1);
}

function fmtConfidence(confidence: number): string {
  return String(Math.round(confidence * 100) / 100);
}

export function ClaimRow({ claim }: { claim: Claim }) {
  const [expanded, setExpanded] = useState(false);
  // staleness-decayed value when the API provides it (spec v3 §5.3)
  const confidence = claim.confidence_now ?? claim.confidence;
  const quote = claim.evidence_quote ?? "";
  const expandable = quote.length > QUOTE_LIMIT;
  const shown =
    expandable && !expanded ? `${quote.slice(0, QUOTE_LIMIT).trimEnd()}…` : quote;

  return (
    <article className="border-b border-border px-6 py-3 last:border-0">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <EntityRef
          surface={claim.subject.surface}
          entityId={claim.subject.entity_id}
          name={claim.subject.name}
        />
        <span className="font-mono text-xs text-muted-foreground">{claim.predicate}</span>
        <ObjectRef object={claim.object} />
      </div>

      {quote &&
        (expandable ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="mt-1 block text-left leading-relaxed text-foreground/80"
          >
            “{shown}”
          </button>
        ) : (
          <p className="mt-1 leading-relaxed text-foreground/80">“{shown}”</p>
        ))}

      <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
        {claim.source_name && <span>{claim.source_name}</span>}
        {claim.doc_title && (
          <>
            <Dot />
            <span className="max-w-72 truncate">{claim.doc_title}</span>
          </>
        )}
        {claim.published_at && (
          <>
            <Dot />
            <span>{fmtDate(claim.published_at)}</span>
          </>
        )}
        {claim.status && claim.status !== "asserted" && (
          <>
            <Dot />
            <Tooltip label="Withdrawn from the current view: a newer claim superseded this one.">
              <Badge tone="warn">{stanceLabel(claim.status)}</Badge>
            </Tooltip>
          </>
        )}
        {claim.stance && claim.stance !== "stated" && (
          <>
            <Dot />
            <Tooltip label={STANCE_TIP}>
              <Badge tone="outline">{stanceLabel(claim.stance)}</Badge>
            </Tooltip>
          </>
        )}
        {confidence !== null && (
          <>
            <Dot />
            <Tooltip label={CONFIDENCE_TIP}>
              <span className="font-mono">{fmtConfidence(confidence)}</span>
            </Tooltip>
          </>
        )}
      </div>
    </article>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function ClaimsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";
  const predicate = searchParams.get("predicate") ?? "";
  const source = searchParams.get("source") ?? "";
  const stance = searchParams.get("stance") ?? "";
  const sector = searchParams.get("sector") ?? "";
  const days = searchParams.get("days") ?? "";
  const entity = searchParams.get("entity") ?? "";

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
    queryKey: ["claims", { q, predicate, source, stance, sector, days, entity }],
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (predicate) params.set("predicate", predicate);
      if (source) params.set("source_type", source);
      if (stance) params.set("stance", stance);
      if (sector) params.set("sector", sector);
      if (days) params.set("days", days);
      if (entity) params.set("entity", entity);
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(pageParam));
      return apiFetch<ClaimsResponse>(`/api/claims?${params.toString()}`);
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((n, page) => n + page.claims.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });

  const claims = useMemo(
    () => query.data?.pages.flatMap((page) => page.claims) ?? [],
    [query.data],
  );
  const total = query.data?.pages[0]?.total ?? 0;

  // Canonical predicates when the gardener has mapped them, raw otherwise
  // (spec v3 §6); the API filters on either form.
  const predicates = useMemo(() => {
    const set = new Set<string>();
    for (const claim of claims) set.add(claim.predicate_canon ?? claim.predicate);
    if (predicate) set.add(predicate);
    return [...set].sort();
  }, [claims, predicate]);

  // Sector options come from the watchlist.
  const { data: sourcesData } = useQuery({
    queryKey: ["sources"],
    queryFn: () => apiFetch<SourcesResponse>("/api/sources"),
    staleTime: 60_000,
  });
  const sectors = useMemo(() => {
    const set = new Set<string>();
    for (const row of sourcesData?.watchlist ?? []) {
      if (row.sector) set.add(row.sector);
    }
    if (sector) set.add(sector);
    return [...set].sort();
  }, [sourcesData, sector]);

  return (
    <div>
      {/* Sticky filter bar */}
      <div className="sticky top-0 z-10 border-b border-border bg-background/95 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-2 px-6 py-3">
          <Input
            placeholder="Search claims"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-56"
          />
          <Select value={predicate} onChange={(e) => setParam("predicate", e.target.value)}>
            <option value="">All predicates</option>
            {predicates.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </Select>
          <Select value={source} onChange={(e) => setParam("source", e.target.value)}>
            <option value="">All sources</option>
            <option value="edgar">EDGAR</option>
            <option value="podcast">Podcasts</option>
            <option value="rss">News feeds</option>
            <option value="manual">Uploads</option>
          </Select>
          <Select value={stance} onChange={(e) => setParam("stance", e.target.value)}>
            <option value="">All stances</option>
            <option value="stated">Stated</option>
            <option value="reported">Reported</option>
            <option value="speculative">Speculative</option>
          </Select>
          <Select value={sector} onChange={(e) => setParam("sector", e.target.value)}>
            <option value="">All sectors</option>
            {sectors.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
          <Select value={days} onChange={(e) => setParam("days", e.target.value)}>
            <option value="1">Today</option>
            <option value="7">7 days</option>
            <option value="30">30 days</option>
            <option value="">All</option>
          </Select>
          {entity && (
            <Button variant="ghost" size="sm" onClick={() => setParam("entity", "")}>
              Clear company filter
            </Button>
          )}
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
            <Empty>Could not load claims.</Empty>
          </div>
        )}

        {!query.isPending && !query.isError && claims.length === 0 && (
          <div className="px-6 py-6">
            <Empty>No claims match the filters.</Empty>
          </div>
        )}

        {claims.length > 0 && (
          <>
            <p className="px-6 pt-3 text-xs text-muted-foreground">
              {fmtNum(total)} claims
            </p>
            <div>
              {claims.map((claim) => (
                <ClaimRow key={claim.claim_id} claim={claim} />
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
