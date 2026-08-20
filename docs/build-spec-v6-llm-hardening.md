# Build spec v6 — LLM engine hardening

Owner direction (2026-08-20): a production incident left 8 of 19 documents
with summaries but zero claims and zero mentions, all recorded as successful
extractions. The diagnosis (§1) found the extract stage accepts any parseable
JSON as success. Separately, an audit of Filter's LLM layer (the stack's
production reference, `~/CAF/Filter/src/caf_filter/llm/`) catalogued the
hardening Vault's port left behind. This spec is the fix for the incident plus
every audited pattern the owner accepted.

Scope decisions, fixed:
- **No metered-API work.** The owner will not use the API engine; Filter's
  API path is being torn down. Leave `_api_complete` and the `api` engine
  branch exactly as they are — do not extend, do not remove, do not add
  spillover from seats to the API. All work in this spec targets the
  `claude_code` engine.
- **No research-framing preamble** (Filter's `_RESEARCH_PREAMBLE`). Refusal
  *detection* lands (§5); the preamble does not, until a refusal is actually
  observed.
- Out of scope entirely: cancellation plumbing, output-token rate limiting,
  server-side tools, refusal-fallback routing. Filter needs them; a
  sequential headless worker does not.

Design references (§) are docs/company-graph-design.md. Filter references are
file:line in `~/CAF/Filter` at commit `git -C ~/CAF/Filter rev-parse HEAD`
(pin it in your first commit message). Engine discipline as everywhere:
`llm.EngineUnavailable` pauses a stage cleanly and never burns an event's
attempts. Read docs/HANDOVER.md before starting; update it when done.

No SQL migration is needed anywhere in this spec: new telemetry rides the
existing `stage_run.summary` jsonb and `app_kv`.

---

## 1. Background: the incident this fixes

Verified on the production box (2026-08-20); this section is the record —
HANDOVER does not carry it yet (adding it is part of §10.5):

- 8 events (5 SemiAnalysis Substack posts, 3 FT pieces) ended
  `status='extracted'`, `attempts=0`, `last_error null`, with a done
  `document_summary` — and zero `claim` rows AND zero `mention` rows.
  `stage_run` counters reconcile exactly with the DB: the extract runs
  genuinely wrote nothing for those documents.
- Reproduced live: the 63 KB document (`1774ee52`), run through the exact
  extract prompt, returned 21,617 chars — the model emitted a top-level JSON
  **array** of claims instead of the `{doc_id, mentions, claims}` envelope.
  `_extract_json` (graph/llm.py:288) takes the first `{`, `raw_decode`s one
  value, ignores the rest → a single claim object → `result.get("mentions")
  or []` finds neither key → zero rows, no error, event marked extracted.
  All five Substack posts (12.6–63.5 KB, every one longer than any document
  that ever produced claims) fail this way structurally; the 3 FT empties
  were transient (same document re-run returns 14 mentions / 26 claims).
- The stuck 8, for the §10 requeue: `e532eb43`, `109e0460`, `d5764998`,
  `14985ea0`, `b5ee3047`, `c21ac0d4`, `874b495a`, `1774ee52`.

The fix has two independent layers: make the transport unable to return the
wrong shape (§2), and make the stage refuse an empty result (§3).

## 2. Structured output on the claude_code engine

Filter's CC backend proves the mechanism in production: pass a JSON schema in
`ClaudeAgentOptions.output_format`, and the CLI enforces it server-side (AJV
strict) and hands back a parsed object in `ResultMessage.structured_output`
(Filter backend_claude_code.py:365-367, 485-503, 598-640). Vault pins
`claude-agent-sdk==0.2.128`, the same version Filter ships; both
`ClaudeAgentOptions.output_format` and `ResultMessage.structured_output`
exist in it (verified with `uv run`).

### 2.1 graph/schemas.py (new)

One module holding a plain-dict JSON schema per LLM stage, strictified at
import. Port Filter's strictifier verbatim (backend_claude_code.py:329-362,
originally IVb's `schema_factory.py`): recursively set
`additionalProperties: false` on every object node that has `properties`,
and drop `discriminator`. Vault's schemas are hand-written dicts, not
pydantic, so the strictifier takes a dict and mutates a deep copy.

Schemas and their consumers (derive required keys from the prompt file and
the caller's `.get()`s; the caller line refs):

| name          | consumer                          | top-level required |
|---------------|-----------------------------------|--------------------|
| `EXTRACTION`  | pipeline/extract.py:37            | `mentions, claims` (`doc_id, defined_terms` optional — the code never reads `doc_id`) |
| `SUMMARY`     | pipeline/summarize.py:204         | `summary` (`key_points` optional — `_dispose` at summarize.py:158-164 tolerates its absence) |
| `ADJUDICATION`| pipeline/adjudicate.py:151        | `decisions` (list of `{mention_id, decision, ...}`) |
| `GARDENER`    | pipeline/gardener.py:49           | `mapping` |
| `HYPOTHESIS`  | discovery/hypothesize.py:100      | `statement, rationale, test_plan` (`budget` optional; every key has a code fallback, so required-ness here is contract tightening, which is fine) |
| `INVESTIGATE` | discovery/investigate.py:87       | `tool` (+ per-tool fields; `conclude` carries `assessment, evidence, reasoning, confidence`) |
| `VERDICT`     | discovery/verify.py:83            | `verdict, reasoning` (`surviving_evidence, confidence` optional) |

Rules for writing them:
- `claim.object` is `{surface}` or `{literal}`; `literal` is free-form JSON —
  give it a permissive `{}` schema node. `qualifiers` likewise.
- `HYPOTHESIS.test_plan.confirm/refute` must allow **empty arrays** (no
  `minItems`): hypothesis.md rule 4 tells the model to return empty when no
  falsifiable plan exists, and hypothesize.py:117-125 turns that into an
  immediate refuted verdict — a deliberate signal the schema must not block.
- Where the model may return one of several action shapes (INVESTIGATE),
  require only the discriminating key and keep the rest optional — AJV
  strict rejects `oneOf` with open branches; test it (§8).
- Keep the prose shape description in each `graph/prompts/*.md` — the model
  still reads it — but the schema is now the contract. Fix any drift between
  prompt prose and schema *toward the caller's actual reads*.

### 2.2 graph/llm.py surface

```python
def complete(prompt, model, max_tokens=8000, system=None) -> str
def complete_json(prompt, model, max_tokens=8000, schema=None, system=None)
```

- `system` goes to `ClaudeAgentOptions.system_prompt`; the prompt stays the
  user turn. SDK 0.2.128 maps unset system to `--system-prompt ""` so today's
  behavior is preserved when callers pass nothing.
- `schema` (claude_code engine): set
  `output_format={"type": "json_schema", "schema": schema}` on the options;
  on return, success requires `not result.is_error`, `subtype == "success"`
  (treat missing subtype as success for fake-transport tolerance), and
  `structured_output is not None` — otherwise raise through the normal
  failure path (after the latch checks, with subtype and a bounded result
  head in the message). Return `structured_output` directly. **No re-prompt
  retry on this path** — the CLI already retried against the schema; a
  failure here is terminal for the attempt. The existing
  `retries`/nag-append loop (llm.py:274-285) survives only for
  `schema=None` legacy calls, and must rebuild each retry from the
  *original* prompt instead of compounding notes onto the last one.
- `schema` (api engine): ignore it; keep the legacy `_extract_json` path
  unchanged. One comment noting the engine is legacy.
- Pin `thinking={"type": "adaptive"}` in `ClaudeAgentOptions` — today it is
  unset and the CLI default can shift under us (Filter
  backend_claude_code.py:496-498 and its options-shape test).
- `max_turns` stays 4.

### 2.3 Call sites

All seven callers pass their schema, and split system/user: the static
prompt file content becomes `system=`, the per-item payload the prompt.
The static half is byte-identical across a batch, which is the only shape
that can ever get prefix cache reuse on the CLI; it also separates our
instructions from untrusted fetched document text. investigate.py's per-turn
prompt keeps the hypothesis base + transcript in the user turn; only
`investigation.md` moves to system.

## 3. Extraction guards — graph/pipeline/extract.py

Schema enforcement guarantees shape, not substance. A conforming
`{"mentions": [], "claims": []}` must not be recorded as success:

- After `write_mentions`/`write_claims`, if mentions written == 0 **and**
  claims written == 0, raise `ValueError("empty extraction: 0 mentions, 0
  claims")` — the existing except-branch turns that into
  attempts+1/last_error/pending-then-failed, which is correct: visible,
  retried next cycle, and re-drivable by the events retry API. Zero claims
  with nonzero mentions stays legal (a document can genuinely assert
  nothing).
- `write_claims` returns `(written, dropped)` where dropped counts claim
  dicts skipped by the validation guards. If the model emitted claims and
  every one was dropped (`written == 0 and dropped > 0`), raise
  `ValueError(f"all {dropped} claims dropped by validation")`. Any nonzero
  dropped count goes into the run counters printed and returned by
  `run()` so it lands in `stage_run.summary`.
- Input shape, matching summarize (`split_artifact`/`parse_header` at
  summarize.py:83-113, prompt assembly at :168-177): split the connector
  header off with `summarize.parse_header`, prepend a one-line context
  (`title — source, date` from the header) to the body, cap the body at
  80000 chars (was `text[:60000]` including the header, extract.py:36).
  When the body exceeds the cap, add `extract_truncated: true` into
  `event.meta` so truncation is at least visible.
- Stamp claims with the *resolved* model (§4.4), not `config.MODELS
  ["extract"]`: `write_claims` takes the model string from the call info.

## 4. Seat engine hardening — graph/llm.py

### 4.1 RateLimitEvent is the primary quota signal

The 2026-08-20 latch miss (comment at llm.py:29-33, hotfix 777e141) is the
proof that regex over CLI prose misses when the wording changes. Filter's
2026-07-28 firing (commit 68b4b0e) is the proof the structured signal works:
its regex missed that string too, but the seat still latched because
RateLimitEvent's `rejected` flag caught it. Consume every message in the
`sdk.query()` stream, not just `ResultMessage`
(backend_claude_code.py:479, 538-547):

```python
rate_limit_event_cls = getattr(sdk, "RateLimitEvent", None)  # version-tolerant
...
elif rate_limit_event_cls is not None and isinstance(msg, rate_limit_event_cls):
    info = msg.rate_limit_info
    if info.status == "rejected":
        rate_rejected = True
    if info.utilization is not None:
        seat.last_utilization = info.utilization
    if info.resets_at is not None:
        seat.last_resets_at = info.resets_at
```

Both latch sites (no-result and is_error) become
`if rate_rejected or _QUOTA_PATTERN.search(...)`. Narrow the text matching
while you are there: quota/auth regexes run against **stderr and the SDK
exception text always**, but against `result.result` **only when
`result.is_error`** — Vault ingests documents whose subject matter is
rate limits, spend limits and reset dates, and a false quota latch stalls
every LLM stage. Auth additionally follows Filter's narrow rule
(backend_claude_code.py:611-619): latch only on `is_error` **and** one of
the literal CLI strings `"Not logged in"` / `"Failed to authenticate"`
(keep `"please run /login"`); drop the broad
`authentication|unauthorized|oauth|api key` match against arbitrary stderr —
`_call_env` sets `ANTHROPIC_API_KEY=""` (llm.py:154) and a TLS error
phrased "authentication" must not permanently kill a seat.

### 4.2 resets_at-driven re-arm

Port Filter's trio exactly — each piece exists because of a specific bug
(Filter commits b838a10, 81e9ea6):

- `_Seat.fresh_resets_at()`: return `last_resets_at` only when still in the
  future. The field is sticky; a benign five-hour warning must not hand a
  later weekly latch an already-elapsed reset (backend_claude_code.py:151-161).
- `trip(reason, kind, resets_at=None)` stores it on the latch.
- `retry_due()`: quota → `max(resets_at + 30, latched_at + 30)` when a reset
  is known, else `latched_at + CAF_VAULT_CC_RETRY_PROBE_S` (3600) as today.
  Auth → `latched_at + CAF_VAULT_CC_AUTH_RETRY_PROBE_S` (default 21600) —
  auth latches stop being permanent-until-restart; a stale token replaced on
  the server should not need a container bounce to be noticed.
- `_maybe_rearm()` fixes two existing bugs while you are in it: it must hold
  `_SEATS_LOCK`, and it must clear `reason`/`latched_at` (today a re-armed
  seat reports a stale reason through `seat_status`).

### 4.3 Latch state survives restarts (app_kv)

`_SEATS` is process memory; a deploy or OOM during a quota window resets
every latch and the dashboard reports all-clear until the next call burns a
subprocess rediscovering the wall. Mirror latch state into `app_kv` key
`seat_latches`: on `trip()` and on re-arm, upsert
`{seat: {kind, reason, latched_at, resets_at}}`; `_seats()` initialisation
re-applies any stored latch whose `retry_due()` is still in the future.
Best-effort — a DB error here is printed and ignored (the engine must work
without a DB, e.g. in unit tests: import `db` lazily inside the helper and
swallow all exceptions).

### 4.4 Per-call usage record

Filter records usage *before* the failure branch so failed calls still book
their quota draw (backend_claude_code.py:595, 642-674; test "a failed call
still drew quota"). Vault throws all of it away at llm.py:218. Add a
module-level bounded log:

```python
_CALL_LOG = deque(maxlen=1000)   # dicts, appended by _cc_call

def take_call_log() -> list[dict]:   # drain-and-return, cli.stage() calls it
def last_call_info() -> dict | None  # most recent entry (worker is sequential)
```

Each `_cc_call` appends, success or failure:
`{stage-agnostic: input_tokens, output_tokens, cache_read_input_tokens,
cache_creation_input_tokens, total_cost_usd, duration_ms, session_id,
model, seat, utilization, ok}`. `model` is the **resolved** id: capture
`AssistantMessage.model` while streaming, fall back to the first key of
`ResultMessage.model_usage` (backend_claude_code.py:536-537, 652-653).
Vault's product is provenance; `claim.extractor` and
`document_summary.model` switch to this value via `last_call_info()`
(extract.py §3; summarize's DONE_SQL executes at summarize.py:207-210).

### 4.5 Failure text quality and token scrub

- The `is_error` branch (llm.py:214-217) gains the stderr tail (last 5
  lines, capped 500 chars, Filter's `_stderr_tail`) and `raise ... from err`
  — today `event.last_error` stores a 300-char CLI blurb and the real
  diagnosis is discarded.
- A teardown error after a valid result keeps the result and prints a
  warning (Filter backend_claude_code.py:598-608, commit b188a33) — do not
  discard a good pass over transport cleanup.
- `_scrub(text)`: replace every non-empty seat token value with
  `<seat-token>` in anything that leaves the process — `trip()` reasons,
  raised exception messages, `seat_status()`. CLI stderr is not a channel we
  control, and `event.last_error` is rendered in the UI. This closes the one
  exemption from the repo rule that credential values never appear in logs
  or error messages (CLAUDE.md; graph/credentials.py enforces it for site
  cookies).

### 4.6 Boot validation and alias table

- Add to `CC_MODEL_ALIASES`, mirroring Filter's map exactly
  (backend_claude_code.py:49-57): `claude-sonnet-4-6 → "sonnet"`,
  `claude-opus-4-7 → "opus"`, `claude-opus-4-8 → "opus"` — bare family
  aliases, resolved server-side; there are no dotted variants and no
  `claude-opus-4-6` entry.
- `llm.validate_models(models: dict)`: raise `ValueError` naming the config
  key, the offending value and the supported set for any value not in
  `CC_MODEL_ALIASES`. Called from `cmd_loop` and `cmd_run` before the first
  cycle. Today a typo in `CAF_MODEL_EXTRACT` in the server .env fails
  per-event, burning two attempts per document with an opaque CLI error;
  after this it fails at boot with a message.

### 4.7 seat_status and the ops surface

`seat_status()` gains `resets_at` (ISO 8601 or null), `utilization` (float
or null), and `retry_at` (from `retry_due()`, ISO 8601 or null). The worker
heartbeat already embeds `seat_status()`, so the API side is free. Frontend:
the dashboard seats card (frontend/src/pages/dashboard.tsx:117-145) shows,
for a latched seat with a known reset, `resets 21 Aug, 03:00 UTC` (sentence
case, copy rules build-spec-v2-frontend.md §7 apply; no new tooltip
strings). Utilization is not shown yet — data first.

## 5. Error taxonomy — transient failures must not burn attempts

Vault has two classes today: `EngineUnavailable` (pause batch, attempts
untouched) and everything else (attempts+1, two strikes → permanently
`failed`). A 20-minute timeout or a process death currently costs documents
permanently. Filter splits three ways (backend_claude_code.py:448-453 plus
the API-path transport retries — the API half does not apply here).

Add `class TransientError(RuntimeError)` to llm.py, raised by `_cc_call` for:
- the anyio timeout (raise at llm.py:192-193, currently RuntimeError),
- ended-without-result when **no** latch matched (llm.py:210-213) — process
  death, crash, transport teardown.

Stage handling — catch `llm.TransientError` *before* the generic handler,
leave state and attempt counters untouched, record the message so it is
visible, continue. Unlike `EngineUnavailable` it does not break the batch.
The shape differs by stage:
- Per-item chains exist in extract (extract.py:62-70, the template),
  summarize, hypothesize, investigate and verify: roll back the savepoint,
  leave status/attempts as they were, write the message to `last_error` (or
  the summary row's `error`), move to the next item.
- adjudicate has **no** try/except today — its one batched call at
  adjudicate.py:151 propagates straight to `cli.stage()`. Wrap that single
  call: on TransientError return `{"paused": False, "transient": 1}` and
  leave the queue untouched (er_queue rows keep their own `passes` counter;
  there is nothing per-item to protect).
- gardener's existing whole-stage catch (gardener.py:48-61) already
  records-and-returns without burning anything — leave it, just let
  TransientError fall into it.

Also fix the drain double-strike while here: `cli._extract_drain`
(cli.py:321-333) re-runs `extract.run` while progress is made, so an event
that fails attempt 1 is re-selected in the same drain and reaches `failed`
within one cycle, and the drain's `failed` counter double-counts (stage_run
"failed: 10" was 5 events). `run()` gains an `exclude` set of event ids the
drain accumulates — covering **both** failed items **and** TransientError
items, since a transient item rolls back to `pending` with attempts
untouched and would otherwise be re-selected the same drain (at up to a
20-minute timeout per attempt). Second strikes then happen a cycle later,
which is the intent of having two, and each item is attempted at most once
per drain.

## 6. Stage telemetry — graph/cli.py

- `stage()` (cli.py:492) drains `llm.take_call_log()` after each stage and
  folds a rollup into the `stage_run.summary` dict it already writes via
  `_stage_finish` (cli.py:381-392): `{"llm": {"calls": n, "input_tokens":
  ..., "output_tokens": ..., "cache_read_tokens": ..., "cost_usd": ...,
  "duration_ms": ...}}`. This is the answer to "which stage burned the
  weekly pool", recorded even for failed calls.
- `_extract_drain` always includes `"paused": False` when not paused —
  today only that drain omits the key unless true (`_summarize_drain`
  already initializes it, cli.py:346), so an extract pause is
  indistinguishable from idle in `stage_run`. One line.

## 7. Adjudicate bug (live, unrelated to the rest)

Production hit `ProgrammingError: cannot adapt type 'dict' using placeholder
'%s'` in the adjudicate stage on 2026-08-20 02:15 (one cycle lost; a later
cycle succeeded). Every dict-valued write in adjudicate.py and the
resolve.py helpers it calls is already `Jsonb()`-wrapped (adjudicate.py:84,
:106, :178, :183; resolve.py:42, :54) — do **not** go hunting for a missing
wrapper. The culprit is unvalidated LLM output landing in a **scalar**
placeholder: the model emitted an object where a scalar was expected —
`d.get("confidence")` (adjudicate.py:72) into the float columns at :91/:109/
:114, or `d.get("entity_hint")` (:93) into `canonical_name` at :104-106 are
the candidates. Fix by coercing/validating the scalar decision fields before
any execute (confidence → float with fallback, entity_hint → str or None);
the §2 ADJUDICATION schema prevents recurrence at the source. Add a
regression test that feeds a decision with object-valued `confidence` and
`entity_hint` through the mocked stage.

## 8. Tests

New `tests/test_llm.py`, built on the seam Filter proved
(tests/test_cc_backend.py:86-109 — copy the shape, same SDK version):

- `llm._reset_seats()` test-only hook (clears `_SEATS`, `_CALL_LOG`).
- `_install_query(monkeypatch, messages, stderr_lines=(), raise_after=None)`:
  monkeypatches `claude_agent_sdk.query` with an async generator that yields
  scripted message objects, pushes lines through `options.stderr`, and can
  raise *after* yielding the ResultMessage — the real CLI contract (it exits
  non-zero after an `is_error` result; Filter commit b188a33 exists because
  the latch was unreachable without modelling this).

Minimum cases:
1. structured success: `output_format` lands on the options; `schema=`
   returns `structured_output` verbatim; no retry loop entered.
2. `structured_output is None` with `subtype='error_max_structured_output_
   retries'` → raises, message carries the subtype; exactly one call.
3. quota latch via RateLimitEvent `status=="rejected"` with **no** regex
   match anywhere → seat latches, rotation proceeds to seat 2.
4. quota latch via regex on stderr (no RateLimitEvent) — the 2026-08-20
   string pinned verbatim.
5. quota vocabulary inside a *successful* result's text does **not** latch.
6. auth latch: `is_error` + `"Not logged in"` latches; `"authentication"`
   in stderr with a healthy result does not.
7. unknown error: no latch, no rotation, raises to caller.
8. all seats latched → `EngineUnavailable` with no query spawned.
9. `resets_at` re-arm: latch with future reset → `retry_due()` honours it
   with the 30 s grace; stale (past) reset falls back to the probe interval;
   auth re-arms after `CAF_VAULT_CC_AUTH_RETRY_PROBE_S`.
10. timeout and died-without-result (no latch text) raise `TransientError`;
    a latch match still wins over TransientError.
11. usage: failed call still appends to the call log; resolved model comes
    from AssistantMessage over the requested alias.
12. token scrub: seat token value appears in neither the raised message,
    the trip reason, nor `seat_status()`.
13. teardown error after a valid result returns the result.
14. `validate_models` rejects a typo naming the key.

New `tests/test_schemas.py`: the strictifier closes every object node; every
stage schema, strictified, validates that stage's existing mocked fixture
output (the `EXTRACTION` constant in tests/test_pipeline.py etc.) using
`jsonschema` if it is already a transitive dependency, else structural
asserts.

Pipeline tests: mocks of `llm.complete_json` gain the `schema=`/`system=`
kwargs (accept `**kw`); add extract tests for the empty-result guard
(0/0 raises, mentions-only passes), the all-claims-dropped guard, the
truncation flag, and the drain single-strike behavior. Existing suite rules
apply (scoped assertions, no readable pending events left behind).

## 9. What the implementing agent must not do

- Do not touch `_api_complete` beyond adding the one legacy comment.
- Do not add an engine crossover (seats → API) in any form.
- Do not loosen the empty-extraction guard to "warn only".
- Do not persist or log any credential or token value; scrub first
  (§4.5). Diagnostic output on the box: statuses, counts, names only.
- Do not edit applied migrations; this spec needs no migration.
- Do not raise `retries` on the legacy JSON path as a "fix" for anything.

## 10. Verification and rollout

1. Local: `uv run pytest` green (~280 existing + new); `uvx ruff check
   graph tests` clean on touched files; `cd frontend && npm run build` for
   the dashboard card.
2. Live overlay verify on the box before deploy. The procedure (not yet in
   HANDOVER — add it there in step 5): ssh `alfred@34.126.95.106`; from the
   repo root `COPYFILE_DISABLE=1 tar czf overlay.tgz <changed files>`
   (without `COPYFILE_DISABLE` macOS tar adds `._*` AppleDouble files that
   break things on the box), scp it over, then inside the container
   (`caf-caf-vault-worker-1`): copy `/app/graph` and `/app/schema` to
   `/tmp/live/`, `docker cp` the tarball in, untar over `/tmp/live`, run
   diagnostics with `PYTHONPATH=/tmp/live python`, and remove `/tmp/live`
   afterwards. With the overlay live, run the real extract path with
   `schema=` against one stuck Substack document (`874b495a`) and one FT
   document. Success = structured_output envelope with nonzero mentions and
   claims *counted*, nothing written (run inside a rolled-back
   transaction), and no credential or token values printed — statuses,
   counts and names only. Seats: seat 1 may still be quota-latched until
   2026-08-21 03:00 UTC; rotation should carry the probe.
3. Deploy: commit → push → bump the Vault submodule in `~/CAF` → push →
   `deploy-YYYYMMDD-HHMMSS` tag (single tag push, local time).
4. Post-deploy: requeue the stuck 8 —
   `update event set status='pending' where left(event_id::text, 8) in
   ('e532eb43','109e0460','d5764998','14985ea0','b5ee3047','c21ac0d4',
   '874b495a','1774ee52')` (they have no claim/mention rows to clean up),
   trigger a run, then verify: every one of the 8 ends `extracted` with
   mentions > 0 (`1774ee52` is ~63.7k chars, under the new 80k cap, so no
   truncation flag is expected on any of them); `stage_run` shows the new
   `llm` rollup; `/vault/api/status` seats carry `resets_at`/`retry_at`.
5. Update docs/HANDOVER.md — current state, verification evidence, the §1
   incident record, and the overlay-verify procedure from step 2 (neither
   is in HANDOVER today) — and close with a "Handover:" commit, per repo
   convention.

Suggested build order (dependencies, not ceremony): §8 seam first (nothing
else is verifiable without it) → §2 llm surface + schemas → §4.1–4.2 in one
change to the message loop → §3 extract guards → §5 taxonomy + drain fix →
§4.4/§6 usage → §4.3, 4.5–4.7 small guards → §7 → §10.
