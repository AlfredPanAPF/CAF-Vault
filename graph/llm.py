"""LLM interface with two engines.

Engine selection via CAF_VAULT_LLM_ENGINE (default "claude_code"):

- "claude_code": claude-agent-sdk on Claude subscription seats. Seats are
  CLAUDE_CODE_OAUTH_TOKEN_VAULT_1..N env vars — additive, one .env line each.
  With no token set the bundled CLI uses the local `claude` login, which is
  the dev path on our Macs. Pattern ported from Filter's
  backend_claude_code.py — the stack's production reference for this engine
  (hardening spec: docs/build-spec-v6-llm-hardening.md).
- "api": Anthropic API with ANTHROPIC_API_KEY. Legacy engine, kept as the
  explicit fallback; not extended (spec v6 scope).

Public surface used by the pipeline: complete() and complete_json(). Both
are engine-agnostic and take an optional system turn; complete_json takes an
optional JSON schema that the claude_code CLI enforces server-side.

Failure taxonomy (spec v6 §5): EngineUnavailable pauses a stage cleanly and
never burns an event's attempts; TransientError (timeout, process death)
leaves per-item state untouched and moves on; anything else is a real
per-item failure.
"""
import asyncio
import json
import os
import re
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone

# Each CLI subprocess is ~300MB; cap concurrency process-wide.
_SEMAPHORE = threading.Semaphore(
    int(os.environ.get("CAF_VAULT_CC_MAX_SUBPROCESSES", "2")))

# Matches every quota phrasing the CLI has used. It changed once already —
# "You've hit your weekly limit · resets Aug 21, 3am (UTC)" (2026-08-20,
# production) matched none of the old alternatives, so the seat never
# latched and extract/summarize burned two attempts per item on a dead
# seat. "hit your … limit" and "resets <Month> <day>" cover that family.
# The regex is the FALLBACK; the primary quota signal is the SDK's
# RateLimitEvent rejected flag (spec v6 §4.1) — Filter's 2026-07-28 firing
# missed the regex too and latched only because of the structured event.
_QUOTA_PATTERN = re.compile(
    r"rate.?limit|usage limit|spend limit|weekly limit"
    r"|hit your .{0,30}limit|limit reached"
    r"|resets at|resets \w{3,9}\.? \d", re.IGNORECASE)

# Auth latches narrowly (Filter's rule): only on an is_error result carrying
# one of these literal CLI strings. The old broad regex
# (authentication|unauthorized|oauth|api key) against arbitrary stderr could
# permanently kill a seat on a TLS error phrased "authentication" —
# _call_env scrubs ANTHROPIC_API_KEY, so those words never mean our auth.
_AUTH_STRINGS = ("Not logged in", "Failed to authenticate", "please run /login")

# Re-arm floor: even a mis-reported reset can never make a seat due in the
# past (Filter commit 81e9ea6).
_REARM_GRACE_S = 30.0

# The CLI accepts aliases only; unknown names pass through untouched at call
# time and are rejected at boot by validate_models. Mirrors Filter's map:
# bare family aliases, resolved server-side; no dotted variants.
CC_MODEL_ALIASES = {
    "claude-fable-5": "fable",
    "claude-opus-5": "opus",
    "claude-opus-4-8": "opus",
    "claude-opus-4-7": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-haiku-4-5": "haiku",
}


class SeatError(RuntimeError):
    """This seat is dead (quota/auth); the call should rotate to the next."""


class EngineUnavailable(RuntimeError):
    """No live path to a model right now (all seats latched, or no API key).

    Callers treat this as "pause and retry later", never as a per-item
    failure — a quota window must not burn attempts across the corpus."""


class TransientError(RuntimeError):
    """The call died without a verdict (timeout, process death, transport
    teardown). Callers leave the item's state and attempt counters untouched,
    record the message, and move to the next item — unlike EngineUnavailable
    it does not break the batch (spec v6 §5)."""


class _Seat:
    def __init__(self, index, token):
        self.index = index
        self.token = token
        self.dead = False
        self.kind = None          # "quota" | "auth"
        self.reason = ""
        self.latched_at = None
        self.resets_at = None            # epoch carried by the latch
        self.last_utilization = None     # last RateLimitEvent signal seen
        self.last_resets_at = None

    def fresh_resets_at(self):
        """last_resets_at, but only when it is still in the future.

        The field is sticky (any RateLimitEvent writes it, e.g. a benign
        five-hour warning) — a latch tripped later by a DIFFERENT limit must
        not inherit an already-elapsed reset, or the seat would re-arm on
        the very next check instead of the probe interval."""
        if self.last_resets_at and self.last_resets_at > time.time():
            return self.last_resets_at
        return None

    def trip(self, reason, kind, resets_at=None):
        self.dead = True
        self.kind = kind
        self.reason = _scrub(reason or "")
        self.resets_at = resets_at
        self.latched_at = time.time()
        print(f"llm: seat {self.index} latched ({kind}): {self.reason[:160]}")
        _store_latches()

    def retry_due(self):
        """Epoch when this latched seat may probe again; None when live.

        Quota honours a known reset (+grace, floored at latch time); without
        one it probes hourly. Auth probes every CAF_VAULT_CC_AUTH_RETRY_PROBE_S
        (6h) — a stale token replaced on the server should not need a
        container bounce to be noticed."""
        if not self.dead:
            return None
        if self.kind == "auth":
            probe = float(os.environ.get(
                "CAF_VAULT_CC_AUTH_RETRY_PROBE_S", "21600"))
            return (self.latched_at or 0) + probe
        if self.resets_at:
            return max(self.resets_at + _REARM_GRACE_S,
                       (self.latched_at or 0) + _REARM_GRACE_S)
        probe = float(os.environ.get("CAF_VAULT_CC_RETRY_PROBE_S", "3600"))
        return (self.latched_at or 0) + probe


_SEATS = None
_SEATS_LOCK = threading.Lock()

# Per-call usage records (spec v6 §4.4): every _cc_call that saw a
# ResultMessage appends one dict, success or failure — a failed call still
# drew quota. cli.stage() drains this after each stage into stage_run.summary.
_CALL_LOG = deque(maxlen=1000)


def take_call_log() -> list[dict]:
    """Drain and return the per-call usage records (cli.stage() calls it)."""
    out = []
    while True:
        try:
            out.append(_CALL_LOG.popleft())
        except IndexError:
            return out


def last_call_info() -> dict | None:
    """Most recent call record (the worker is sequential, so right after a
    complete()/complete_json() this is that call)."""
    return _CALL_LOG[-1] if _CALL_LOG else None


def _scrub(text):
    """Replace every seat token value with <seat-token> in anything that
    leaves the process. CLI stderr is not a channel we control and
    event.last_error is rendered in the UI; the repo rule is that credential
    values never appear in logs or error messages."""
    if not text:
        return text
    for s in (_SEATS or []):
        if s.token:
            text = text.replace(s.token, "<seat-token>")
    return text


def _seats():
    global _SEATS
    with _SEATS_LOCK:
        if _SEATS is None:
            seats = [_Seat(1, os.environ.get(
                "CLAUDE_CODE_OAUTH_TOKEN_VAULT_1", "").strip())]
            i = 2
            while True:
                token = os.environ.get(
                    f"CLAUDE_CODE_OAUTH_TOKEN_VAULT_{i}", "").strip()
                if not token:
                    break
                seats.append(_Seat(i, token))
                i += 1
            _load_latches(seats)
            _SEATS = seats
        return _SEATS


def _reset_seats():  # tests only
    global _SEATS
    with _SEATS_LOCK:
        _SEATS = None
        _CALL_LOG.clear()


def _store_latches():
    """Mirror latch state into app_kv (spec v6 §4.3): process memory resets
    on deploy/OOM, and until this the dashboard reported all-clear mid-quota
    window. Best-effort — the engine must work without a DB (unit tests)."""
    try:
        from psycopg.types.json import Jsonb

        from . import db
        payload = {str(s.index): {
            "kind": s.kind, "reason": s.reason,
            "latched_at": s.latched_at, "resets_at": s.resets_at,
        } for s in (_SEATS or []) if s.dead}
        with db.connect() as con:
            con.execute(
                "insert into app_kv (key, value, updated_at) values "
                "('seat_latches', %s, now()) on conflict (key) do update "
                "set value=excluded.value, updated_at=now()", (Jsonb(payload),))
            con.commit()
    except Exception as e:
        print(f"llm: seat latch mirror skipped ({e!r})")


def _load_latches(seats):
    """Re-apply stored latches whose retry_due() is still in the future.
    Best-effort, same rules as _store_latches."""
    try:
        from . import db
        with db.connect() as con:
            row = con.execute(
                "select value from app_kv where key='seat_latches'").fetchone()
        stored = row["value"] if row and isinstance(row["value"], dict) else {}
        now = time.time()
        for s in seats:
            entry = stored.get(str(s.index))
            if not isinstance(entry, dict) or not entry.get("kind"):
                continue
            s.dead = True
            s.kind = entry.get("kind")
            s.reason = str(entry.get("reason") or "")[:500]
            s.latched_at = entry.get("latched_at")
            s.resets_at = entry.get("resets_at")
            due = s.retry_due()
            if due is not None and now >= due:
                s.dead, s.kind, s.reason = False, None, ""
                s.latched_at, s.resets_at = None, None
            else:
                print(f"llm: seat {s.index} still latched from stored state "
                      f"({s.kind})")
    except Exception as e:
        print(f"llm: seat latch restore skipped ({e!r})")


def _maybe_rearm():
    """Re-arm latched seats whose reset (or probe interval) has passed.
    Holds _SEATS_LOCK against a concurrent trip and clears reason/latched_at
    so seat_status never reports a stale latch on a live seat."""
    seats = _seats()
    now = time.time()
    rearmed = False
    with _SEATS_LOCK:
        for s in seats:
            due = s.retry_due()
            if s.dead and due is not None and now >= due:
                s.dead, s.kind, s.reason = False, None, ""
                s.latched_at, s.resets_at = None, None
                rearmed = True
                print(f"llm: seat {s.index} re-armed for probe")
    if rearmed:
        _store_latches()


def seat_status() -> list[dict]:
    """Read-only view of the seat pool for the ops surface (spec v2 §2,
    v6 §4.7). JSON-safe: epochs are ISO 8601 UTC (or None), reason is
    truncated and token-scrubbed."""
    def iso(epoch):
        return (datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                if epoch else None)
    return [{
        "seat": s.index,
        "has_token": bool(s.token),
        "latched": s.dead,
        "kind": s.kind,
        "reason": _scrub(s.reason or "")[:160],
        "latched_at": iso(s.latched_at),
        "resets_at": iso(s.resets_at),
        "retry_at": iso(s.retry_due()),
        "utilization": s.last_utilization,
    } for s in _seats()]


def validate_models(models: dict) -> None:
    """Boot-time model check (spec v6 §4.6), called by cmd_run/cmd_loop
    before the first cycle. A typo in a CAF_MODEL_* env var used to fail
    per-event with an opaque CLI error, burning two attempts per document.
    claude_code engine only — the api engine takes raw model ids."""
    engine = os.environ.get("CAF_VAULT_LLM_ENGINE", "claude_code").strip()
    if engine != "claude_code":
        return
    for key in sorted(models):
        value = models[key]
        if value not in CC_MODEL_ALIASES:
            raise ValueError(
                f"config.MODELS[{key!r}] = {value!r} (CAF_MODEL_{key.upper()}) "
                f"cannot run on the claude_code engine; supported: "
                f"{sorted(CC_MODEL_ALIASES)}")


def _call_env(seat, max_tokens):
    """Per-call env overrides. The API-key scrub comes LAST — an inherited
    key outranks OAuth and silently bills the metered account."""
    env = {
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_AUTO_COMPACT": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_tokens),
    }
    if seat.token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat.token
    state_dir = os.environ.get("CAF_VAULT_CC_STATE_DIR", "").strip()
    if state_dir:
        seat_dir = os.path.join(state_dir, f"seat-{seat.index}")
        os.makedirs(seat_dir, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = seat_dir
        env["HOME"] = seat_dir
    getuid = getattr(os, "getuid", None)
    if getuid is not None and getuid() == 0:
        # The CLI refuses bypassPermissions as root; containers run as uid 0.
        env["IS_SANDBOX"] = "1"
    env["ANTHROPIC_API_KEY"] = ""
    env["ANTHROPIC_AUTH_TOKEN"] = ""
    return env


def _workdir():
    path = (os.environ.get("CAF_VAULT_CC_WORKDIR", "").strip()
            or os.path.join(tempfile.gettempdir(), "caf-vault-cc-work"))
    os.makedirs(path, exist_ok=True)
    return path


def _stderr_tail(stderr_lines):
    if not stderr_lines:
        return ""
    return " | stderr tail: " + "\n".join(list(stderr_lines)[-5:])[-500:]


async def _cc_call(seat, prompt, model, max_tokens, schema=None, system=None):
    import anyio
    import claude_agent_sdk as sdk
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage

    rate_limit_event_cls = getattr(sdk, "RateLimitEvent", None)  # version-tolerant

    stderr_lines = deque(maxlen=50)
    options = ClaudeAgentOptions(
        model=CC_MODEL_ALIASES.get(model, model),
        system_prompt=system,           # None keeps today's behavior
        tools=[],                       # one-shot text completion, no tools
        permission_mode="bypassPermissions",
        setting_sources=[],             # isolation from filesystem settings
        # pinned: unset, the CLI default can shift under us (spec v6 §2.2)
        thinking={"type": "adaptive"},
        max_turns=4,
        cwd=_workdir(),
        stderr=stderr_lines.append,
        env=_call_env(seat, max_tokens),
    )
    if schema is not None:
        options.output_format = {"type": "json_schema", "schema": schema}

    timeout_s = float(os.environ.get("CAF_VAULT_CC_CALL_TIMEOUT_S", "1200"))
    result = None
    resolved_model = None
    rate_rejected = False
    err = None
    try:
        # Timeout inside the coroutine (anyio): raw cancellation around
        # asyncio.run skips the SDK's terminate->SIGKILL and leaks the child.
        with anyio.fail_after(timeout_s):
            async for msg in sdk.query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    resolved_model = msg.model or resolved_model
                elif (rate_limit_event_cls is not None
                        and isinstance(msg, rate_limit_event_cls)):
                    # The structured quota signal — primary; the regex over
                    # CLI prose is only the fallback (spec v6 §4.1)
                    info = msg.rate_limit_info
                    if info.status == "rejected":
                        rate_rejected = True
                    if info.utilization is not None:
                        seat.last_utilization = info.utilization
                    if info.resets_at is not None:
                        seat.last_resets_at = info.resets_at
                elif isinstance(msg, ResultMessage):
                    result = msg
    except TimeoutError:
        raise TransientError(
            f"claude code call timed out after {timeout_s:.0f}s")
    except BaseExceptionGroup as eg:
        # The CLI exits non-zero after an is_error result, so the SDK raises
        # even when a ResultMessage was already yielded — hold the error and
        # fall through so latch matching still runs on what was collected.
        err = eg.exceptions[0] if len(eg.exceptions) == 1 else eg
    except Exception as exc:
        err = exc

    if result is None:
        stderr_text = "\n".join(stderr_lines)
        err_text = str(err) if err is not None else ""
        if rate_rejected or _QUOTA_PATTERN.search(f"{err_text}\n{stderr_text}"):
            seat.trip((err_text or stderr_text or "rate limit rejected")[:200],
                      "quota", resets_at=seat.fresh_resets_at())
            raise SeatError(f"seat {seat.index} quota exhausted")
        # Process death, crash, transport teardown with no latch matched:
        # transient — never a per-item strike (spec v6 §5)
        raise TransientError(_scrub(
            "claude code call ended without a result: "
            + err_text + _stderr_tail(stderr_lines))) from err

    result_text = result.result or ""
    failed = (
        getattr(result, "is_error", False)
        # missing subtype counts as success (fake-transport tolerance)
        or bool(getattr(result, "subtype", None)
                and result.subtype != "success")
        or (schema is not None and result.structured_output is None)
    )

    # Usage is booked BEFORE the failure branch: a failed call still drew
    # quota (spec v6 §4.4). model is the resolved id — AssistantMessage
    # first, model_usage key second, requested alias last.
    if resolved_model is None and getattr(result, "model_usage", None):
        resolved_model = next(iter(result.model_usage), None)
    usage = getattr(result, "usage", None) or {}
    _CALL_LOG.append({
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get(
            "cache_creation_input_tokens", 0),
        "total_cost_usd": getattr(result, "total_cost_usd", None),
        "duration_ms": getattr(result, "duration_ms", None),
        "session_id": getattr(result, "session_id", None),
        "model": resolved_model or CC_MODEL_ALIASES.get(model, model),
        "seat": seat.index,
        "utilization": seat.last_utilization,
        "ok": not failed,
    })

    if not failed:
        if err is not None:
            # Complete valid result followed by a teardown error — keep the
            # result; discarding a good pass over transport cleanup is worse
            # (Filter commit b188a33).
            print("llm: teardown error after a valid result "
                  f"({_scrub(str(err))[:200]})")
        return result.structured_output if schema is not None else result_text

    stderr_text = "\n".join(stderr_lines)
    # Auth: narrow, literal CLI strings on an is_error result only.
    if getattr(result, "is_error", False) and any(
            a.lower() in result_text.lower() for a in _AUTH_STRINGS):
        seat.trip(result_text[:200], "auth")
        raise SeatError(f"seat {seat.index} auth failed")
    # Quota: stderr and the SDK exception text always; the result text only
    # when is_error — Vault ingests documents ABOUT rate limits and reset
    # dates, and a false latch stalls every LLM stage.
    quota_haystack = "\n".join(
        ([result_text] if getattr(result, "is_error", False) else [])
        + ([str(err)] if err is not None else []) + [stderr_text])
    if rate_rejected or _QUOTA_PATTERN.search(quota_haystack):
        seat.trip((quota_haystack.strip() or "rate limit rejected")[:200],
                  "quota", resets_at=seat.fresh_resets_at())
        raise SeatError(f"seat {seat.index} quota exhausted")
    raise RuntimeError(_scrub(
        f"claude code call failed (subtype={getattr(result, 'subtype', None)!r},"
        f" is_error={getattr(result, 'is_error', False)}): {result_text[:300]}"
        + _stderr_tail(stderr_lines))) from err


def _cc_complete(prompt, model, max_tokens, schema=None, system=None):
    seats = _seats()
    _maybe_rearm()
    with _SEMAPHORE:
        last = None
        for seat in seats:
            if seat.dead:
                continue
            try:
                return asyncio.run(_cc_call(seat, prompt, model, max_tokens,
                                            schema=schema, system=system))
            except SeatError as e:
                last = e
                continue
        reasons = "; ".join(
            f"seat {s.index}: {s.kind or 'dead'}" for s in seats if s.dead)
        raise EngineUnavailable(_scrub(
            f"all {len(seats)} claude code seat(s) unavailable ({reasons})"
            + (f"; last: {last}" if last else "")))


def _api_complete(prompt, model, max_tokens):
    # Legacy engine (spec v6 scope): kept exactly as-is, never extended.
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EngineUnavailable("engine 'api' selected but ANTHROPIC_API_KEY is empty")
    client = anthropic.Anthropic()
    # Quota/auth/overload conditions must pause the pipeline like the
    # claude_code engine does (seat latch -> EngineUnavailable), never surface
    # as per-item failures that burn attempts across the corpus.
    try:
        msg = client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
    except anthropic.RateLimitError as e:
        raise EngineUnavailable(f"api rate limited: {e}") from e
    except anthropic.AuthenticationError as e:
        raise EngineUnavailable(f"api auth failed: {e}") from e
    except anthropic.APIStatusError as e:
        if (getattr(e, "status_code", None) == 529
                or "overloaded" in str(e).lower()):
            raise EngineUnavailable(f"api overloaded: {e}") from e
        raise
    return "".join(b.text for b in msg.content if b.type == "text")


def complete(prompt: str, model: str, max_tokens: int = 8000,
             system: str | None = None) -> str:
    engine = os.environ.get("CAF_VAULT_LLM_ENGINE", "claude_code").strip()
    if engine == "claude_code":
        return _cc_complete(prompt, model, max_tokens, system=system)
    if engine == "api":
        # the legacy engine has no separate system turn; rejoin the halves
        return _api_complete(
            f"{system}\n\n{prompt}" if system else prompt, model, max_tokens)
    raise ValueError(f"unknown CAF_VAULT_LLM_ENGINE: {engine!r}")


def complete_json(prompt: str, model: str, max_tokens: int = 8000,
                  schema: dict | None = None, system: str | None = None):
    """Complete and return a parsed JSON object.

    With a schema on the claude_code engine, the CLI enforces it server-side
    and hands back the parsed object — no re-prompt retry here: the CLI
    already retried against the schema, so a failure is terminal for the
    attempt. Without a schema (or on the legacy api engine, which ignores
    it), JSON is parsed out of the text with one retry rebuilt from the
    ORIGINAL prompt."""
    engine = os.environ.get("CAF_VAULT_LLM_ENGINE", "claude_code").strip()
    if engine == "claude_code" and schema is not None:
        return _cc_complete(prompt, model, max_tokens,
                            schema=schema, system=system)
    last = None
    for attempt in range(2):
        p = prompt if attempt == 0 else (
            prompt + "\n\nYour previous output was not valid JSON "
            f"({last}). Output ONLY the JSON object, nothing else.")
        text = complete(p, model, max_tokens, system=system)
        try:
            return _extract_json(text)
        except Exception as e:
            last = e
    raise ValueError(f"no valid JSON after 2 attempts: {last}")


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    # raw_decode parses exactly one JSON value and ignores trailing text; it is
    # string-aware, so braces inside string values (quoted filing text, code
    # fragments) never truncate the object the way naive brace counting did.
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON object: {e}") from e
    return obj
