import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import {
  ApiError,
  getAlerts,
  getDigest,
  markAlertRead,
  markAllAlertsRead,
  type Alert,
  type DigestHypothesis,
  type DigestResponse,
} from "@/lib/api";
import { fmtNum, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty } from "@/components/ui/empty";
import { Spinner } from "@/components/ui/spinner";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : "Request failed.";
}

// ─── Alert kinds ─────────────────────────────────────────────

const kindBadges: Record<string, { label: string; tone: BadgeTone }> = {
  material_event: { label: "Material event", tone: "warn" },
  promoted_link: { label: "New link", tone: "ok" },
  hypothesis_wake: { label: "Woke", tone: "idle" },
  contradiction: { label: "Conflict", tone: "fail" },
};

function kindBadge(kind: string): { label: string; tone: BadgeTone } {
  return kindBadges[kind] ?? { label: kind.replace(/_/g, " "), tone: "neutral" };
}

/** Where a row click lands (spec §4.4): material events open the claims feed
 *  filtered to the company, hypothesis alerts open the hypothesis detail,
 *  conflicts open the review page. */
function alertTarget(alert: Alert): string | null {
  if (alert.kind === "material_event") {
    const entity = alert.entities[0];
    return entity ? `/claims?entity=${entity.entity_id}` : null;
  }
  if (
    (alert.kind === "promoted_link" || alert.kind === "hypothesis_wake") &&
    alert.hypothesis_id
  ) {
    return `/hypotheses?selected=${alert.hypothesis_id}`;
  }
  if (alert.kind === "contradiction") return "/review";
  return null;
}

// ─── Morning digest ──────────────────────────────────────────

const docWords: Record<string, [string, string]> = {
  edgar: ["EDGAR filing", "EDGAR filings"],
  podcast: ["podcast", "podcasts"],
  rss: ["news item", "news items"],
  manual: ["upload", "uploads"],
};

function plural(n: number, one: string, many: string): string {
  return `${fmtNum(n)} ${n === 1 ? one : many}`;
}

function countsLine(digest: DigestResponse): string {
  const parts = [plural(digest.claims, "new claim", "new claims")];
  for (const [connector, n] of Object.entries(digest.events)) {
    if (n === 0) continue;
    const [one, many] = docWords[connector] ?? [connector, connector];
    parts.push(plural(n, one, many));
  }
  if (digest.contradictions_open > 0) {
    parts.push(plural(digest.contradictions_open, "open conflict", "open conflicts"));
  }
  if (digest.failed_events > 0) {
    parts.push(plural(digest.failed_events, "failed item", "failed items"));
  }
  return parts.join(" · ");
}

function HypothesisList({ items }: { items: DigestHypothesis[] }) {
  return (
    <ul className="mt-1 space-y-0.5">
      {items.map((item, i) => (
        <li key={item.hypothesis_id ?? i}>
          {item.hypothesis_id ? (
            <Link
              to={`/hypotheses?selected=${item.hypothesis_id}`}
              className="text-foreground hover:text-accent hover:underline"
            >
              {item.title}
            </Link>
          ) : (
            <span className="text-foreground">{item.title}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function DigestCard() {
  const query = useQuery({
    queryKey: ["digest"],
    queryFn: () => getDigest(),
    staleTime: 60_000,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Morning digest</CardTitle>
        <span className="text-xs text-muted-foreground">Last 24 hours</span>
      </CardHeader>
      <CardContent className="space-y-3">
        {query.isPending && (
          <div className="flex justify-center py-4">
            <Spinner />
          </div>
        )}
        {query.isError && (
          <p className="text-muted-foreground">Could not load the digest.</p>
        )}
        {query.data && (
          <>
            <p>{countsLine(query.data)}</p>

            {query.data.top_entities.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-muted-foreground">Top companies</h3>
                <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
                  {query.data.top_entities.map((entity) => (
                    <Link
                      key={entity.entity_id}
                      to={`/entity/${entity.entity_id}`}
                      className="font-medium text-foreground hover:text-accent hover:underline"
                    >
                      {entity.name}{" "}
                      <span className="font-mono text-xs font-normal text-muted-foreground">
                        {fmtNum(entity.claims)}
                      </span>
                    </Link>
                  ))}
                </p>
              </div>
            )}

            {query.data.promoted.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-muted-foreground">Promoted links</h3>
                <HypothesisList items={query.data.promoted} />
              </div>
            )}

            {query.data.woke.length > 0 && (
              <div>
                <h3 className="text-xs font-medium text-muted-foreground">Woke hypotheses</h3>
                <HypothesisList items={query.data.woke} />
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Alert list ──────────────────────────────────────────────

function AlertRow({ alert, onOpen }: { alert: Alert; onOpen: (alert: Alert) => void }) {
  const badge = kindBadge(alert.kind);
  return (
    <li className="border-b border-border last:border-0">
      <button
        type="button"
        onClick={() => onOpen(alert)}
        className="w-full px-4 py-2.5 text-left transition-colors hover:bg-muted/40"
      >
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "size-1.5 shrink-0 rounded-full",
              alert.read_at ? "bg-transparent" : "bg-accent",
            )}
          />
          <Badge tone={badge.tone}>{badge.label}</Badge>
          <span
            className={cn(
              "min-w-0 truncate",
              alert.read_at ? "text-muted-foreground" : "font-medium text-foreground",
            )}
          >
            {alert.title}
          </span>
          <span className="ml-auto shrink-0 font-mono text-xs text-muted-foreground">
            {timeAgo(alert.created_at)}
          </span>
        </div>
        {alert.body && (
          <p className="mt-0.5 truncate pl-3.5 text-xs text-muted-foreground">{alert.body}</p>
        )}
      </button>
    </li>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function AlertsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["alerts"],
    queryFn: () => getAlerts(),
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["alerts"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  const markRead = useMutation({
    mutationFn: markAlertRead,
    onSuccess: invalidate,
    onError: (err) => toast.error(errorDetail(err)),
  });

  const markAll = useMutation({
    mutationFn: markAllAlertsRead,
    onSuccess: invalidate,
    onError: (err) => toast.error(errorDetail(err)),
  });

  const openAlert = (alert: Alert) => {
    if (!alert.read_at) markRead.mutate(alert.alert_id);
    const target = alertTarget(alert);
    if (target) navigate(target);
  };

  const alerts = query.data?.alerts ?? [];
  const unread = query.data?.unread ?? 0;

  return (
    <div className="mx-auto max-w-5xl space-y-4 px-6 py-6">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-baseline gap-2">
          <h1 className="text-base font-medium tracking-tight">Alerts</h1>
          {unread > 0 && (
            <span className="text-xs text-muted-foreground">{fmtNum(unread)} unread</span>
          )}
        </div>
        <Button
          size="sm"
          disabled={markAll.isPending || unread === 0}
          onClick={() => markAll.mutate()}
        >
          Mark all read
        </Button>
      </div>

      <DigestCard />

      {query.isPending && (
        <div className="flex justify-center py-24">
          <Spinner />
        </div>
      )}

      {query.isError && <Empty>Could not load alerts.</Empty>}

      {!query.isPending && !query.isError && alerts.length === 0 && (
        <Empty>No alerts yet. Material filings and new links appear here.</Empty>
      )}

      {alerts.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <ul>
              {alerts.map((alert) => (
                <AlertRow key={alert.alert_id} alert={alert} onOpen={openAlert} />
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
