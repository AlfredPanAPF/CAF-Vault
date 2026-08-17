import { Fragment, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  ApiError,
  getHypotheses,
  getHypothesis,
  reviewHypothesis,
  type Claim,
  type Hypothesis,
} from "@/lib/api";
import { fmtDate } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const stateTabs = [
  { value: "", label: "All" },
  { value: "generated", label: "Generated" },
  { value: "triaged", label: "Triaged" },
  { value: "investigating", label: "Investigating" },
  { value: "promoted", label: "Promoted" },
  { value: "parked", label: "Parked" },
  { value: "refuted", label: "Refuted" },
];

const stateTones: Record<string, BadgeTone> = {
  generated: "neutral",
  triaged: "outline",
  investigating: "warn",
  promoted: "ok",
  parked: "idle",
  refuted: "fail",
};

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : "Request failed.";
}

function typeLabel(type: string): string {
  const plain = type.replace(/_/g, " ");
  return plain.charAt(0).toUpperCase() + plain.slice(1);
}

function StateBadge({ state }: { state: string }) {
  return <Badge tone={stateTones[state] ?? "neutral"}>{state}</Badge>;
}

function Dot() {
  return <span className="text-muted-foreground/50">·</span>;
}

function Subjects({ subjects }: { subjects: Hypothesis["subjects"] }) {
  return (
    <span className="flex flex-wrap items-baseline gap-x-1.5">
      {subjects.map((subject, i) => (
        <Fragment key={subject.entity_id}>
          {i > 0 && <span className="text-muted-foreground/50">·</span>}
          <Link
            to={`/entity/${subject.entity_id}`}
            className="font-medium text-foreground hover:text-accent hover:underline"
          >
            {subject.name}
          </Link>
        </Fragment>
      ))}
    </span>
  );
}

// ─── Detail panel ────────────────────────────────────────────

function EvidenceItem({ claim }: { claim: Claim }) {
  return (
    <li className="rounded-md border border-border px-3 py-2">
      {claim.evidence_quote && (
        <p className="leading-relaxed text-foreground/80">“{claim.evidence_quote}”</p>
      )}
      <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
        {claim.status && claim.status !== "asserted" && (
          <Badge tone="warn">{claim.status}</Badge>
        )}
        {claim.source_name && <span>{claim.source_name}</span>}
        {claim.doc_title && (
          <>
            <Dot />
            <span className="max-w-64 truncate">{claim.doc_title}</span>
          </>
        )}
        <Dot />
        <span>{fmtDate(claim.published_at ?? claim.observed_at)}</span>
      </p>
    </li>
  );
}

function PanelSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      {children}
    </div>
  );
}

function DetailPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["hypothesis", id],
    queryFn: () => getHypothesis(id),
  });

  const review = useMutation({
    mutationFn: (verdict: "accept" | "reject") => reviewHypothesis(id, verdict),
    onSuccess: () => {
      toast.success("Saved.");
      void queryClient.invalidateQueries({ queryKey: ["hypotheses"] });
      void queryClient.invalidateQueries({ queryKey: ["hypothesis", id] });
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const data = query.data;
  const h = data?.hypothesis;
  const confirms = h?.test_plan?.confirm ?? [];
  const refutes = h?.test_plan?.refute ?? [];
  const reasoning = data?.verifier?.["reasoning"];

  return (
    <Card className="lg:sticky lg:top-4">
      <CardHeader>
        <CardTitle>Hypothesis</CardTitle>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {query.isPending && (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        )}

        {query.isError && (
          <p className="text-muted-foreground">
            {query.error instanceof ApiError
              ? query.error.detail
              : "Could not load this hypothesis."}
          </p>
        )}

        {data && h && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <StateBadge state={h.state} />
              <span className="text-xs text-muted-foreground">{typeLabel(h.type)}</span>
              {h.score !== null && (
                <span className="font-mono text-xs text-muted-foreground">
                  Score {h.score.toFixed(2)}
                </span>
              )}
            </div>

            <Subjects subjects={data.subjects} />

            {h.statement?.text && <p className="leading-relaxed">{h.statement.text}</p>}

            {h.rationale && (
              <PanelSection title="Rationale">
                <p className="mt-1 leading-relaxed text-foreground/80">{h.rationale}</p>
              </PanelSection>
            )}

            {confirms.length > 0 && (
              <PanelSection title="Confirms">
                <ul className="mt-1 list-disc space-y-0.5 pl-4 leading-relaxed text-foreground/80">
                  {confirms.map((test, i) => (
                    <li key={i}>{test}</li>
                  ))}
                </ul>
              </PanelSection>
            )}

            {refutes.length > 0 && (
              <PanelSection title="Refutes">
                <ul className="mt-1 list-disc space-y-0.5 pl-4 leading-relaxed text-foreground/80">
                  {refutes.map((test, i) => (
                    <li key={i}>{test}</li>
                  ))}
                </ul>
              </PanelSection>
            )}

            {data.evidence.length > 0 && (
              <PanelSection title="Evidence">
                <ul className="mt-1 space-y-2">
                  {data.evidence.map((claim) => (
                    <EvidenceItem key={claim.claim_id} claim={claim} />
                  ))}
                </ul>
              </PanelSection>
            )}

            {h.state === "promoted" && typeof reasoning === "string" && reasoning && (
              <PanelSection title="Verifier">
                <p className="mt-1 leading-relaxed text-foreground/80">{reasoning}</p>
              </PanelSection>
            )}

            {data.history.length > 0 && (
              <PanelSection title="History">
                <ul className="mt-1 space-y-1">
                  {data.history.map((entry, i) => (
                    <li key={i} className="text-xs leading-relaxed">
                      {entry.at && (
                        <span className="font-mono text-muted-foreground">
                          {fmtDate(entry.at)}{" "}
                        </span>
                      )}
                      {entry.from && entry.to ? (
                        <span>
                          {entry.from} → {entry.to}
                        </span>
                      ) : (
                        entry.to && <span>{entry.to}</span>
                      )}
                      {entry.note && (
                        <span className="text-muted-foreground"> · {entry.note}</span>
                      )}
                    </li>
                  ))}
                </ul>
              </PanelSection>
            )}

            {(h.state === "promoted" || h.state === "triaged" || h.state === "parked") && (
              <div className="flex gap-2 border-t border-border pt-3">
                {h.state === "promoted" && (
                  <Button
                    variant="primary"
                    size="sm"
                    disabled={review.isPending}
                    onClick={() => review.mutate("accept")}
                  >
                    Accept
                  </Button>
                )}
                <Button
                  variant="destructive"
                  size="sm"
                  disabled={review.isPending}
                  onClick={() => review.mutate("reject")}
                >
                  Reject
                </Button>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function HypothesesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const state = searchParams.get("state") ?? "";
  const selected = searchParams.get("selected") ?? "";

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

  const query = useQuery({
    queryKey: ["hypotheses", state],
    queryFn: () => getHypotheses(state),
  });

  const hypotheses = query.data ?? [];

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <div>
        <h1 className="text-base font-medium tracking-tight">Hypotheses</h1>
        <p className="mt-1 text-xs text-muted-foreground">
          Candidates the system generated but has not yet verified.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-1">
        {stateTabs.map((tab) => (
          <button
            key={tab.value}
            type="button"
            onClick={() => setParam("state", tab.value)}
            className={cn(
              "rounded-md px-2.5 py-1 text-[13px] transition-colors",
              state === tab.value
                ? "bg-muted font-medium text-foreground"
                : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
            )}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div
        className={cn(
          "grid items-start gap-4",
          selected && "lg:grid-cols-[minmax(0,1fr)_360px]",
        )}
      >
        <div>
          {query.isPending && (
            <div className="flex justify-center py-24">
              <Spinner />
            </div>
          )}

          {query.isError && <Empty>Could not load hypotheses.</Empty>}

          {!query.isPending && !query.isError && hypotheses.length === 0 && (
            <Empty>{state ? "No hypotheses in this state." : "No hypotheses yet."}</Empty>
          )}

          {hypotheses.length > 0 && (
            <div className="rounded-lg border border-border bg-card">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>Companies</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead className="text-right">Score</TableHead>
                    <TableHead>Rationale</TableHead>
                    <TableHead>Date</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {hypotheses.map((hypothesis) => (
                    <TableRow
                      key={hypothesis.hypothesis_id}
                      tabIndex={0}
                      role="button"
                      aria-pressed={selected === hypothesis.hypothesis_id}
                      className={cn(
                        "cursor-pointer",
                        selected === hypothesis.hypothesis_id && "bg-muted/40",
                      )}
                      onClick={() => setParam("selected", hypothesis.hypothesis_id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setParam("selected", hypothesis.hypothesis_id);
                        }
                      }}
                    >
                      <TableCell className="whitespace-normal align-top">
                        <Subjects subjects={hypothesis.subjects} />
                      </TableCell>
                      <TableCell className="align-top text-muted-foreground">
                        {typeLabel(hypothesis.type)}
                      </TableCell>
                      <TableCell className="align-top">
                        <StateBadge state={hypothesis.state} />
                      </TableCell>
                      <TableCell className="text-right align-top font-mono text-xs">
                        {hypothesis.score !== null ? hypothesis.score.toFixed(2) : ""}
                      </TableCell>
                      <TableCell className="max-w-xl whitespace-normal align-top leading-relaxed text-foreground/80">
                        {hypothesis.rationale}
                      </TableCell>
                      <TableCell className="align-top font-mono text-xs text-muted-foreground">
                        {fmtDate(hypothesis.created_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>

        {selected && (
          <DetailPanel id={selected} onClose={() => setParam("selected", "")} />
        )}
      </div>
    </div>
  );
}
