import { useCallback, useEffect, useRef, useState } from "react";
import type { ClipboardEvent, FormEvent, KeyboardEvent, MouseEvent, WheelEvent } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { toast } from "sonner";

import {
  addSource,
  apiFetch,
  ApiError,
  cancelSignIn,
  deleteCredential,
  dropSignIn,
  finishSignIn,
  removeSource,
  resolveSource,
  retryLink,
  saveCredential,
  searchTickers,
  signInEvent,
  signInFrame,
  startSignIn,
  submitSignIn,
  testCredential,
  type CredentialRow,
  type LinkRow,
  type Resolution,
  type SignInEventBody,
  type SignInFrame,
  type SignInSession,
  type SourceItem,
  type SourcesResponse,
  type UploadResponse,
  type UploadResult,
  type WatchlistRow,
} from "@/lib/api";
import { fmtNum, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : "Request failed.";
}

/** Short site names for notes and toasts (spec v4 §3.1 sites). */
const siteNames: Record<string, string> = {
  ft: "FT",
  wsj: "WSJ",
  substack: "Substack",
  x: "X",
  youtube: "YouTube",
};

function siteName(site: string | null): string {
  if (!site) return "the site";
  return siteNames[site] ?? site;
}

// ─── Watchlist ───────────────────────────────────────────────

/** Toast after an add: "Added NVIDIA Corporation (Technology, NasdaqGS)." */
function addedLine(res: {
  ticker: string;
  name: string | null;
  sector: string | null;
  exchange: string | null;
}): string {
  const company = res.name ?? res.ticker;
  const facts = [res.sector, res.exchange].filter((v): v is string => Boolean(v));
  return facts.length > 0 ? `Added ${company} (${facts.join(", ")}).` : `Added ${company}.`;
}

function WatchlistCard({ watchlist }: { watchlist: WatchlistRow[] }) {
  const queryClient = useQueryClient();
  const [term, setTerm] = useState("");
  const [typed, setTyped] = useState("");
  const [listOpen, setListOpen] = useState(false);

  // Suggestions lag the input by 300 ms (spec §8).
  useEffect(() => {
    const timer = window.setTimeout(() => setTyped(term.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [term]);

  // Only search once there is a letter and two characters to work with.
  const searchable = typed.length >= 2 && /[a-z]/i.test(typed);

  const search = useQuery({
    queryKey: ["watchlist-search", typed],
    queryFn: () => searchTickers(typed),
    enabled: searchable && listOpen,
    staleTime: 60_000,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  const add = useMutation({
    mutationFn: (ticker: string) =>
      apiFetch<{
        ok: boolean;
        ticker: string;
        name: string | null;
        sector: string | null;
        exchange: string | null;
      }>("/api/watchlist", { method: "POST", body: JSON.stringify({ ticker }) }),
    onSuccess: (res) => {
      toast.success(addedLine(res));
      setTerm("");
      setTyped("");
      setListOpen(false);
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const toggle = useMutation({
    mutationFn: (t: string) =>
      apiFetch<{ ok: boolean; active: boolean }>(`/api/watchlist/${t}/toggle`, {
        method: "POST",
      }),
    onSuccess: (res, t) => {
      toast.success(res.active ? `Resumed ${t}.` : `Paused ${t}.`);
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const t = term.trim();
    if (!t) return;
    setListOpen(false);
    add.mutate(t);
  };

  const suggestions = search.data ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Watchlist</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-0 pb-3">
        {watchlist.length === 0 ? (
          <p className="px-4 text-muted-foreground">No tickers on the watchlist.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Ticker</TableHead>
                <TableHead>Company</TableHead>
                <TableHead>Sector</TableHead>
                <TableHead>Exchange</TableHead>
                <TableHead className="text-right">Docs</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {watchlist.map((row) => (
                <TableRow key={row.ticker}>
                  <TableCell className="font-mono text-xs font-medium">
                    {row.ticker}
                  </TableCell>
                  <TableCell className="max-w-72 truncate">{row.name ?? "-"}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {row.sector ?? "-"}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {row.exchange ?? "-"}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs">
                    {fmtNum(row.events)}
                  </TableCell>
                  <TableCell>
                    <Badge tone={row.active ? "ok" : "idle"}>
                      {row.active ? "active" : "paused"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      disabled={toggle.isPending}
                      onClick={() => toggle.mutate(row.ticker)}
                    >
                      {row.active ? "Pause" : "Resume"}
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}

        <form onSubmit={onSubmit} className="flex flex-wrap items-start gap-2 px-4">
          <div
            className="relative"
            onBlur={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget)) setListOpen(false);
            }}
          >
            <Input
              placeholder="Ticker or company name"
              value={term}
              onChange={(e) => {
                setTerm(e.target.value);
                setListOpen(true);
              }}
              className="w-64"
            />
            {listOpen && searchable && suggestions.length > 0 && (
              <ul className="absolute left-0 top-9 z-20 w-80 overflow-hidden rounded-md border border-border bg-card shadow-lg">
                {suggestions.map((row) => (
                  <li key={row.symbol}>
                    <button
                      type="button"
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => {
                        setTerm(row.symbol);
                        setListOpen(false);
                      }}
                      className="flex w-full items-baseline gap-2 px-2.5 py-1.5 text-left transition-colors hover:bg-muted/60"
                    >
                      <span className="shrink-0 font-mono text-xs font-medium">
                        {row.symbol}
                      </span>
                      <span className="min-w-0 truncate">{row.name ?? "-"}</span>
                      {row.exchange && (
                        <span className="ml-auto shrink-0 text-xs text-muted-foreground">
                          {row.exchange}
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <Button type="submit" disabled={add.isPending || !term.trim()}>
            {add.isPending && <Spinner className="size-3.5" />}
            Add
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// ─── Sources ─────────────────────────────────────────────────

function sourceStatus(status: string): { label: string; tone: BadgeTone } {
  if (status === "active") return { label: "active", tone: "ok" };
  if (status === "paused" || status === "demoted") return { label: "paused", tone: "idle" };
  return { label: status, tone: "neutral" };
}

/** What a one-off link came back as, for toasts (spec §8). */
function linkMessage(link: LinkRow): { text: string; ok: boolean } {
  if (link.status === "done") return { text: "Added.", ok: true };
  if (link.status === "queued") {
    return { text: "Queued. The worker fetches it within 15 minutes.", ok: true };
  }
  if (link.status === "blocked") {
    return { text: `Sign-in needed for ${siteName(link.site)}.`, ok: false };
  }
  if (link.status === "duplicate") return { text: "Already in the vault.", ok: true };
  return { text: link.error ?? "Could not add that link.", ok: false };
}

function SourcesCard({ sources }: { sources: SourceItem[] }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [resolution, setResolution] = useState<Resolution | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const asked = useRef("");
  const confirmTimer = useRef<number | null>(null);
  const resolveTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    if (resolveTimer.current !== null) window.clearTimeout(resolveTimer.current);
  }, []);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  const resolve = useMutation({
    mutationFn: (raw: string) => resolveSource(raw),
    onSuccess: (res, raw) => {
      if (raw !== asked.current) return; // a newer paste won
      setResolution(res);
      setName(res.one_off ? "" : (res.name ?? ""));
    },
    onError: (_err, raw) => {
      if (raw !== asked.current) return;
      asked.current = ""; // let Enter try the same address again
      setResolution(null);
    },
  });

  const clearResolveTimer = () => {
    if (resolveTimer.current !== null) {
      window.clearTimeout(resolveTimer.current);
      resolveTimer.current = null;
    }
  };

  const runResolve = (raw: string) => {
    // paste and Enter go now, so the pending debounce is dropped: it would
    // otherwise fire 600 ms later and repeat several seconds of server fetches
    clearResolveTimer();
    const text = raw.trim();
    if (!text) {
      asked.current = "";
      setResolution(null);
      return;
    }
    if (text === asked.current) return;
    asked.current = text;
    resolve.mutate(text);
  };

  // Resolve 600 ms after typing stops; paste and Enter go straight through.
  useEffect(() => {
    const text = url.trim();
    if (!text) {
      asked.current = "";
      setResolution(null);
      return;
    }
    if (text === asked.current) return;
    resolveTimer.current = window.setTimeout(() => {
      resolveTimer.current = null;
      asked.current = text;
      resolve.mutate(text);
    }, 600);
    return clearResolveTimer;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  const add = useMutation({
    mutationFn: () => addSource(url.trim(), name.trim() || undefined),
    onSuccess: (res) => {
      if (res.kind === "source" && res.source) {
        toast.success(`Added ${res.source.name}.`);
      } else if (res.link) {
        const message = linkMessage(res.link);
        if (message.ok) toast.success(message.text);
        else toast.error(message.text);
      }
      setUrl("");
      setName("");
      setResolution(null);
      asked.current = "";
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const toggle = useMutation({
    mutationFn: (source: SourceItem) =>
      apiFetch<{ ok: boolean; status: string }>(`/api/sources/${source.source_id}/toggle`, {
        method: "POST",
      }),
    onSuccess: (res, source) => {
      toast.success(
        res.status === "active" ? `Resumed ${source.name}.` : `Paused ${source.name}.`,
      );
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const remove = useMutation({
    mutationFn: (source: SourceItem) => removeSource(source.source_id),
    onSuccess: (_res, source) => {
      toast.success(`Removed ${source.name}.`);
      setConfirmId(null);
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  // Two steps instead of a browser dialog: the button asks, then acts.
  const askRemove = (sourceId: string) => {
    if (confirmTimer.current !== null) window.clearTimeout(confirmTimer.current);
    setConfirmId(sourceId);
    confirmTimer.current = window.setTimeout(() => setConfirmId(null), 3000);
  };

  const unsupported = resolution?.kind === "unsupported";
  // the router already wrote the note (router.py NOTES); re-deriving it here
  // drifted from the API, which knows a free Substack needs no sign-in at all
  const note = resolution && !unsupported ? resolution.message : null;
  const credential = resolution?.credential ?? null;
  const needsSignIn = Boolean(credential && !credential.set);
  const showName = Boolean(resolution && !unsupported && !resolution.one_off);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Sources</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-0 pb-3">
        {sources.length === 0 ? (
          <p className="px-4 text-muted-foreground">No sources yet.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Last polled</TableHead>
                <TableHead className="text-right">Docs</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {sources.map((source) => {
                const status = sourceStatus(source.status);
                return (
                  <TableRow key={source.source_id}>
                    <TableCell className="max-w-64 truncate font-medium" title={source.url}>
                      {source.name}
                    </TableCell>
                    <TableCell className="text-muted-foreground">{source.label}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {source.last_polled ? timeAgo(source.last_polled) : "Never"}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      <Link
                        to={`/documents?source=${source.source_id}`}
                        className="hover:text-accent hover:underline"
                      >
                        {fmtNum(source.events)}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <span className="flex items-center gap-1.5">
                        <Badge tone={status.tone}>{status.label}</Badge>
                        {source.last_error && (
                          <Badge
                            tone="warn"
                            className="max-w-48 truncate"
                            title={source.last_error}
                          >
                            {source.last_error}
                          </Badge>
                        )}
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      <span className="flex justify-end gap-1.5">
                        <Button
                          size="sm"
                          disabled={toggle.isPending}
                          onClick={() => toggle.mutate(source)}
                        >
                          {source.status === "active" ? "Pause" : "Resume"}
                        </Button>
                        <Button
                          size="sm"
                          variant={confirmId === source.source_id ? "destructive" : "ghost"}
                          disabled={remove.isPending}
                          onClick={() =>
                            confirmId === source.source_id
                              ? remove.mutate(source)
                              : askRemove(source.source_id)
                          }
                        >
                          {confirmId === source.source_id ? "Remove?" : "Remove"}
                        </Button>
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}

        <div className="space-y-2 px-4">
          <div className="flex flex-wrap items-center gap-2">
            <Input
              placeholder="Paste a link: feed, YouTube, X, Substack, FT, WSJ, article"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onPaste={(e) => {
                const text = e.clipboardData.getData("text").trim();
                if (!text) return;
                e.preventDefault();
                setUrl(text);
                runResolve(text);
              }}
              onKeyDown={(e) => {
                if (e.key !== "Enter") return;
                e.preventDefault();
                runResolve(url);
              }}
              className="w-[26rem] max-w-full"
            />
            {showName && (
              <Input
                placeholder="Name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && url.trim()) add.mutate();
                }}
                className="w-56"
              />
            )}
            <Button
              variant="primary"
              disabled={!url.trim() || unsupported || add.isPending || resolve.isPending}
              onClick={() => add.mutate()}
            >
              {add.isPending && <Spinner className="size-3.5" />}
              Add
            </Button>
          </div>

          {resolve.isPending && (
            <p className="flex items-center gap-1.5 text-muted-foreground">
              <Spinner className="size-3.5" />
              Checking the link.
            </p>
          )}

          {resolution && !resolve.isPending && (
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
              {unsupported ? (
                <span className="text-fail/90">
                  {resolution.message ??
                    "No feed found at this address. Paste an article link to add it once, or a feed URL."}
                </span>
              ) : (
                <>
                  <span>
                    <span className="text-muted-foreground">{resolution.label}:</span>{" "}
                    {resolution.name ?? resolution.url}
                  </span>
                  {note && <span className="text-muted-foreground">{note}</span>}
                  {needsSignIn && (
                    <a href="#sign-ins">
                      <Badge tone="warn">Sign-in needed</Badge>
                    </a>
                  )}
                </>
              )}
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ─── Links ───────────────────────────────────────────────────

const linkKinds: Record<string, string> = {
  article: "Article",
  substack_post: "Substack post",
  youtube_video: "YouTube video",
  x_post: "X post",
};

const linkStatuses: Record<string, { label: string; tone: BadgeTone }> = {
  done: { label: "added", tone: "ok" },
  queued: { label: "queued", tone: "idle" },
  blocked: { label: "sign-in needed", tone: "warn" },
  failed: { label: "failed", tone: "fail" },
  duplicate: { label: "already in the vault", tone: "idle" },
};

function LinksCard({ links }: { links: LinkRow[] }) {
  const queryClient = useQueryClient();

  const retry = useMutation({
    mutationFn: (link: LinkRow) => retryLink(link.link_id),
    onSuccess: (row) => {
      const message = linkMessage(row);
      if (message.ok) toast.success(message.text);
      else toast.error(message.text);
      void queryClient.invalidateQueries({ queryKey: ["sources"] });
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Links</CardTitle>
      </CardHeader>
      <CardContent className="p-0 pb-3">
        {links.length === 0 ? (
          <p className="px-4 text-muted-foreground">
            Paste an article, video, or post link above to add it once.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Item</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Added</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {links.map((link) => {
                const status = linkStatuses[link.status] ?? {
                  label: link.status,
                  tone: "neutral" as BadgeTone,
                };
                const retryable = link.status === "failed" || link.status === "blocked";
                return (
                  <TableRow key={link.link_id}>
                    <TableCell className="max-w-96 whitespace-normal">
                      {link.event_id ? (
                        <Link
                          to={`/document/${link.event_id}`}
                          className={cn(
                            "block truncate hover:text-accent hover:underline",
                            link.title ? "font-medium" : "font-mono text-xs",
                          )}
                          title={link.url}
                        >
                          {link.title ?? link.url}
                        </Link>
                      ) : (
                        <span
                          className={cn(
                            "block truncate",
                            link.title ? "font-medium" : "font-mono text-xs",
                          )}
                          title={link.url}
                        >
                          {link.title ?? link.url}
                        </span>
                      )}
                      {link.error && (
                        <span className="block truncate text-xs text-muted-foreground">
                          {link.error}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {linkKinds[link.kind] ?? link.kind}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {timeAgo(link.created_at)}
                    </TableCell>
                    <TableCell>
                      <Badge tone={status.tone}>{status.label}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      {retryable && (
                        <Button
                          size="sm"
                          disabled={retry.isPending}
                          onClick={() => retry.mutate(link)}
                        >
                          Retry
                        </Button>
                      )}
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

// ─── Sign-ins ────────────────────────────────────────────────

/** Sites with a browser sign-in in the sidecar (spec v4 §10c). */
const signInTitles: Record<string, string> = {
  ft: "Sign in to Financial Times",
  wsj: "Sign in to Wall Street Journal",
};

/** Keys the API takes one at a time. Everything printable goes as text. */
const specialKeys = new Set([
  "Enter",
  "Backspace",
  "Tab",
  "Escape",
  "Delete",
  "ArrowUp",
  "ArrowDown",
  "ArrowLeft",
  "ArrowRight",
  "Home",
  "End",
]);

/** Select all, copy, cut: sent as the API writes them ("Meta+a"). Paste is not
 *  here: the browser default runs and onPaste forwards the text itself. */
const comboKeys = new Set(["a", "c", "x"]);
const onMac = navigator.userAgent.includes("Mac");
const comboName = onMac ? "Meta" : "Control";

const frameMs = 700; // one live frame per poll
const typeMs = 150; // characters batch into one type event
const scrollMs = 120; // one wheel event at most
const typeMax = 500; // the API caps text at 500 characters
const frameFails = 4; // failed polls before the view is called stale

/**
 * Live view of a sign-in session in the sidecar browser. Frames are polled
 * one at a time (setTimeout chaining, so a slow answer never queues up) and
 * input is forwarded to the remote page.
 */
function SignInModal({
  session,
  intro,
  onClose,
  onSaved,
}: {
  session: SignInSession;
  intro?: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [frame, setFrame] = useState<SignInFrame | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [stalled, setStalled] = useState(false);
  const layerRef = useRef<HTMLDivElement>(null);
  const ended = useRef(false);
  const opened = useRef(false); // a poll was answered, so the session is real
  const fails = useRef(0);
  const typed = useRef("");
  const typeTimer = useRef<number | null>(null);
  const scrolled = useRef(0);
  const scrollTimer = useRef<number | null>(null);
  // One chain for every forwarded input: two fetches in the same tick arrive
  // in either order, and typed text has to land before the Enter that submits
  // it.
  const queue = useRef<Promise<unknown>>(Promise.resolve());

  const end = useCallback(
    (message?: string) => {
      if (ended.current) return;
      ended.current = true;
      if (message) toast.error(message);
      onClose();
    },
    [onClose],
  );

  /** Back to the live view: a button press leaves focus on the button, and
   *  keys only reach the remote page from the layer. */
  const focusLayer = useCallback(() => {
    layerRef.current?.focus();
  }, []);

  const send = useCallback(
    (ev: SignInEventBody) => {
      queue.current = queue.current
        .then(() => (ended.current ? null : signInEvent(session.id, ev)))
        .then(
          () => {
            if (!ended.current) setNote(null);
          },
          (err: unknown) => {
            if (ended.current) return;
            if (err instanceof ApiError && err.status === 404) {
              end("That sign-in ended.");
              return;
            }
            if (err instanceof ApiError && err.status === 409) {
              setNote("The page did not respond. Try again.");
            } else if (err instanceof ApiError) {
              setNote(err.detail);
            } else {
              setNote("That did not reach the page. Try again.");
            }
            focusLayer();
          },
        );
    },
    [session.id, end, focusLayer],
  );

  // Poll a frame, then wait 700 ms from the answer before asking again.
  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;

    const again = () => {
      timer = window.setTimeout(tick, frameMs);
    };

    const tick = () => {
      timer = null;
      if (stopped) return;
      if (document.hidden) {
        again(); // the tab is in the background; skip the frame, keep the loop
        return;
      }
      void signInFrame(session.id).then(
        (next) => {
          if (stopped) return;
          opened.current = true;
          if (fails.current > 0) {
            fails.current = 0;
            setStalled(false);
          }
          setFrame(next);
          again();
        },
        (err: unknown) => {
          if (stopped) return;
          opened.current = true;
          if (err instanceof ApiError && err.status === 404) {
            end("That sign-in ended.");
            return;
          }
          // the last good frame stays on screen, so say when it went still
          fails.current += 1;
          if (fails.current >= frameFails) setStalled(true);
          again();
        },
      );
    };

    tick();
    return () => {
      stopped = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [session.id, end]);

  // The view takes the keyboard as soon as it opens.
  useEffect(() => {
    focusLayer();
  }, [focusLayer]);

  // The page behind must not scroll under the modal.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  useEffect(
    () => () => {
      if (typeTimer.current !== null) window.clearTimeout(typeTimer.current);
      if (scrollTimer.current !== null) window.clearTimeout(scrollTimer.current);
    },
    [],
  );

  // Leaving the view or closing the tab ends the session: nothing sweeps the
  // sidecar between requests, so an abandoned login page would sit in the
  // shared browser for twenty minutes. The `opened` guard keeps the dev
  // double-mount from ending a session whose first poll is still in flight.
  useEffect(() => {
    const drop = () => {
      if (ended.current || !opened.current) return;
      ended.current = true;
      dropSignIn(session.id);
    };
    window.addEventListener("pagehide", drop);
    return () => {
      window.removeEventListener("pagehide", drop);
      drop();
    };
  }, [session.id]);

  const flushTyped = useCallback(() => {
    if (typeTimer.current !== null) {
      window.clearTimeout(typeTimer.current);
      typeTimer.current = null;
    }
    const text = typed.current;
    typed.current = "";
    if (text) send({ kind: "type", text });
  }, [send]);

  // Characters gather for 150 ms so typing reads as words, not keystrokes.
  const queueChar = (ch: string) => {
    typed.current += ch;
    if (typed.current.length >= typeMax) {
      flushTyped();
      return;
    }
    if (typeTimer.current !== null) window.clearTimeout(typeTimer.current);
    typeTimer.current = window.setTimeout(() => {
      typeTimer.current = null;
      flushTyped();
    }, typeMs);
  };

  // Scaled to the remote viewport and kept inside it: a click on the far edge
  // rounds up to the width itself, which the API rejects.
  const point = (e: MouseEvent<HTMLDivElement>): { x: number; y: number } | null => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;
    const on = (value: number, size: number) =>
      Math.min(size - 1, Math.max(0, Math.round(value)));
    return {
      x: on(((e.clientX - rect.left) / rect.width) * session.width, session.width),
      y: on(((e.clientY - rect.top) / rect.height) * session.height, session.height),
    };
  };

  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const at = point(e);
    if (!at) return;
    flushTyped();
    send({ kind: "click", ...at });
  };

  const onDoubleClick = (e: MouseEvent<HTMLDivElement>) => {
    const at = point(e);
    if (!at) return;
    flushTyped();
    send({ kind: "dblclick", ...at });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const key = e.key;
    const lower = key.toLowerCase();
    if ((onMac ? e.metaKey : e.ctrlKey) && comboKeys.has(lower)) {
      e.preventDefault();
      flushTyped();
      send({ kind: "key", key: `${comboName}+${lower}` });
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (specialKeys.has(key)) {
      e.preventDefault();
      flushTyped();
      send({ kind: "key", key });
      return;
    }
    if (key.length === 1) {
      e.preventDefault();
      queueChar(key);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLDivElement>) => {
    e.preventDefault();
    const text = e.clipboardData.getData("text");
    if (!text) return;
    flushTyped();
    send({ kind: "type", text: text.slice(0, typeMax) });
  };

  const onWheel = (e: WheelEvent<HTMLDivElement>) => {
    scrolled.current += e.deltaY;
    if (scrollTimer.current !== null) return;
    scrollTimer.current = window.setTimeout(() => {
      scrollTimer.current = null;
      const dy = Math.round(scrolled.current);
      scrolled.current = 0;
      if (dy !== 0) send({ kind: "scroll", dy });
    }, scrollMs);
  };

  const finish = useMutation({
    mutationFn: () => finishSignIn(session.id),
    onSuccess: (res) => {
      if (res.ok && res.signed_in) {
        ended.current = true;
        toast.success("Signed in. Cookies saved.");
        onSaved();
        onClose();
        return;
      }
      // the modal stays open to finish signing in, so the keyboard goes back
      // to the live view instead of sitting on the Done button
      setNote(res.message);
      focusLayer();
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 404) {
        end("That sign-in ended.");
        return;
      }
      toast.error(errorDetail(err));
      focusLayer();
    },
  });

  const cancel = () => {
    ended.current = true;
    void cancelSignIn(session.id).catch(() => undefined);
    onClose();
  };

  const title = signInTitles[session.site] ?? "Sign in";
  const address = frame?.url ?? session.url;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-background/85 p-4 sm:p-6"
    >
      <div className="w-full max-w-5xl rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 className="text-[13px] font-medium text-foreground">{title}</h2>
            <p className="mt-0.5 text-muted-foreground">
              {intro ??
                "Sign in as you normally would. When the site shows you signed in, press Done."}
            </p>
          </div>
          <span
            className="max-w-full min-w-0 truncate font-mono text-xs text-muted-foreground"
            title={address}
          >
            {address}
          </span>
        </div>

        <div className="space-y-2 p-4">
          <div
            className="relative w-full overflow-hidden rounded-md border border-border bg-muted"
            style={{ aspectRatio: `${session.width} / ${session.height}` }}
          >
            {frame ? (
              <img
                src={`data:image/jpeg;base64,${frame.image}`}
                alt=""
                draggable={false}
                className={cn(
                  "h-full w-full select-none object-contain transition-opacity",
                  stalled && "opacity-40",
                )}
              />
            ) : (
              <span className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-muted-foreground">
                <Spinner />
                Opening the page.
              </span>
            )}
            <div
              ref={layerRef}
              tabIndex={0}
              autoFocus
              onClick={onClick}
              onDoubleClick={onDoubleClick}
              onKeyDown={onKeyDown}
              onPaste={onPaste}
              onWheel={onWheel}
              className="absolute inset-0 outline-none ring-inset focus-visible:ring-2 focus-visible:ring-accent/40"
            />
          </div>

          {stalled && (
            <p className="text-warn">
              The live view stopped updating. Close and start the sign-in again.
            </p>
          )}
          {note && <p className="text-warn">{note}</p>}
        </div>

        <div className="flex justify-end gap-1.5 border-t border-border px-4 py-3">
          <Button variant="ghost" onClick={cancel}>
            Cancel
          </Button>
          <Button variant="primary" disabled={finish.isPending} onClick={() => finish.mutate()}>
            {finish.isPending && <Spinner className="size-3.5" />}
            Done
          </Button>
        </div>
      </div>
    </div>
  );
}

/**
 * The fast path (spec v4 §10f): the analyst's email and password, typed into
 * the site's login form by the server. On success the row goes green with no
 * live view at all; when the site needs a step the server cannot type (a
 * one-time code, a captcha) the live view takes over the same open session.
 * "Sign in in the browser" skips straight to the live view.
 */
function CredentialSignIn({
  site,
  label,
  onLive,
  onSaved,
  onClose,
}: {
  site: string;
  label: string;
  onLive: (session: SignInSession, intro?: string) => void;
  onSaved: () => void;
  onClose: () => void;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const signIn = useMutation({
    mutationFn: () => submitSignIn(site, { email, password }),
    onSuccess: (res) => {
      if (res.signed_in) {
        toast.success("Signed in. Cookies saved.");
        onSaved();
        onClose();
        return;
      }
      if (res.session) {
        onLive(res.session, res.message);
        return;
      }
      toast.error(res.message);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 503) {
        toast.error("The browser is not running.");
        return;
      }
      toast.error(errorDetail(err));
    },
  });

  const live = useMutation({
    mutationFn: () => startSignIn(site),
    onSuccess: (session) => onLive(session),
    onError: (err) => {
      if (err instanceof ApiError && err.status === 503) {
        toast.error("The browser is not running.");
        return;
      }
      toast.error(errorDetail(err));
    },
  });

  const busy = signIn.isPending || live.isPending;
  const ready = email.trim().length > 0 && password.length > 0;

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (ready && !busy) signIn.mutate();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Sign in to ${label}`}
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-background/85 p-4 sm:p-6"
    >
      <form
        onSubmit={onSubmit}
        className="mt-[8vh] w-full max-w-sm rounded-lg border border-border bg-card p-4"
      >
        <h2 className="text-[13px] font-medium text-foreground">Sign in to {label}</h2>
        <p className="mt-0.5 text-muted-foreground">
          Your details are used once to sign in. They are not stored.
        </p>

        <label className="mt-3 block">
          <span className="text-xs text-muted-foreground">Email</span>
          <Input
            type="email"
            autoComplete="username"
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full"
          />
        </label>
        <label className="mt-2 block">
          <span className="text-xs text-muted-foreground">Password</span>
          <Input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full"
          />
        </label>

        <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={() => live.mutate()}
          >
            {live.isPending && <Spinner className="size-3.5" />}
            Sign in in the browser
          </Button>
          <div className="flex gap-1.5">
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={busy || !ready}>
              {signIn.isPending && <Spinner className="size-3.5" />}
              Sign in
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}

function credentialBadge(row: CredentialRow): { label: string; tone: BadgeTone } {
  if (!row.set) return { label: "not set", tone: "idle" };
  if (row.check_ok === true) return { label: "signed in", tone: "ok" };
  if (row.check_ok === false) return { label: "failing", tone: "fail" };
  return { label: "set", tone: "neutral" };
}

function CredentialItem({ row }: { row: CredentialRow }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  // the link the probe runs against, for sites whose test needs one (Substack:
  // a paid post from a publication the account subscribes to, since a
  // subscription is per publication and no post we could pick proves anything)
  const [testLink, setTestLink] = useState("");
  const needsTestLink = Boolean(row.test_link);
  // the email/password step, and the live view it can hand off to (with the
  // note that says why the live view appeared)
  const [signingIn, setSigningIn] = useState(false);
  const [session, setSession] = useState<SignInSession | null>(null);
  const [intro, setIntro] = useState<string | undefined>(undefined);
  const badge = credentialBadge(row);
  const canSignIn = row.site in signInTitles;

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  const closeCreds = useCallback(() => setSigningIn(false), []);
  const closeSignIn = useCallback(() => setSession(null), []);

  // the fast path or the live view lands on the same open session; take over
  const goLive = useCallback((next: SignInSession, note?: string) => {
    setSigningIn(false);
    setIntro(note);
    setSession(next);
  }, []);

  // the same refresh as a pasted sign-in: the finish requeued blocked links
  // and cleared the source errors, and both are counted on the dashboard
  const savedSignIn = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  }, [queryClient]);

  const save = useMutation({
    mutationFn: () => saveCredential(row.site, value.trim()),
    onSuccess: () => {
      toast.success("Saved.");
      setValue("");
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const check = useMutation({
    mutationFn: () =>
      testCredential(row.site, needsTestLink ? testLink.trim() : undefined),
    onSuccess: (res) => {
      if (res.ok) toast.success(res.message);
      else toast.error(res.message);
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const drop = useMutation({
    mutationFn: () => deleteCredential(row.site),
    onSuccess: () => {
      toast.success(`Removed the ${row.label} sign-in.`);
      setValue("");
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const busy = save.isPending || check.isPending || drop.isPending;
  const canTest = !busy && row.set && (!needsTestLink || testLink.trim().length > 0);
  const testButton = (
    <Button disabled={!canTest} onClick={() => check.mutate()}>
      {check.isPending && <Spinner className="size-3.5" />}
      Test
    </Button>
  );

  return (
    <li className="border-b border-border px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{row.label}</span>
        <Badge tone={badge.tone}>{badge.label}</Badge>
        {row.set && row.checked_at && (
          <span className="text-xs text-muted-foreground">
            Checked {timeAgo(row.checked_at)}
          </span>
        )}
      </div>

      <p className="mt-0.5 text-muted-foreground">{row.help}</p>

      {row.check_ok === false && row.check_message && (
        <p className="mt-0.5 text-xs text-muted-foreground">{row.check_message}</p>
      )}

      <div className="mt-2 flex flex-wrap items-start gap-2">
        {row.kind === "cookies" ? (
          <textarea
            rows={3}
            spellCheck={false}
            placeholder={row.set ? "Paste a new export to replace it" : "Paste here"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-[26rem] max-w-full rounded-md border border-border bg-card px-2.5 py-1.5 font-mono text-xs text-foreground outline-none transition-colors placeholder:font-sans placeholder:text-[13px] placeholder:text-muted-foreground/70 focus:border-accent/50 focus:ring-2 focus:ring-accent/20"
          />
        ) : (
          <Input
            type="password"
            autoComplete="off"
            placeholder={row.set ? "Paste a new token to replace it" : "Paste here"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-[26rem] max-w-full"
          />
        )}

        <div className="flex flex-wrap gap-1.5">
          <Button
            variant="primary"
            disabled={busy || !value.trim()}
            onClick={() => save.mutate()}
          >
            {save.isPending && <Spinner className="size-3.5" />}
            Save
          </Button>
          {!needsTestLink && testButton}
          {canSignIn && (
            <Button disabled={busy} onClick={() => setSigningIn(true)}>
              Sign in
            </Button>
          )}
          <Button variant="ghost" disabled={busy || !row.set} onClick={() => drop.mutate()}>
            Remove
          </Button>
        </div>
      </div>

      {needsTestLink && (
        <div className="mt-2 flex flex-wrap items-start gap-2">
          <Input
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            aria-label={`${row.label} test link`}
            placeholder={row.test_link ?? ""}
            value={testLink}
            onChange={(e) => setTestLink(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && canTest) check.mutate();
            }}
            className="w-[26rem] max-w-full"
          />
          {testButton}
        </div>
      )}

      {signingIn && (
        <CredentialSignIn
          site={row.site}
          label={row.label}
          onLive={goLive}
          onSaved={savedSignIn}
          onClose={closeCreds}
        />
      )}

      {session && (
        <SignInModal
          session={session}
          intro={intro}
          onClose={closeSignIn}
          onSaved={savedSignIn}
        />
      )}
    </li>
  );
}

function SignInsCard({ credentials }: { credentials: CredentialRow[] }) {
  return (
    <Card id="sign-ins" className="scroll-mt-4">
      <CardHeader>
        <CardTitle>Sign-ins</CardTitle>
        <span className="text-xs text-muted-foreground">
          Stored values stay in the database
        </span>
      </CardHeader>
      <CardContent className="p-0 pb-1">
        <ul>
          {credentials.map((row) => (
            <CredentialItem key={row.site} row={row} />
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

// ─── Upload ──────────────────────────────────────────────────

function UploadCard() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [results, setResults] = useState<UploadResult[]>([]);

  const upload = useMutation({
    mutationFn: (files: File[]) => {
      const form = new FormData();
      for (const file of files) form.append("files", file);
      return apiFetch<UploadResponse>("/api/upload", { method: "POST", body: form });
    },
    onSuccess: (res) => {
      setResults((prev) => [...res.events, ...prev]);
      void queryClient.invalidateQueries({ queryKey: ["status"] });
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const submit = (list: FileList | null) => {
    const files = [...(list ?? [])];
    if (files.length > 0) upload.mutate(files);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Upload</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            submit(e.dataTransfer.files);
          }}
          className={cn(
            "flex w-full flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed px-6 py-8 text-muted-foreground outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent/40",
            dragging
              ? "border-accent/50 bg-accent/5 text-foreground"
              : "border-border hover:border-idle/70 hover:text-foreground",
          )}
        >
          {upload.isPending ? <Spinner /> : <Upload className="size-4" />}
          <span>Drop files here or click to choose</span>
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            submit(e.target.files);
            e.target.value = "";
          }}
        />

        {results.length > 0 && (
          <ul>
            {results.map((result, i) => (
              <li
                key={`${result.filename}-${i}`}
                className="flex items-center justify-between gap-3 border-b border-border py-2 last:border-0"
              >
                <span className="min-w-0 truncate">{result.filename ?? "upload"}</span>
                <Badge tone={result.duplicate ? "idle" : "ok"}>
                  {result.duplicate ? "already in the vault" : "added"}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ─── Page ────────────────────────────────────────────────────

export function SourcesPage() {
  const query = useQuery({
    queryKey: ["sources"],
    queryFn: () => apiFetch<SourcesResponse>("/api/sources"),
  });

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <h1 className="text-base font-medium tracking-tight">Sources</h1>

      {query.isPending && (
        <div className="flex justify-center py-24">
          <Spinner />
        </div>
      )}

      {query.isError && (
        <Card>
          <CardContent className="px-4 py-8 text-center text-muted-foreground">
            Could not load sources.
          </CardContent>
        </Card>
      )}

      {query.data && (
        <>
          <WatchlistCard watchlist={query.data.watchlist} />
          <SourcesCard sources={query.data.sources} />
          <LinksCard links={query.data.links} />
          <SignInsCard credentials={query.data.credentials} />
          <UploadCard />
        </>
      )}
    </div>
  );
}
