import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  ApiError,
  decideErQueue,
  getContradictions,
  getErQueue,
  resolveContradiction,
  type Claim,
  type Contradiction,
  type ErDecideBody,
  type ErQueueItem,
} from "@/lib/api";
import { fmtDate, fmtNum, timeAgo } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : "Request failed.";
}

function Dot() {
  return <span className="text-muted-foreground/50">·</span>;
}

// ─── Company matches (ER queue) ──────────────────────────────

function ErItem({ item }: { item: ErQueueItem }) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);

  const decide = useMutation({
    mutationFn: (body: ErDecideBody) => decideErQueue(item.mention_id, body),
    onSuccess: () => {
      toast.success("Saved.");
      void queryClient.invalidateQueries({ queryKey: ["er-queue"] });
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const match = () => {
    const candidate = selected === null ? undefined : item.candidates[selected];
    if (!candidate) return;
    const body: ErDecideBody = { decision: "match" };
    if (candidate.cik !== null && candidate.cik !== undefined) body.cik = candidate.cik;
    else if (candidate.lei) body.lei = candidate.lei;
    decide.mutate(body);
  };

  return (
    <li className="border-b border-border px-4 py-3 last:border-0">
      <span className="font-mono font-medium">{item.surface}</span>

      {item.context && (
        <p className="mt-1 leading-relaxed text-foreground/80">…{item.context.trim()}…</p>
      )}

      <p className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
        {item.source_name && <span>{item.source_name}</span>}
        {item.doc_title && (
          <>
            <Dot />
            <span className="max-w-72 truncate">{item.doc_title}</span>
          </>
        )}
        <Dot />
        <span>{timeAgo(item.created_at)}</span>
      </p>

      {item.candidates.length > 0 && (
        <ul className="mt-2 space-y-1">
          {item.candidates.map((candidate, i) => (
            <li key={i}>
              <label className="flex cursor-pointer items-baseline gap-2 rounded-md border border-border px-2.5 py-1.5 transition-colors hover:bg-muted/40 has-[:checked]:border-accent/40">
                <input
                  type="radio"
                  name={`candidate-${item.mention_id}`}
                  checked={selected === i}
                  onChange={() => setSelected(i)}
                  className="translate-y-px accent-accent"
                />
                <span className="min-w-0 truncate font-medium">{candidate.title}</span>
                {(candidate.ticker ?? candidate.lei) && (
                  <span className="shrink-0 font-mono text-xs text-muted-foreground">
                    {candidate.ticker ?? candidate.lei}
                  </span>
                )}
                {candidate.country && (
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {candidate.country}
                  </span>
                )}
              </label>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          variant="primary"
          size="sm"
          disabled={selected === null || decide.isPending}
          onClick={match}
        >
          Match
        </Button>
        <Button
          size="sm"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ decision: "new_entity", name: item.surface })}
        >
          New company
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ decision: "not_a_company" })}
        >
          Not a company
        </Button>
      </div>
    </li>
  );
}

function CompanyMatches() {
  const query = useQuery({
    queryKey: ["er-queue"],
    queryFn: () => getErQueue(),
  });

  const items = query.data?.items ?? [];
  const pending = query.data?.pending ?? 0;

  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2">
        <h2 className="text-sm font-medium tracking-tight">Company matches</h2>
        {pending > 0 && (
          <span className="text-xs text-muted-foreground">{fmtNum(pending)} pending</span>
        )}
      </div>

      {query.isPending && (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      )}

      {query.isError && <Empty>Could not load company matches.</Empty>}

      {!query.isPending && !query.isError && items.length === 0 && (
        <Empty>Nothing waiting for review.</Empty>
      )}

      {items.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <ul>
              {items.map((item) => (
                <ErItem key={item.mention_id} item={item} />
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </section>
  );
}

// ─── Conflicting claims ──────────────────────────────────────

function ConflictClaim({ claim, label }: { claim: Claim | null; label: string }) {
  if (!claim) {
    return (
      <div className="rounded-md border border-border px-3 py-2 text-xs text-muted-foreground">
        Claim unavailable.
      </div>
    );
  }
  return (
    <div className="rounded-md border border-border px-3 py-2">
      <p className="flex items-center gap-x-1.5 text-xs font-medium text-muted-foreground">
        {label}
        {claim.status && claim.status !== "asserted" && (
          <Badge tone="warn">{claim.status}</Badge>
        )}
      </p>
      {claim.evidence_quote && (
        <p className="mt-0.5 leading-relaxed text-foreground/80">“{claim.evidence_quote}”</p>
      )}
      <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
        {claim.source_name && <span>{claim.source_name}</span>}
        {claim.doc_title && (
          <>
            <Dot />
            <span className="max-w-72 truncate">{claim.doc_title}</span>
          </>
        )}
        <Dot />
        <span>{fmtDate(claim.published_at ?? claim.observed_at)}</span>
      </p>
    </div>
  );
}

function ContradictionItem({ row }: { row: Contradiction }) {
  const queryClient = useQueryClient();

  const resolve = useMutation({
    mutationFn: (keep: "a" | "b" | "none") => resolveContradiction(row.contradiction_id, keep),
    onSuccess: () => {
      toast.success("Saved.");
      void queryClient.invalidateQueries({ queryKey: ["contradictions"] });
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  return (
    <li className="border-b border-border px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <Link
          to={`/entity/${row.subject.entity_id}`}
          className="font-medium text-foreground hover:text-accent hover:underline"
        >
          {row.subject.name}
        </Link>
        <span className="font-mono text-xs text-muted-foreground">{row.predicate_canon}</span>
      </div>

      <div className="mt-2 space-y-2">
        <ConflictClaim claim={row.claims.a} label="First" />
        <ConflictClaim claim={row.claims.b} label="Second" />
      </div>

      <div className="mt-2 flex flex-wrap gap-2">
        <Button size="sm" disabled={resolve.isPending} onClick={() => resolve.mutate("a")}>
          Keep first
        </Button>
        <Button size="sm" disabled={resolve.isPending} onClick={() => resolve.mutate("b")}>
          Keep second
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={resolve.isPending}
          onClick={() => resolve.mutate("none")}
        >
          Dismiss
        </Button>
      </div>
    </li>
  );
}

function ConflictingClaims() {
  const query = useQuery({
    queryKey: ["contradictions"],
    queryFn: () => getContradictions(),
  });

  const rows = query.data ?? [];

  return (
    <section className="space-y-2">
      <div className="flex items-baseline gap-2">
        <h2 className="text-sm font-medium tracking-tight">Conflicting claims</h2>
        {rows.length > 0 && (
          <span className="text-xs text-muted-foreground">{fmtNum(rows.length)} open</span>
        )}
      </div>

      {query.isPending && (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      )}

      {query.isError && <Empty>Could not load conflicts.</Empty>}

      {!query.isPending && !query.isError && rows.length === 0 && (
        <Empty>No open conflicts.</Empty>
      )}

      {rows.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <ul>
              {rows.map((row) => (
                <ContradictionItem key={row.contradiction_id} row={row} />
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </section>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function ReviewPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-6 px-6 py-6">
      <h1 className="text-base font-medium tracking-tight">Review</h1>
      <CompanyMatches />
      <ConflictingClaims />
    </div>
  );
}
