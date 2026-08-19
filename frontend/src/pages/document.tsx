import { useState } from "react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { toast } from "sonner";

import {
  apiFetch,
  ApiError,
  getDocument,
  getDocumentText,
  requestSummary,
  type Claim,
  type DocumentAlert,
  type DocumentContradiction,
  type DocumentDetailRow,
  type DocumentEdge,
  type DocumentEntity,
  type DocumentFacts,
  type DocumentHypothesis,
  type DocumentMention,
  type DocumentSummary,
} from "@/lib/api";
import { fmtDate, fmtNum, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ClaimRow } from "@/pages/claims";
import { typeLabel } from "@/pages/hypotheses";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip } from "@/components/ui/tooltip";

const RELEVANCE_TIP = "Fades as evidence ages. Ownership and role links do not fade.";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : "Request failed.";
}

function statusLabel(status: string): string {
  if (status === "extracted") return "Extracted";
  if (status === "failed") return "Failed";
  if (status === "duplicate") return "Duplicate";
  return "Pending";
}

function Dot() {
  return <span className="text-muted-foreground/50">·</span>;
}

// ─── Header ──────────────────────────────────────────────────

function DocumentHeader({ doc }: { doc: DocumentDetailRow }) {
  const root = doc.lineage.root;
  return (
    <header className="space-y-1.5">
      <h1 className="text-base font-medium tracking-tight">{doc.title}</h1>

      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
        <Link
          to={`/documents?source=${doc.source.source_id}`}
          className="text-foreground hover:text-accent hover:underline"
        >
          {doc.source.name}
        </Link>
        <Dot />
        <span>{doc.type}</span>
        {doc.published_at && (
          <>
            <Dot />
            <span>{fmtDate(doc.published_at)}</span>
          </>
        )}
        <Dot />
        <span>Fetched {timeAgo(doc.fetched_at)}</span>
        {doc.url && (
          <>
            <Dot />
            <a
              href={doc.url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-foreground hover:text-accent hover:underline"
            >
              <ExternalLink className="size-3.5" />
              Open original
            </a>
          </>
        )}
        {doc.status !== "extracted" && (
          <Badge tone={doc.status === "failed" ? "fail" : "idle"}>
            {statusLabel(doc.status)}
          </Badge>
        )}
        {doc.thin && <Badge tone="outline">Headline only</Badge>}
      </div>

      {root && (
        <p className="text-xs text-muted-foreground">
          Copy of{" "}
          <Link
            to={`/document/${root.event_id}`}
            className="text-foreground hover:text-accent hover:underline"
          >
            {root.title}
          </Link>
          .
        </p>
      )}
    </header>
  );
}

// ─── Summary ─────────────────────────────────────────────────

function SummaryCard({
  doc,
  summary,
  refresh,
}: {
  doc: DocumentDetailRow;
  summary: DocumentSummary;
  refresh: () => void;
}) {
  const ask = useMutation({
    mutationFn: () => requestSummary(doc.event_id),
    onSuccess: () => {
      toast.success("Requested. The worker writes it shortly.");
      refresh();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const isCopy = doc.status === "duplicate";
  const root = doc.lineage.root;
  const status = summary.status;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Summary</CardTitle>
        {!isCopy && status === "done" && (
          <Button
            variant="ghost"
            size="sm"
            disabled={ask.isPending}
            onClick={() => ask.mutate()}
          >
            Summarize again
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-2">
        {isCopy ? (
          <p className="text-muted-foreground">
            {root ? (
              <>
                Copy of{" "}
                <Link
                  to={`/document/${root.event_id}`}
                  className="text-foreground hover:text-accent hover:underline"
                >
                  {root.title}
                </Link>
                .{" "}
              </>
            ) : null}
            The summary is on the original.
          </p>
        ) : (
          <>
            {summary.summary && (
              <p className="leading-relaxed">{summary.summary}</p>
            )}

            {summary.key_points.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-muted-foreground">Key points</h3>
                <ul className="mt-1 space-y-1">
                  {summary.key_points.map((point, i) => (
                    <li key={i} className="flex gap-2 leading-relaxed">
                      <span className="text-muted-foreground/50">·</span>
                      <span>{point}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {summary.summary && summary.model && (
              <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
                <span className="font-mono">{summary.model}</span>
                {status === "done" && summary.updated_at && (
                  <>
                    <Dot />
                    <span>{fmtDate(summary.updated_at)}</span>
                  </>
                )}
              </p>
            )}

            {(status === "none" || status === "pending") && (
              <>
                <p className="text-muted-foreground">Not summarized yet.</p>
                <Button size="sm" disabled={ask.isPending} onClick={() => ask.mutate()}>
                  Summarize
                </Button>
              </>
            )}

            {status === "requested" && (
              <p className="flex items-center gap-2 text-muted-foreground">
                <Spinner className="size-3.5" />
                Requested. The worker writes it shortly.
              </p>
            )}

            {status === "failed" && (
              <>
                <p className="text-muted-foreground">Could not summarize.</p>
                {summary.error && (
                  <p className="truncate text-xs text-muted-foreground">{summary.error}</p>
                )}
                <Button size="sm" disabled={ask.isPending} onClick={() => ask.mutate()}>
                  Retry
                </Button>
              </>
            )}

            {status === "skipped" && (
              <p className="text-muted-foreground">Too short to summarize.</p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Claims ──────────────────────────────────────────────────

function ClaimsCard({
  doc,
  claims,
  refresh,
}: {
  doc: DocumentDetailRow;
  claims: Claim[];
  refresh: () => void;
}) {
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: () =>
      apiFetch<{ ok: boolean }>(`/api/events/${doc.event_id}/retry`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Queued for another attempt.");
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Claims</CardTitle>
        <span className="font-mono text-xs text-muted-foreground">
          {fmtNum(claims.length)}
        </span>
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {claims.length > 0 ? (
          claims.map((claim) => (
            <ClaimRow
              key={claim.claim_id}
              claim={claim}
              showSource={false}
              className="px-4"
            />
          ))
        ) : doc.status === "failed" ? (
          <div className="space-y-2 px-4 pb-2">
            <p className="text-muted-foreground">Extraction failed.</p>
            {doc.last_error && (
              <p className="truncate text-xs text-fail/90">{doc.last_error}</p>
            )}
            <Button size="sm" disabled={retry.isPending} onClick={() => retry.mutate()}>
              Retry
            </Button>
          </div>
        ) : (
          <p className="px-4 pb-2 text-muted-foreground">
            {doc.status === "pending" || doc.status === "extracting"
              ? "Not extracted yet."
              : "No claims extracted."}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Full text ───────────────────────────────────────────────

function TextCard({ eventId }: { eventId: string }) {
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ["document-text", eventId],
    queryFn: () => getDocumentText(eventId),
    enabled: open,
    staleTime: Infinity,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Full text</CardTitle>
        <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)}>
          {open ? "Hide full text" : "Show full text"}
        </Button>
      </CardHeader>
      {open && (
        <CardContent className="space-y-2">
          {query.isPending && (
            <div className="flex justify-center py-6">
              <Spinner />
            </div>
          )}
          {query.isError && (
            <p className="text-muted-foreground">{errorDetail(query.error)}</p>
          )}
          {query.data && (
            <>
              <div className="max-h-[32rem] overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-background px-3 py-2 text-[13px] leading-relaxed text-foreground/90">
                {query.data.body}
              </div>
              <p className="text-xs text-muted-foreground">
                {fmtNum(query.data.chars)} characters
                {query.data.truncated && " · Cut at 200,000 characters."}
              </p>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}

// ─── Companies ───────────────────────────────────────────────

function CompaniesCard({
  entities,
  mentions,
}: {
  entities: DocumentEntity[];
  mentions: DocumentMention[];
}) {
  const unresolved = mentions
    .filter((m) => m.state === "unresolved" || m.state === "pending")
    .map((m) => m.surface);
  const queued = mentions.filter((m) => m.state === "queued").map((m) => m.surface);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Companies</CardTitle>
        {entities.length > 0 && (
          <span className="font-mono text-xs text-muted-foreground">
            {fmtNum(entities.length)}
          </span>
        )}
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {entities.length === 0 ? (
          <p className="px-4 pb-2 text-muted-foreground">No companies resolved yet.</p>
        ) : (
          <ul>
            {entities.map((entity) => (
              <li
                key={entity.entity_id}
                className="border-b border-border px-4 py-2 last:border-0"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <Link
                    to={`/entity/${entity.entity_id}`}
                    className="min-w-0 truncate font-medium text-foreground hover:text-accent hover:underline"
                  >
                    {entity.name}
                  </Link>
                  {entity.ticker && (
                    <Badge tone="outline" className="font-mono">
                      {entity.ticker}
                    </Badge>
                  )}
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {fmtNum(entity.claims)} {entity.claims === 1 ? "claim" : "claims"} ·{" "}
                  {fmtNum(entity.mentions)}{" "}
                  {entity.mentions === 1 ? "mention" : "mentions"}
                </p>
              </li>
            ))}
          </ul>
        )}

        {(unresolved.length > 0 || queued.length > 0) && (
          <div className="space-y-0.5 border-t border-border px-4 pb-1 pt-2 text-xs text-muted-foreground">
            {unresolved.length > 0 && <p>Not resolved: {unresolved.join(", ")}</p>}
            {queued.length > 0 && (
              <p className="flex flex-wrap items-center gap-x-1.5">
                <span>Awaiting review: {queued.join(", ")}</span>
                <Dot />
                <Link to="/review" className="text-foreground hover:text-accent hover:underline">
                  Review
                </Link>
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Links ───────────────────────────────────────────────────

function LinksCard({ edges }: { edges: DocumentEdge[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Links</CardTitle>
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {edges.length === 0 ? (
          <p className="px-4 pb-2 text-muted-foreground">No links yet.</p>
        ) : (
          <ul>
            {edges.map((edge) => (
              <li
                key={edge.edge_id}
                className={cn(
                  "border-b border-border px-4 py-2 last:border-0",
                  edge.archived && "opacity-60",
                )}
              >
                <p className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
                  <Link
                    to={`/entity/${edge.src.entity_id}`}
                    className="font-medium text-foreground hover:text-accent hover:underline"
                  >
                    {edge.src.name}
                  </Link>
                  <span className="font-mono text-xs text-muted-foreground">
                    {edge.predicate}
                  </span>
                  <Link
                    to={`/entity/${edge.dst.entity_id}`}
                    className="font-medium text-foreground hover:text-accent hover:underline"
                  >
                    {edge.dst.name}
                  </Link>
                  {edge.origin === "inferred" && <Badge tone="outline">Inferred</Badge>}
                </p>
                <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
                  <span>
                    {fmtNum(edge.claims_here)} of {fmtNum(edge.claims_total)} claims
                  </span>
                  <Dot />
                  <Tooltip label={RELEVANCE_TIP}>
                    <span className="font-mono">
                      {Math.round(edge.relevance * 100)}%
                    </span>
                  </Tooltip>
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Details ─────────────────────────────────────────────────

const factLabels: [keyof DocumentFacts, string][] = [
  ["ticker", "Ticker"],
  ["form", "Form"],
  ["items", "Items"],
  ["accession", "Accession"],
  ["feed", "Feed"],
  ["channel", "Channel"],
  ["username", "Account"],
  ["filename", "File"],
  ["site", "Site"],
  ["bridge", "Bridge"],
];

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-border py-1.5 last:border-0">
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}

function DetailsCard({
  doc,
  alerts,
  hypotheses,
  contradictions,
}: {
  doc: DocumentDetailRow;
  alerts: DocumentAlert[];
  hypotheses: DocumentHypothesis[];
  contradictions: DocumentContradiction[];
}) {
  const copies = doc.lineage.copies;
  const openConflicts = contradictions.filter((c) => c.status === "open").length;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Details</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="text-[13px]">
          <DetailRow label="Type">{doc.type}</DetailRow>
          <DetailRow label="Source">
            <Link
              to={`/documents?source=${doc.source.source_id}`}
              className="text-foreground hover:text-accent hover:underline"
            >
              {doc.source.name}
            </Link>
          </DetailRow>
          <DetailRow label="Published">
            {doc.published_at ? fmtDate(doc.published_at) : "-"}
          </DetailRow>
          <DetailRow label="Fetched">{fmtDate(doc.fetched_at)}</DetailRow>
          <DetailRow label="Status">{statusLabel(doc.status)}</DetailRow>
          {doc.materiality !== null && (
            <DetailRow label="Materiality">
              <span className="font-mono text-xs">{doc.materiality.toFixed(2)}</span>
            </DetailRow>
          )}

          {factLabels.map(([key, label]) => {
            const value = doc.facts[key];
            if (!value) return null;
            return (
              <DetailRow key={key} label={label}>
                <span className="break-words font-mono text-xs">{value}</span>
              </DetailRow>
            );
          })}

          {copies.length > 0 && (
            <DetailRow label="Copies">
              <span className="font-mono text-xs">{fmtNum(copies.length)}</span>
              <ul className="mt-1 space-y-0.5">
                {copies.map((copy) => (
                  <li key={copy.event_id} className="text-xs text-muted-foreground">
                    <Link
                      to={`/document/${copy.event_id}`}
                      className="text-foreground hover:text-accent hover:underline"
                    >
                      {copy.title}
                    </Link>
                    <br />
                    {copy.source_name} · {fmtDate(copy.fetched_at)}
                  </li>
                ))}
              </ul>
            </DetailRow>
          )}

          {alerts.length > 0 && (
            <DetailRow label="Alerts">
              <ul className="space-y-0.5">
                {alerts.map((alert) => (
                  <li key={alert.alert_id}>
                    <Link
                      to="/alerts"
                      className="text-foreground hover:text-accent hover:underline"
                    >
                      {alert.title}
                    </Link>
                  </li>
                ))}
              </ul>
            </DetailRow>
          )}

          {hypotheses.length > 0 && (
            <DetailRow label="Hypotheses">
              <ul className="space-y-0.5">
                {hypotheses.map((hypothesis) => (
                  <li key={hypothesis.hypothesis_id}>
                    <Link
                      to={`/hypotheses?selected=${hypothesis.hypothesis_id}`}
                      className="text-foreground hover:text-accent hover:underline"
                    >
                      {typeLabel(hypothesis.type)} · {hypothesis.state}
                    </Link>
                  </li>
                ))}
              </ul>
            </DetailRow>
          )}

          {openConflicts > 0 && (
            <DetailRow label="Contradictions">
              <Link to="/review" className="text-foreground hover:text-accent hover:underline">
                {fmtNum(openConflicts)} open
              </Link>
            </DetailRow>
          )}

          {doc.near_duplicate_of && (
            <DetailRow label="Near duplicate of">
              <Link
                to={`/document/${doc.near_duplicate_of.event_id}`}
                className="text-foreground hover:text-accent hover:underline"
              >
                {doc.near_duplicate_of.title}
              </Link>
            </DetailRow>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function DocumentPage() {
  const { id } = useParams<{ id: string }>();
  const queryClient = useQueryClient();
  const queryKey = ["document", id];
  const query = useQuery({
    queryKey,
    queryFn: () => getDocument(id ?? ""),
    enabled: Boolean(id),
    // a requested summary lands within a cycle of the worker's nap loop
    refetchInterval: (q) => (q.state.data?.summary.status === "requested" ? 5_000 : false),
  });
  // the cards invalidate the key this page queries on (the route param),
  // not the canonical id the API echoes back
  const refresh = () => void queryClient.invalidateQueries({ queryKey });

  if (query.isPending) {
    return (
      <div className="flex justify-center py-24">
        <Spinner />
      </div>
    );
  }

  // a failed poll while a summary is pending keeps the cached page: only a
  // query with nothing to show falls back to the empty state
  if (!query.data) {
    // a hand-edited or truncated id fails path validation (422) the same
    // way an unknown one 404s: the analyst sees one sentence either way
    const missing =
      query.error instanceof ApiError && (query.error.status === 404 || query.error.status === 422);
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        <Empty>{missing ? "No such document." : "Could not load this document."}</Empty>
      </div>
    );
  }

  const { summary, claims, mentions, entities, edges, alerts, hypotheses, contradictions } =
    query.data;
  const doc = query.data.document;

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <DocumentHeader doc={doc} />

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-4">
          <SummaryCard doc={doc} summary={summary} refresh={refresh} />
          <ClaimsCard doc={doc} claims={claims} refresh={refresh} />
          <TextCard eventId={doc.event_id} />
        </div>

        <div className="space-y-4">
          <CompaniesCard entities={entities} mentions={mentions} />
          <LinksCard edges={edges} />
          <DetailsCard
            doc={doc}
            alerts={alerts}
            hypotheses={hypotheses}
            contradictions={contradictions}
          />
        </div>
      </div>
    </div>
  );
}
