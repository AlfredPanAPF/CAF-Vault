import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { apiFetch, ApiError, type EdgeRow, type EntityResponse } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ClaimRow } from "@/pages/claims";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip } from "@/components/ui/tooltip";

const RELEVANCE_TIP = "Fades as evidence ages. Ownership and role links do not fade.";

// ─── Registry chips ──────────────────────────────────────────

const registryKeys = [
  { key: "ticker", label: "" },
  { key: "lei", label: "LEI " },
  { key: "country", label: "" },
] as const;

function RegistryChips({ refs }: { refs: Record<string, unknown> }) {
  const chips = registryKeys
    .map(({ key, label }) => {
      const value = refs[key];
      return typeof value === "string" && value ? `${label}${value}` : null;
    })
    .filter((chip): chip is string => chip !== null);
  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {chips.map((chip) => (
        <Badge key={chip} tone="outline" className="font-mono">
          {chip}
        </Badge>
      ))}
    </div>
  );
}

// ─── Links card ──────────────────────────────────────────────

function EdgeItem({ edge }: { edge: EdgeRow }) {
  return (
    <li
      className={cn(
        "border-b border-border px-4 py-2 last:border-0",
        edge.archived && "opacity-60",
      )}
    >
      <div className="flex items-baseline justify-between gap-2">
        <Link
          to={`/entity/${edge.peer.entity_id}`}
          className="min-w-0 truncate font-medium text-foreground hover:text-accent hover:underline"
        >
          {edge.peer.name}
        </Link>
        <Tooltip label={RELEVANCE_TIP}>
          <span className="font-mono text-xs text-muted-foreground">
            {Math.round(edge.relevance * 100)}%
          </span>
        </Tooltip>
      </div>
      <p className="mt-0.5 flex flex-wrap items-baseline gap-x-1.5 text-xs text-muted-foreground">
        <span className="font-mono">{edge.predicate}</span>
        <span className="text-muted-foreground/50">·</span>
        <span>
          {fmtNum(edge.claims)} {edge.claims === 1 ? "claim" : "claims"}
        </span>
      </p>
    </li>
  );
}

function LinksCard({ edges }: { edges: EntityResponse["edges"] }) {
  const [showArchived, setShowArchived] = useState(false);
  const hasArchived = [...edges.asserted, ...edges.inferred].some((e) => e.archived);
  const asserted = edges.asserted.filter((e) => showArchived || !e.archived);
  const inferred = edges.inferred.filter((e) => showArchived || !e.archived);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Links</CardTitle>
        {hasArchived && (
          <Button variant="ghost" size="sm" onClick={() => setShowArchived((v) => !v)}>
            {showArchived ? "Hide faded links" : "Show faded links"}
          </Button>
        )}
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {asserted.length === 0 ? (
          <p className="px-4 pb-2 text-muted-foreground">None yet.</p>
        ) : (
          <ul>
            {asserted.map((edge) => (
              <EdgeItem key={edge.edge_id} edge={edge} />
            ))}
          </ul>
        )}

        <h3 className="border-t border-border px-4 pb-1 pt-3 text-xs font-medium text-muted-foreground">
          Inferred
        </h3>
        {inferred.length === 0 ? (
          <p className="px-4 pb-2 text-muted-foreground">
            None yet. Links appear here once the system can back them with independent
            sources.
          </p>
        ) : (
          <ul>
            {inferred.map((edge) => (
              <EdgeItem key={edge.edge_id} edge={edge} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function EntityPage() {
  const { id } = useParams<{ id: string }>();
  const query = useQuery({
    queryKey: ["entity", id],
    queryFn: () => apiFetch<EntityResponse>(`/api/entity/${id}`),
    enabled: Boolean(id),
  });

  if (query.isPending) {
    return (
      <div className="flex justify-center py-24">
        <Spinner />
      </div>
    );
  }

  if (query.isError || !query.data) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        <Empty>
          {query.error instanceof ApiError
            ? query.error.detail
            : "Could not load this company."}
        </Empty>
      </div>
    );
  }

  const { entity, aliases, claims, edges } = query.data;

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <header className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-base font-medium tracking-tight">{entity.name}</h1>
          <RegistryChips refs={entity.registry_refs} />
        </div>
        {aliases.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Also known as {aliases.join(", ")}
          </p>
        )}
      </header>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        <Card>
          <CardHeader>
            <CardTitle>Claims</CardTitle>
            <span className="font-mono text-xs text-muted-foreground">
              {fmtNum(claims.length)}
            </span>
          </CardHeader>
          <CardContent className="p-0 pb-2">
            {claims.length === 0 ? (
              <p className="px-4 pb-2 text-muted-foreground">No claims yet.</p>
            ) : (
              claims.map((claim) => <ClaimRow key={claim.claim_id} claim={claim} />)
            )}
          </CardContent>
        </Card>

        <LinksCard edges={edges} />
      </div>
    </div>
  );
}
