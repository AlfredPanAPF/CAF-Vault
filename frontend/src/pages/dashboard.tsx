import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  apiFetch,
  ApiError,
  type FailedEvent,
  type Heartbeat,
  type Seat,
  type SourceRow,
  type StageRun,
  type StatusResponse,
} from "@/lib/api";
import { fmtDuration, fmtNum, timeAgo, timeUntil } from "@/lib/format";
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

const connectorLabels: Record<string, string> = {
  edgar: "EDGAR",
  podcast: "Podcast",
  rss: "News feed",
  manual: "Upload",
};

function connectorLabel(connector: string): string {
  return connectorLabels[connector] ?? connector;
}

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : "Request failed.";
}

// ─── Worker card ─────────────────────────────────────────────

function WorkerCard({ heartbeat }: { heartbeat: Heartbeat }) {
  const queryClient = useQueryClient();
  const runNow = useMutation({
    mutationFn: () => apiFetch<{ ok: boolean }>("/api/run-now", { method: "POST" }),
    onSuccess: () => {
      toast.success("Requested. The worker picks it up within 15 seconds.");
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const nextRun = new Date(
    new Date(heartbeat.last_cycle_at).getTime() + heartbeat.interval_s * 1000,
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>Worker</CardTitle>
        <div className="flex items-center gap-2">
          {heartbeat.run_requested && (
            <span className="text-xs text-muted-foreground">Requested</span>
          )}
          <Button
            variant="primary"
            size="sm"
            disabled={runNow.isPending || heartbeat.run_requested}
            onClick={() => runNow.mutate()}
          >
            Run now
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-muted-foreground">Last cycle</span>
          <span className="font-mono text-xs">{timeAgo(heartbeat.last_cycle_at)}</span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-muted-foreground">Next run</span>
          <span className="font-mono text-xs">{timeUntil(nextRun)}</span>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-muted-foreground">Interval</span>
          <span className="font-mono text-xs">{Math.round(heartbeat.interval_s / 60)} min</span>
        </div>
      </CardContent>
    </Card>
  );
}

// ─── Claude seats card ───────────────────────────────────────

function seatState(seat: Seat): { label: string; tone: BadgeTone } {
  if (!seat.has_token) return { label: "signed out", tone: "idle" };
  if (seat.latched) {
    if (seat.kind === "auth") return { label: "signed out", tone: "fail" };
    return { label: "limit reached", tone: "warn" };
  }
  return { label: "active", tone: "ok" };
}

function SeatsCard({ seats }: { seats: Seat[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Claude seats</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {seats.length === 0 && (
          <p className="text-muted-foreground">No seats reported.</p>
        )}
        {seats.map((seat) => {
          const state = seatState(seat);
          return (
            <div key={seat.seat}>
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs">Seat {seat.seat}</span>
                <Badge tone={state.tone}>{state.label}</Badge>
              </div>
              {seat.latched && (seat.reason || seat.latched_at) && (
                <p className="mt-0.5 truncate text-xs text-muted-foreground">
                  {seat.latched_at && <span>since {timeAgo(seat.latched_at)}</span>}
                  {seat.latched_at && seat.reason && <span> · </span>}
                  {seat.reason}
                </p>
              )}
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

// ─── Stages card ─────────────────────────────────────────────

function summaryLine(summary: Record<string, unknown> | null): string {
  if (!summary) return "";
  return Object.entries(summary)
    .map(([key, value]) => {
      const text =
        value !== null && typeof value === "object" ? JSON.stringify(value) : String(value);
      return `${key}: ${text}`;
    })
    .join(" · ");
}

function StagesCard({ stages }: { stages: StageRun[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Last cycle</CardTitle>
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {stages.length === 0 && (
          <p className="px-4 pb-2 text-muted-foreground">No stages recorded.</p>
        )}
        <ul>
          {stages.map((stage) => (
            <li
              key={stage.stage}
              className={cn("px-4 py-1.5", stage.error && "bg-fail/5")}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className={cn("font-mono text-xs", stage.error ? "text-fail" : "text-foreground")}>
                  {stage.stage}
                </span>
                <span className="font-mono text-xs text-muted-foreground">
                  {stage.finished_at ? fmtDuration(stage.started_at, stage.finished_at) : "running"}
                </span>
              </div>
              {stage.error ? (
                <p className="truncate text-xs text-fail">{stage.error}</p>
              ) : (
                summaryLine(stage.summary) && (
                  <p className="truncate text-xs text-muted-foreground">
                    {summaryLine(stage.summary)}
                  </p>
                )
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

// ─── Counters ────────────────────────────────────────────────

function Counter({
  label,
  value,
  sub,
  to,
}: {
  label: string;
  value: number;
  sub?: string;
  to?: string;
}) {
  const body = (
    <Card className={cn("h-full", to && "transition-colors hover:border-idle/70")}>
      <CardContent className="px-4 py-3">
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="mt-1 font-mono text-2xl leading-none text-foreground">{fmtNum(value)}</p>
        {sub && <p className="mt-1.5 truncate text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  );
  return to ? (
    <Link to={to} className="block h-full">
      {body}
    </Link>
  ) : (
    body
  );
}

// ─── Sources table ───────────────────────────────────────────

function sourceStatus(status: string): { label: string; tone: BadgeTone } {
  if (status === "active") return { label: "active", tone: "ok" };
  if (status === "demoted") return { label: "paused", tone: "idle" };
  if (status === "error" || status === "failed") return { label: status, tone: "fail" };
  return { label: status, tone: "neutral" };
}

function SourcesCard({ sources }: { sources: SourceRow[] }) {
  const navigate = useNavigate();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Sources</CardTitle>
      </CardHeader>
      <CardContent className="p-0 pb-1">
        {sources.length === 0 ? (
          <p className="px-4 pb-3 text-muted-foreground">No sources configured.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Last polled</TableHead>
                <TableHead className="text-right">Docs</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sources.map((source) => {
                const status = sourceStatus(source.status);
                return (
                  <TableRow
                    key={source.source_id}
                    className="cursor-pointer"
                    onClick={() => navigate("/sources")}
                  >
                    <TableCell className="max-w-48 truncate font-medium">
                      {source.name}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {connectorLabel(source.connector)}
                    </TableCell>
                    <TableCell>
                      <Badge tone={status.tone}>{status.label}</Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {source.last_polled ? timeAgo(source.last_polled) : "Never"}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtNum(source.events)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Failed items ────────────────────────────────────────────

function FailedCard({ failed }: { failed: FailedEvent[] }) {
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: (eventId: string) =>
      apiFetch<{ ok: boolean }>(`/api/events/${eventId}/retry`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Queued for another attempt.");
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Failed items</CardTitle>
      </CardHeader>
      <CardContent className="p-0 pb-2">
        {failed.length === 0 ? (
          <p className="px-4 pb-2 text-muted-foreground">Nothing has failed.</p>
        ) : (
          <ul>
            {failed.map((event) => (
              <li
                key={event.event_id}
                className="flex items-center gap-3 border-b border-border px-4 py-2 last:border-0"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="truncate">{event.title ?? event.event_id}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {connectorLabel(event.connector)}
                    </span>
                  </div>
                  {event.last_error && (
                    <p className="truncate text-xs text-fail/90">{event.last_error}</p>
                  )}
                </div>
                <Button
                  size="sm"
                  disabled={retry.isPending}
                  onClick={() => retry.mutate(event.event_id)}
                >
                  Retry
                </Button>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function DashboardPage() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["status"],
    queryFn: () => apiFetch<StatusResponse>("/api/status"),
    refetchInterval: 30_000,
  });

  if (isPending) {
    return (
      <div className="flex justify-center py-24">
        <Spinner />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="mx-auto max-w-6xl px-6 py-6">
        <Empty>Could not load status.</Empty>
      </div>
    );
  }

  const { heartbeat, stages, counts, sources, failed_events } = data;
  const documents =
    counts.events.pending + counts.events.extracting + counts.events.extracted + counts.events.failed;
  const links = counts.edges.asserted + counts.edges.inferred;
  const hypotheses = Object.values(counts.hypotheses).reduce((sum, n) => sum + n, 0);

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <h1 className="text-base font-medium tracking-tight">Dashboard</h1>

      {/* Row 1: worker */}
      {heartbeat === null ? (
        <Card>
          <CardContent className="px-4 py-8 text-center text-muted-foreground">
            No cycles recorded yet. The worker runs every 15 minutes.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-3">
          <WorkerCard heartbeat={heartbeat} />
          <SeatsCard seats={heartbeat.seats} />
          <StagesCard stages={stages} />
        </div>
      )}

      {/* Row 2: counters */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <Counter
          label="Documents"
          value={documents}
          sub={`${fmtNum(counts.events.extracted)} extracted · ${fmtNum(
            counts.events.pending + counts.events.extracting,
          )} pending`}
        />
        <Counter
          label="Claims"
          value={counts.claims}
          sub={`+${fmtNum(counts.claims_7d)} this week`}
          to="/claims"
        />
        <Counter label="Companies" value={counts.entities} to="/entities" />
        <Counter
          label="Links"
          value={links}
          sub={`${fmtNum(counts.edges.asserted)} asserted · ${fmtNum(counts.edges.inferred)} inferred`}
        />
        <Counter label="Awaiting review" value={counts.er_queue.pending} />
        <Counter label="Hypotheses" value={hypotheses} to="/hypotheses" />
      </div>

      {/* Row 3: sources + failed */}
      <div className="grid gap-4 lg:grid-cols-2">
        <SourcesCard sources={sources} />
        <FailedCard failed={failed_events} />
      </div>
    </div>
  );
}
