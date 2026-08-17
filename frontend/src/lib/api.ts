/**
 * Minimal JSON API client. The app is served under /vault/ (vite base);
 * BASE_URL keeps API calls on the same prefix so nginx routes them to the
 * backend in prod and the dev proxy handles them locally.
 */

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const base = import.meta.env.BASE_URL.replace(/\/+$/, "");

export async function apiFetch<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const headers = new Headers(opts.headers);
  if (opts.body !== undefined && typeof opts.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${base}${path}`, { ...opts, headers });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body: unknown = await res.json();
      if (
        body !== null &&
        typeof body === "object" &&
        "detail" in body &&
        typeof (body as { detail: unknown }).detail === "string"
      ) {
        detail = (body as { detail: string }).detail;
      }
    } catch {
      // non-JSON error body; keep the generic detail
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// ─── /api/status ─────────────────────────────────────────────

export interface Seat {
  seat: number;
  has_token: boolean;
  latched: boolean;
  kind: string | null;
  reason: string | null;
  latched_at: string | null;
}

export interface Heartbeat {
  last_cycle_at: string;
  interval_s: number;
  seats: Seat[];
  run_requested: boolean;
}

export interface StageRun {
  stage: string;
  started_at: string;
  finished_at: string | null;
  summary: Record<string, unknown> | null;
  error: string | null;
}

export interface StatusCounts {
  events: {
    pending: number;
    extracting: number;
    extracted: number;
    failed: number;
    duplicate: number;
  };
  claims: number;
  claims_7d: number;
  mentions: {
    resolved: number;
    queued: number;
    unresolved: number;
    skipped: number;
  };
  entities: number;
  edges: { asserted: number; inferred: number };
  er_queue: { pending: number; decided: number; failed: number };
  hypotheses: Record<string, number>;
}

export interface SourceRow {
  source_id: string;
  name: string;
  connector: string;
  status: string;
  last_polled: string | null;
  events: number;
}

export interface FailedEvent {
  event_id: string;
  connector: string;
  title: string | null;
  last_error: string | null;
  fetched_at: string;
}

export interface StatusResponse {
  heartbeat: Heartbeat | null;
  stages: StageRun[];
  counts: StatusCounts;
  sources: SourceRow[];
  failed_events: FailedEvent[];
}

// ─── /api/claims ─────────────────────────────────────────────

export interface ClaimSubject {
  surface: string;
  entity_id: string | null;
  name: string | null;
}

export interface ClaimObject {
  surface?: string | null;
  literal?: unknown;
  entity_id?: string | null;
  name?: string | null;
}

export interface Claim {
  claim_id: string;
  subject: ClaimSubject;
  predicate: string;
  object: ClaimObject;
  stance: string | null;
  confidence: number | null;
  evidence_quote: string | null;
  observed_at: string;
  published_at: string | null;
  connector: string;
  source_name: string | null;
  doc_title: string | null;
  event_id: string;
}

export interface ClaimsResponse {
  total: number;
  claims: Claim[];
}

// ─── /api/entities ───────────────────────────────────────────

export interface EntityListRow {
  entity_id: string;
  name: string;
  kind: string;
  registry: string;
  claims: number;
}

// ─── /api/entity/{id} ────────────────────────────────────────

export interface EntityPeer {
  entity_id: string;
  name: string;
}

export interface EdgeRow {
  edge_id: string;
  peer: EntityPeer;
  predicate: string;
  direction: "out" | "in";
  claims: number;
  confidence: number | null;
  last_evidence_at: string | null;
  relevance: number;
  archived: boolean;
}

export interface EntityResponse {
  entity: {
    entity_id: string;
    name: string;
    kind: string;
    registry_refs: Record<string, unknown>;
  };
  aliases: string[];
  claims: Claim[];
  edges: { asserted: EdgeRow[]; inferred: EdgeRow[] };
}

// ─── /api/hypotheses ─────────────────────────────────────────

export interface Hypothesis {
  hypothesis_id: string;
  type: string;
  subjects: EntityPeer[];
  state: string;
  rationale: string | null;
  created_at: string;
}

// ─── /api/sources ────────────────────────────────────────────

export interface WatchlistRow {
  ticker: string;
  sector: string | null;
  active: boolean;
  company: string | null;
  events: number;
}

export interface FeedRow {
  source_id: string;
  name: string;
  connector: string;
  url: string;
  status: string;
  last_polled: string | null;
  events: number;
}

export interface SourcesResponse {
  watchlist: WatchlistRow[];
  feeds: FeedRow[];
}

// ─── /api/upload ─────────────────────────────────────────────

export interface UploadResult {
  filename: string | null;
  event_id: string | null;
  duplicate: boolean;
}

export interface UploadResponse {
  events: UploadResult[];
}
