import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { toast } from "sonner";

import {
  apiFetch,
  ApiError,
  type FeedRow,
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

// ─── Watchlist ───────────────────────────────────────────────

function WatchlistCard({ watchlist }: { watchlist: WatchlistRow[] }) {
  const queryClient = useQueryClient();
  const [ticker, setTicker] = useState("");
  const [sector, setSector] = useState("");

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  const add = useMutation({
    mutationFn: (body: { ticker: string; sector: string | null }) =>
      apiFetch<{ ok: boolean; ticker: string; company: string }>("/api/watchlist", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (res) => {
      toast.success(`Added ${res.company}.`);
      setTicker("");
      setSector("");
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

  const sectors = [...new Set(watchlist.map((row) => row.sector).filter(Boolean))].sort();

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const t = ticker.trim();
    if (!t) return;
    add.mutate({ ticker: t, sector: sector.trim() || null });
  };

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
                  <TableCell className="max-w-72 truncate">{row.company ?? "-"}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {row.sector ?? "-"}
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

        <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-2 px-4">
          <Input
            placeholder="Ticker"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            className="w-28 font-mono"
          />
          <Input
            placeholder="Sector"
            value={sector}
            onChange={(e) => setSector(e.target.value)}
            list="sector-options"
            className="w-44"
          />
          <datalist id="sector-options">
            {sectors.map((s) => (
              <option key={s} value={s ?? ""} />
            ))}
          </datalist>
          <Button type="submit" disabled={add.isPending || !ticker.trim()}>
            {add.isPending && <Spinner className="size-3.5" />}
            Add
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

// ─── Feeds ───────────────────────────────────────────────────

function feedStatus(status: string): { label: string; tone: BadgeTone } {
  if (status === "active") return { label: "active", tone: "ok" };
  if (status === "demoted") return { label: "paused", tone: "idle" };
  return { label: status, tone: "neutral" };
}

function feedType(connector: string): string {
  return connector === "podcast" ? "Podcast" : "News feed";
}

function FeedsCard({ feeds }: { feeds: FeedRow[] }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [kind, setKind] = useState<"podcast" | "rss">("rss");

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["sources"] });
    void queryClient.invalidateQueries({ queryKey: ["status"] });
  };

  const add = useMutation({
    mutationFn: (body: { name: string; url: string; kind: string }) =>
      apiFetch<{ ok: boolean; source_id: string }>("/api/feeds", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSuccess: (_res, body) => {
      toast.success(`Added ${body.name}.`);
      setName("");
      setUrl("");
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const toggle = useMutation({
    mutationFn: (feed: FeedRow) =>
      apiFetch<{ ok: boolean; status: string }>(`/api/sources/${feed.source_id}/toggle`, {
        method: "POST",
      }),
    onSuccess: (res, feed) => {
      toast.success(res.status === "active" ? `Resumed ${feed.name}.` : `Paused ${feed.name}.`);
      invalidate();
    },
    onError: (err) => toast.error(errorDetail(err)),
  });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !url.trim()) return;
    add.mutate({ name: name.trim(), url: url.trim(), kind });
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Feeds</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 p-0 pb-3">
        {feeds.length === 0 ? (
          <p className="px-4 text-muted-foreground">No feeds configured.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>URL</TableHead>
                <TableHead>Last polled</TableHead>
                <TableHead className="text-right">Docs</TableHead>
                <TableHead>Status</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {feeds.map((feed) => {
                const status = feedStatus(feed.status);
                return (
                  <TableRow key={feed.source_id}>
                    <TableCell className="max-w-56 truncate font-medium">
                      {feed.name}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {feedType(feed.connector)}
                    </TableCell>
                    <TableCell className="max-w-64 truncate font-mono text-xs text-muted-foreground">
                      {feed.url}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {feed.last_polled ? timeAgo(feed.last_polled) : "Never"}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs">
                      {fmtNum(feed.events)}
                    </TableCell>
                    <TableCell>
                      <Badge tone={status.tone}>{status.label}</Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <Button
                        size="sm"
                        disabled={toggle.isPending}
                        onClick={() => toggle.mutate(feed)}
                      >
                        {feed.status === "active" ? "Pause" : "Resume"}
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}

        <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4">
          <Input
            placeholder="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-44"
          />
          <Input
            placeholder="URL"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className="w-72"
          />
          <span className="flex items-center gap-3 text-[13px]">
            <label className="flex cursor-pointer items-center gap-1.5">
              <input
                type="radio"
                name="feed-kind"
                checked={kind === "podcast"}
                onChange={() => setKind("podcast")}
                className="accent-accent"
              />
              Podcast
            </label>
            <label className="flex cursor-pointer items-center gap-1.5">
              <input
                type="radio"
                name="feed-kind"
                checked={kind === "rss"}
                onChange={() => setKind("rss")}
                className="accent-accent"
              />
              News feed
            </label>
          </span>
          <Button type="submit" disabled={add.isPending || !name.trim() || !url.trim()}>
            {add.isPending && <Spinner className="size-3.5" />}
            Add
          </Button>
        </form>
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
          <FeedsCard feeds={query.data.feeds} />
          <UploadCard />
        </>
      )}
    </div>
  );
}
