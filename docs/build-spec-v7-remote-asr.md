# Build spec v7 — remote ASR (off-site whisper on a Mac)

## §1 Why

Whisper on the production box is the wrong trade: the e2-standard-2 runs
faster-whisper `small` at ~1.1x realtime on 75% of its single physical core
(measured 2026-08-19 on a real episode), stalling every other worker stage for
the length of the audio, and `small` is the weakest model we would want for
finance audio — names and tickers are what ER keys on. An M4 Mac mini runs
mlx-whisper `large-v3-turbo` at ~16x realtime in 640 MB. So: the worker stops
transcribing when `CAF_ASR=remote` and instead enqueues transcription jobs;
an agent on the Mac leases jobs over the existing SSH trust, transcribes
locally, and posts the text back; the server ingests through the normal event
envelope so provenance, dedup and lineage are identical to local ASR.

Everything else about ASR is unchanged: `CAF_ASR=off|auto|mlx|faster-whisper`
behave exactly as before; `remote` is a new value with meaning only in
`podcast.poll` and `youtube` polling. The web container stays `CAF_ASR=off`
(it never transcribes; it only serves the queue API).

## §2 Schema (`schema/014_asr_job.sql`)

One table, `asr_job`. A job is one audio item awaiting a transcript:

- `job_id uuid pk`, `source_id -> source`, `connector` (`podcast`|`youtube`),
  `external_id` (enclosure URL / video id), `title`, `published_at text`
  (carried through verbatim to `envelope.ingest`).
- `audio_url` — set for podcasts; the agent downloads the enclosure itself
  (no credentials involved, only the browser UA).
- `audio_path` — set for YouTube; the worker downloads audio with its cookies
  (credentials never leave the server, per the v4 rules) into the spool
  (`config.ASR_SPOOL`, prod `/data/asr_spool` on the shared volume) and the
  agent fetches it from the queue API.
- `meta jsonb` — `{"doc_prefix": ..., "event_meta": {...}}`: everything the
  completion handler needs to build the artifact and event without knowing
  the connector. `doc_prefix` is the exact document header the connector
  would have written (`# title: ...\n---\n`); completion appends the
  transcript and a newline, so a remote transcript is byte-identical in form
  to a local one.
- `status` `pending|leased|done|error`, `attempts`, `leased_by`,
  `lease_expires`, `error`, `event_id`, `created_at`, `done_at`.
- Unique `(connector, external_id)` makes enqueue idempotent. A terminal
  `error` row deliberately blocks re-enqueue (same role as the YouTube skip
  list); delete the row to retry.

## §3 Queue module (`graph/asr_queue.py`)

Library functions, no commits (webapp/worker callers commit):

- `enqueue(con, ...) -> job_id | None` — insert, `on conflict do nothing`;
  an optional `link_id` ties the job to a link_queue row.
- `get_external(con, connector, external_id)` — the item's job row, for the
  connectors' pre-filters: under remote, any row means never download or
  enqueue again; under a local engine, a pending/leased row means the agent
  owns the item and transcribing it locally would double-ingest it.
- `lease(con, worker) -> row | None` — oldest job with `status='pending'` or
  an expired lease, `for update skip locked` (safe under concurrent agents),
  sets `leased/leased_by/lease_expires (now + CAF_ASR_LEASE_MINUTES, default
  60)` and increments `attempts`.
- `complete(con, job_id, text) -> (event_id, is_new)` — builds
  `doc_prefix + text + "\n"`, calls `envelope.ingest` with the stored
  connector/meta/published_at, marks the job `done`, and resolves the job's
  link_queue row when it has one. Accepted from any non-`done` status: a
  transcript that arrives after a lease expired (or after a terminal error)
  is still the transcript. Spool files are removed by the API layer AFTER
  its commit (`cleanup_spool`) — never inside the transaction, where a
  failed commit would leave a live job without its only audio copy; a crash
  between commit and unlink leaves an orphan file, the benign direction.
- `fail(con, job_id, error)` — back to `pending` behind a retry backoff
  (`not_before = now() + CAF_ASR_RETRY_MINUTES`, default 15, so one agent
  pass cannot burn the whole attempt budget on the same broken download), or
  `error` once `attempts >= CAF_ASR_MAX_ATTEMPTS` (default 3; spool file
  unlinked then).
- `counts(con)` — by status, surfaced in `/api/status` under
  `counts.asr_jobs`.

## §4 Connector behaviour under `CAF_ASR=remote`

`podcast.poll`: the event-level dedup check runs as today; then instead of
download+transcribe, the episode is enqueued (`audio_url` = enclosure URL,
doc prefix identical to the inline format). New `queued` key in the counts.

`youtube.transcript`: the caption path is unchanged (captions never need
ASR). On the whisper fallback, the same guards run (duration cap, live/no
duration), the audio is downloaded server-side exactly as today (cookies stay
server-side), then moved to the spool — named by video id, so enqueueing
needs no post-insert rename and there is no crash window between a job row
and its file — and the function returns `("", "remote", info)` with
`info["asr_audio_path"]` set. `youtube.poll` enqueues from that (it has the
feed entry and source), counts it `queued`, and does NOT put the video on
the per-source skip list. A pre-filter skips entries that already have a job
row.

The one-off link queue: pasted YouTube links are processed by the WORKER
(the web container defers them), which in production runs with
`CAF_ASR=remote`, so `ingest_video` is a first-class remote path, not a
dev-only one. It enqueues with the row's `link_id`, returns `queued: True`,
and `links.process_one` keeps the row `queued` without burning an attempt;
every later cycle costs one select (the job pre-check runs before any
yt-dlp work). When the agent posts the transcript, `asr_queue.complete()`
resolves the link row to `done` with the event. A job already `done` hands
the link its event; a terminal `error` job fails the link with
"Transcription failed."

Engine switches: under a local engine, both polls and the link queue skip
items whose job is pending/leased — the agent owns them, and transcribing
locally too would ingest the same audio twice.

Completion creates the event with the same `meta` the inline path writes
(`transcript_source: "whisper"`), so downstream (extract, documents pages)
cannot tell remote from local transcription.

## §5 Queue API (in `graph/webapp.py`)

Four endpoints under `/api/asr`, all guarded by a shared token: header
`X-CAF-ASR-Token` must equal env `CAF_ASR_TOKEN`, compared with
`secrets.compare_digest`; when the env is unset every request is 403 (the
feature is off, not open). The token authorizes the transcription agent
only; it is not a user credential and grants nothing else.

- `POST /api/asr/lease` `{worker}` -> job JSON, or 204 when the queue is
  empty. The job JSON carries `audio_url` (podcast) or `audio: true`
  (server-held).
- `GET /api/asr/jobs/{id}/audio` -> the spool file (leased jobs only; the
  resolved path must sit under `config.ASR_SPOOL`, same containment rule as
  `artifacts.read_bounded`).
- `POST /api/asr/jobs/{id}/complete` `{text}` -> `{event_id, is_new}`.
  Blank text is 400. `done` jobs answer 409.
- `POST /api/asr/jobs/{id}/fail` `{error}` -> `{status, attempts}`. `done`
  jobs answer 409. The error string is stored for the operator; it must
  never contain credential material (the agent holds none besides its own
  token, which it never prints).

Transport in production: the vault web port is already published on the
server's loopback (`127.0.0.1:8600`), so the agent reaches it through an SSH
tunnel under the existing key trust — no new ingress, no client-cert
extraction, and the token is never carried over plain HTTP beyond the tunnel.

## §6 The agent (`graph/asr_agent.py`, `python -m graph.asr_agent`)

A deliberately small poller that runs on the Mac from this repo checkout
(`uv run python -m graph.asr_agent`). No database access; only the queue API.

- `--server URL` or `--ssh user@host` (spawns `ssh -N -L <free local
  port>:127.0.0.1:8600`, waits for the port, restarts the tunnel if it
  drops — always on the SAME local port, since the Api's base URL is built
  from it; `--remote-port` overrides 8600). Token from `--token`,
  `--token-file`, or `CAF_ASR_TOKEN`.
- Loop: lease → download audio (enclosure URL with the podcast UA and the
  same `MAX_AUDIO_BYTES` cap, or the job's server audio endpoint) →
  `podcast.transcribe` (engine from `CAF_ASR`/`--engine`; `auto` resolves to
  mlx on this Mac; `off`/`remote` are refused at startup) → post the
  transcript. Download/transcribe failures post `fail` and carry on. A
  COMPLETION failure is different: the finished transcript is minutes of
  work, so posting retries (3 tries, growing backoff), and if the server
  still refuses, the job is NOT failed — the lease expires and the work is
  redone, rather than a server-side blip burning the job's attempt budget.
  204 sleeps `--poll` seconds (default 60). `--once` drains the queue and
  exits (used by tests and the e2e check).
- One line per job on stdout; never prints the token; exits cleanly on
  SIGINT/SIGTERM (the launchd stop path).

launchd (owner's Mac): `~/Library/LaunchAgents/com.caf.vault-asr.plist`,
`KeepAlive`, runs the module with `--ssh alfred@34.126.95.106
--token-file ~/.caf/vault-asr-token` (token file mode 600; `--token-file`
keeps the token out of the plist and `ps` output). Jobs simply wait while
the Mac is asleep; podcasts are not latency-critical.

## §7 Config and deploy

- `graph/config.py`: `ASR_SPOOL` (`CAF_ASR_SPOOL`, default
  `<artifacts parent>/asr_spool`), `ASR_LEASE_MINUTES`, `ASR_MAX_ATTEMPTS`.
- Compose (`~/CAF/docker-compose.yml`): `CAF_ASR_TOKEN: ${CAF_ASR_TOKEN:-}`
  on both vault services (web checks it; worker doesn't need it but gets it
  for symmetry); worker `CAF_ASR: ${CAF_VAULT_ASR:-off}` is already there —
  production turns this on with `CAF_VAULT_ASR=remote` in `.env`, plus a
  generated `CAF_ASR_TOKEN`. `.env.example` documents both.
- The spool lives on the shared `vault-data` volume so web (serves audio)
  and worker (writes audio) see the same files. Spool files are removed on
  completion and on terminal failure; an interrupted worker can leave an
  orphan, which is bounded (YouTube audio is capped by `max_minutes`) and
  cleaned by hand until a sweeper is worth having.

## §8 Verification bar

- Unit/API tests (`tests/test_asr_remote.py`): enqueue idempotence for both
  connectors, off/auto behaviour unchanged, lease/expiry/attempts flow,
  token gate (403 without/with wrong token; no token configured = all 403),
  completion doc byte-format vs the inline path, spool containment, the
  agent loop end-to-end against the app with a faked transcriber.
- Live e2e on the dev Mac before shipping: a scratch database, the real
  Unhedged feed, `CAF_ASR=remote` worker pass, the real agent with real
  mlx-whisper against the local server, and the resulting document checked
  on the API.
- Production: deploy, set env, install launchd, confirm the agent idles
  against the empty queue (production sources are the owner's population
  run; nothing is preloaded).
