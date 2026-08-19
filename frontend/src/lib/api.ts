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
  alerts_unread: number;
  contradictions_open: number;
}

export interface SourceRow {
  source_id: string;
  name: string;
  connector: string;
  /** User-facing type from the API's source_label helper (spec v4 §7). */
  label: string;
  status: string;
  last_polled: string | null;
  last_error: string | null;
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
  predicate_canon: string | null;
  object: ClaimObject;
  stance: string | null;
  status: string;
  superseded_by: string | null;
  confidence: number | null;
  confidence_now: number | null;
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
  merged_from: EntityPeer[];
  aliases: string[];
  claims: Claim[];
  edges: { asserted: EdgeRow[]; inferred: EdgeRow[] };
}

export function mergeEntity(entityId: string, into: string): Promise<{ ok: boolean; into: string }> {
  return apiFetch(`/api/entities/${entityId}/merge`, {
    method: "POST",
    body: JSON.stringify({ into }),
  });
}

export function unmergeEntity(entityId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/entities/${entityId}/unmerge`, { method: "POST" });
}

// ─── /api/documents ──────────────────────────────────────────

/** The feed, bucket or upload folder a document came from (spec v5 §5). */
export interface DocumentSourceRef {
  source_id: string;
  name: string;
  label: string;
}

/** One document in the list (spec v5 §5). `type` is the API's own label. */
export interface DocumentRow {
  event_id: string;
  title: string;
  connector: string;
  type: string;
  source: DocumentSourceRef;
  site: string | null;
  url: string | null;
  published_at: string | null;
  fetched_at: string;
  status: string;
  thin: boolean;
  materiality: number | null;
  claims: number;
  entities: number;
  summary_status: SummaryStatus;
  summary: string | null;
}

export interface DocumentsResponse {
  total: number;
  documents: DocumentRow[];
}

/** A source facet for the list filter: every source with a document. */
export interface DocumentSourceFacet {
  source_id: string;
  name: string;
  connector: string;
  label: string;
  events: number;
}

export interface DocumentCopy {
  event_id: string;
  title: string;
  source_name: string;
  fetched_at: string;
}

export interface DocumentLineage {
  lineage_id: string | null;
  /** Set when this document is a copy of another one. */
  root: { event_id: string; title: string } | null;
  copies: DocumentCopy[];
}

/** Header facts the connector recorded; only present keys are sent. */
export type DocumentFacts = Partial<
  Record<
    | "ticker"
    | "form"
    | "items"
    | "accession"
    | "feed"
    | "channel"
    | "username"
    | "filename"
    | "site"
    | "bridge"
    | "video_id",
    string
  >
>;

export interface DocumentDetailRow
  extends Omit<DocumentRow, "summary_status" | "summary"> {
  attempts: number;
  last_error: string | null;
  chars: number;
  route: string | null;
  facts: DocumentFacts;
  lineage: DocumentLineage;
  /** The document this one was matched to as a near duplicate, if any. */
  near_duplicate_of: { event_id: string; title: string } | null;
}

/** "none" when the worker has not written a row yet (spec v5 §1). */
export type SummaryStatus =
  | "none"
  | "requested"
  | "pending"
  | "done"
  | "failed"
  | "skipped";

export interface DocumentSummary {
  status: SummaryStatus;
  summary: string | null;
  key_points: string[];
  model: string | null;
  error: string | null;
  requested_at: string | null;
  updated_at: string | null;
}

export type MentionState =
  | "resolved"
  | "queued"
  | "pending"
  | "unresolved"
  | "skipped";

export interface DocumentMention {
  mention_id: string;
  surface: string;
  state: MentionState;
  entity: EntityPeer | null;
  confidence: number | null;
}

export interface DocumentEntity {
  entity_id: string;
  name: string;
  kind: string;
  /** Ticker, else country, else "-" (the API's registry_short). */
  registry: string;
  /** The ticker alone, null when the company has none. */
  ticker: string | null;
  claims: number;
  mentions: number;
}

/** An edge backed by at least one of this document's claims. */
export interface DocumentEdge {
  edge_id: string;
  src: EntityPeer;
  dst: EntityPeer;
  predicate: string;
  origin: string;
  claims_here: number;
  claims_total: number;
  relevance: number;
  archived: boolean;
}

export interface DocumentAlert {
  alert_id: string;
  kind: string;
  title: string;
  created_at: string;
}

export interface DocumentHypothesis {
  hypothesis_id: string;
  type: string;
  state: string;
  subjects: EntityPeer[];
}

export interface DocumentContradiction {
  contradiction_id: string;
  status: string;
  predicate_canon: string;
  subject: EntityPeer;
}

export interface DocumentResponse {
  document: DocumentDetailRow;
  summary: DocumentSummary;
  claims: Claim[];
  mentions: DocumentMention[];
  entities: DocumentEntity[];
  edges: DocumentEdge[];
  alerts: DocumentAlert[];
  hypotheses: DocumentHypothesis[];
  contradictions: DocumentContradiction[];
}

/** The artifact: the connector header and the body, cut at 200k chars. */
export interface DocumentText {
  header: Record<string, string>;
  body: string;
  chars: number;
  truncated: boolean;
}

export interface DocumentsQuery {
  q?: string;
  source?: string;
  type?: string;
  status?: string;
  days?: string;
  ticker?: string;
  limit?: number;
  offset?: number;
}

export function getDocuments(opts: DocumentsQuery = {}): Promise<DocumentsResponse> {
  const params = new URLSearchParams();
  if (opts.q) params.set("q", opts.q);
  if (opts.source) params.set("source", opts.source);
  if (opts.type) params.set("type", opts.type);
  if (opts.status) params.set("status", opts.status);
  if (opts.days) params.set("days", opts.days);
  if (opts.ticker) params.set("ticker", opts.ticker);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.offset !== undefined) params.set("offset", String(opts.offset));
  const qs = params.toString();
  return apiFetch(`/api/documents${qs ? `?${qs}` : ""}`);
}

export function getDocumentSources(): Promise<DocumentSourceFacet[]> {
  return apiFetch("/api/documents/sources");
}

export function getDocument(eventId: string): Promise<DocumentResponse> {
  return apiFetch(`/api/documents/${eventId}`);
}

export function getDocumentText(eventId: string): Promise<DocumentText> {
  return apiFetch(`/api/documents/${eventId}/text`);
}

/** Ask the worker for a summary; the web process never calls the model. */
export function requestSummary(
  eventId: string,
): Promise<{ ok: boolean; status: SummaryStatus }> {
  return apiFetch(`/api/documents/${eventId}/summarize`, { method: "POST" });
}

// ─── /api/hypotheses ─────────────────────────────────────────

export interface Hypothesis {
  hypothesis_id: string;
  type: string;
  subjects: EntityPeer[];
  state: string;
  score: number | null;
  rationale: string | null;
  created_at: string;
  updated_at: string;
}

export interface TestPlan {
  confirm?: string[];
  refute?: string[];
}

export interface HypothesisHistoryEntry {
  at?: string;
  from?: string | null;
  to?: string | null;
  note?: string | null;
  refined?: Record<string, unknown> | null;
  investigated?: Record<string, unknown> | null;
  error?: string | null;
}

export interface HypothesisDetail {
  hypothesis: {
    hypothesis_id: string;
    type: string;
    statement: { text?: string; template?: string; via_entity?: string } | null;
    rationale: string | null;
    test_plan: TestPlan | null;
    score: number | null;
    state: string;
    confidence: number | null;
    wake_conditions: Record<string, unknown> | null;
    parked_at: string | null;
    created_at: string;
    updated_at: string;
  };
  subjects: EntityPeer[];
  evidence: Claim[];
  lineages: number;
  verifier: Record<string, unknown> | null;
  history: HypothesisHistoryEntry[];
}

export function getHypotheses(state = ""): Promise<Hypothesis[]> {
  const suffix = state ? `?state=${encodeURIComponent(state)}` : "";
  return apiFetch(`/api/hypotheses${suffix}`);
}

export function getHypothesis(hypothesisId: string): Promise<HypothesisDetail> {
  return apiFetch(`/api/hypotheses/${hypothesisId}`);
}

export function reviewHypothesis(
  hypothesisId: string,
  verdict: "accept" | "reject",
): Promise<{ ok: boolean; state: string }> {
  return apiFetch(`/api/hypotheses/${hypothesisId}/review`, {
    method: "POST",
    body: JSON.stringify({ verdict }),
  });
}

// ─── /api/alerts ─────────────────────────────────────────────

export interface Alert {
  alert_id: string;
  kind: string;
  title: string;
  body: string | null;
  entities: EntityPeer[];
  event_id: string | null;
  hypothesis_id: string | null;
  created_at: string;
  read_at: string | null;
}

export interface AlertsResponse {
  unread: number;
  alerts: Alert[];
}

export function getAlerts(opts: { limit?: number; unread?: boolean } = {}): Promise<AlertsResponse> {
  const params = new URLSearchParams();
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.unread) params.set("unread", "true");
  const qs = params.toString();
  return apiFetch(`/api/alerts${qs ? `?${qs}` : ""}`);
}

export function markAlertRead(alertId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/alerts/${alertId}/read`, { method: "POST" });
}

export function markAllAlertsRead(): Promise<{ ok: boolean; marked: number }> {
  return apiFetch("/api/alerts/read-all", { method: "POST" });
}

// ─── /api/digest ─────────────────────────────────────────────

export interface DigestEntity {
  entity_id: string;
  name: string;
  claims: number;
}

export interface DigestHypothesis {
  hypothesis_id: string | null;
  title: string;
}

export interface DigestResponse {
  since: string;
  claims: number;
  events: Record<string, number>;
  top_entities: DigestEntity[];
  promoted: DigestHypothesis[];
  woke: DigestHypothesis[];
  contradictions_open: number;
  failed_events: number;
}

export function getDigest(hours?: number): Promise<DigestResponse> {
  const suffix = hours !== undefined ? `?hours=${hours}` : "";
  return apiFetch(`/api/digest${suffix}`);
}

// ─── /api/er-queue ───────────────────────────────────────────

export interface ErCandidate {
  title: string;
  cik?: number | null;
  ticker?: string | null;
  lei?: string | null;
  country?: string | null;
  status?: string | null;
  registry?: string | null;
}

export interface ErQueueItem {
  mention_id: string;
  surface: string;
  context: string;
  doc_title: string | null;
  source_name: string | null;
  connector: string;
  candidates: ErCandidate[];
  created_at: string;
  passes: number;
}

export interface ErQueueRecent {
  mention_id: string;
  surface: string;
  doc_title: string | null;
  source_name: string | null;
  connector: string;
  decision: Record<string, unknown> | null;
  decided_at: string | null;
}

export interface ErQueueResponse {
  pending: number;
  items: ErQueueItem[];
  recent: ErQueueRecent[];
}

export interface ErDecideBody {
  decision: "match" | "new_entity" | "not_a_company";
  cik?: number;
  lei?: string;
  name?: string;
}

export function getErQueue(opts: { status?: string; limit?: number } = {}): Promise<ErQueueResponse> {
  const params = new URLSearchParams();
  if (opts.status) params.set("status", opts.status);
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  const qs = params.toString();
  return apiFetch(`/api/er-queue${qs ? `?${qs}` : ""}`);
}

export function decideErQueue(
  mentionId: string,
  body: ErDecideBody,
): Promise<{ ok: boolean; entity_id: string | null }> {
  return apiFetch(`/api/er-queue/${mentionId}/decide`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ─── /api/contradictions ─────────────────────────────────────

export interface Contradiction {
  contradiction_id: string;
  subject: EntityPeer;
  predicate_canon: string;
  kind: string;
  status: string;
  claims: { a: Claim | null; b: Claim | null };
  created_at: string;
}

export function getContradictions(status = "open"): Promise<Contradiction[]> {
  return apiFetch(`/api/contradictions?status=${encodeURIComponent(status)}`);
}

export function resolveContradiction(
  contradictionId: string,
  keep: "a" | "b" | "none",
): Promise<{ ok: boolean }> {
  return apiFetch(`/api/contradictions/${contradictionId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ keep }),
  });
}

// ─── /api/events/retry-all ───────────────────────────────────

export function retryAllEvents(): Promise<{ ok: boolean; retried: number }> {
  return apiFetch("/api/events/retry-all", { method: "POST" });
}

// ─── /api/sources ────────────────────────────────────────────

/** Watchlist row (spec v4 §7). Company facts are resolved from Yahoo
 *  Finance or the SEC registry when the ticker is added. */
export interface WatchlistRow {
  ticker: string;
  name: string | null;
  sector: string | null;
  industry: string | null;
  exchange: string | null;
  country: string | null;
  active: boolean;
  events: number;
  cik: number | null;
}

/** Which premium site a source or link needs, and whether it is signed in. */
export interface CredentialRef {
  site: string;
  set: boolean;
}

/** A polled source (spec v4 §7). `label` is the user-facing type. */
export interface SourceItem {
  source_id: string;
  name: string;
  connector: string;
  label: string;
  site: string | null;
  url: string;
  feed_url: string | null;
  status: string;
  last_polled: string | null;
  last_error: string | null;
  events: number;
  credential: CredentialRef | null;
}

/** A one-off link pasted into the sources page (spec v4 §6.3). */
export interface LinkRow {
  link_id: string;
  url: string;
  title: string | null;
  kind: string;
  site: string | null;
  status: string;
  error: string | null;
  event_id: string | null;
  created_at: string;
}

/** Sign-in state for one premium site. The stored value never leaves the API. */
export interface CredentialRow {
  site: string;
  label: string;
  kind: string;
  set: boolean;
  updated_at: string | null;
  checked_at: string | null;
  check_ok: boolean | null;
  check_message: string | null;
  help: string;
  /** Placeholder for the link the Test button runs against (Substack: a
   *  paid post the account subscribes to); null when the probe takes none. */
  test_link: string | null;
}

export interface SourcesResponse {
  watchlist: WatchlistRow[];
  sources: SourceItem[];
  links: LinkRow[];
  credentials: CredentialRow[];
}

// ─── /api/watchlist ──────────────────────────────────────────

export interface TickerSuggestion {
  symbol: string;
  name: string | null;
  exchange: string | null;
  type: string | null;
}

export function searchTickers(q: string): Promise<TickerSuggestion[]> {
  return apiFetch(`/api/watchlist/search?q=${encodeURIComponent(q)}`);
}

// ─── /api/sources/resolve ────────────────────────────────────

/** What the URL router made of a pasted link (spec v4 §4). */
export interface Resolution {
  kind: string;
  connector: string | null;
  label: string;
  url: string;
  name: string | null;
  feed_url: string | null;
  site: string | null;
  config: Record<string, unknown>;
  one_off: boolean;
  link_kind: string | null;
  credential: CredentialRef | null;
  message: string | null;
}

export interface AddSourceResponse {
  ok: boolean;
  kind: "source" | "link";
  source?: SourceItem;
  link?: LinkRow;
}

export function resolveSource(url: string): Promise<Resolution> {
  return apiFetch("/api/sources/resolve", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export function addSource(url: string, name?: string): Promise<AddSourceResponse> {
  return apiFetch("/api/sources", {
    method: "POST",
    body: JSON.stringify({ url, name }),
  });
}

export function removeSource(sourceId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/sources/${sourceId}`, { method: "DELETE" });
}

// ─── /api/links ──────────────────────────────────────────────

export function retryLink(linkId: string): Promise<LinkRow> {
  return apiFetch(`/api/links/${linkId}/retry`, { method: "POST" });
}

// ─── /api/credentials ────────────────────────────────────────

export function saveCredential(
  site: string,
  value: string,
  note?: string,
): Promise<{ ok: boolean; site: string; set: boolean }> {
  return apiFetch(`/api/credentials/${site}`, {
    method: "PUT",
    body: JSON.stringify({ value, note }),
  });
}

export function deleteCredential(site: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/credentials/${site}`, { method: "DELETE" });
}

export function testCredential(
  site: string,
  url?: string,
): Promise<{ ok: boolean; message: string }> {
  return apiFetch(`/api/credentials/${site}/test`, {
    method: "POST",
    ...(url ? { body: JSON.stringify({ url }) } : {}),
  });
}

// ─── /api/signin ─────────────────────────────────────────────

/** A live sign-in session in the sidecar browser (spec v4 §10c). One per
 *  site, held in the web process, closed on Done or Cancel. */
export interface SignInSession {
  id: string;
  site: string;
  url: string;
  title: string;
  width: number;
  height: number;
  /** True when a session for the site was already open. */
  resumed: boolean;
}

/** One polled frame: the page viewport as a base64 JPEG. */
export interface SignInFrame {
  id: string;
  site: string;
  url: string;
  title: string;
  width: number;
  height: number;
  image: string;
}

/** An input forwarded to the remote page. Coordinates are viewport pixels. */
export interface SignInEventBody {
  kind: "click" | "dblclick" | "type" | "key" | "scroll" | "goto";
  x?: number;
  y?: number;
  text?: string;
  key?: string;
  dy?: number;
  url?: string;
}

export interface SignInEventResult {
  ok: boolean;
  url: string;
}

export interface SignInFinish {
  ok: boolean;
  cookies: number;
  /** The site's session cookie was found, so the credential was stored. */
  signed_in: boolean;
  message: string;
}

/** The result of the fast path (spec v4 §10f): the server filled the login
 *  form and either signed in, or handed back a live session to finish by hand
 *  (a one-time code, a captcha). */
export interface SignInSubmit {
  ok: boolean;
  signed_in: boolean;
  needs_view?: boolean;
  session?: SignInSession;
  cookies?: number;
  message: string;
}

export function startSignIn(site: string): Promise<SignInSession> {
  return apiFetch(`/api/signin/${site}`, { method: "POST" });
}

/** Fill the login form for `site` with the analyst's own details. They are
 *  used once and never stored; the response never carries them back. */
export function submitSignIn(
  site: string,
  body: { email: string; password: string },
): Promise<SignInSubmit> {
  return apiFetch(`/api/signin/${site}/submit`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function signInFrame(id: string): Promise<SignInFrame> {
  return apiFetch(`/api/signin/${id}`);
}

export function signInEvent(id: string, ev: SignInEventBody): Promise<SignInEventResult> {
  return apiFetch(`/api/signin/${id}/event`, {
    method: "POST",
    body: JSON.stringify(ev),
  });
}

export function finishSignIn(id: string): Promise<SignInFinish> {
  return apiFetch(`/api/signin/${id}/finish`, { method: "POST" });
}

export function cancelSignIn(id: string): Promise<{ ok: boolean }> {
  return apiFetch(`/api/signin/${id}`, { method: "DELETE" });
}

/**
 * End a session on the way out: leaving the view or closing the tab must not
 * leave a login page open in the shared browser. `keepalive` lets the request
 * outlive the document, and nothing waits on the answer.
 */
export function dropSignIn(id: string): void {
  void fetch(`${base}/api/signin/${id}`, { method: "DELETE", keepalive: true }).catch(
    () => undefined,
  );
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
