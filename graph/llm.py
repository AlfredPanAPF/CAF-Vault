"""LLM interface with two engines.

Engine selection via CAF_VAULT_LLM_ENGINE (default "claude_code"):

- "claude_code": claude-agent-sdk on Claude subscription seats. Seats are
  CLAUDE_CODE_OAUTH_TOKEN_VAULT_1..N env vars — additive, one .env line each.
  With no token set the bundled CLI uses the local `claude` login, which is
  the dev path on our Macs. Pattern ported (simplified) from Filter's
  backend_claude_code.py — the stack's production reference for this engine.
- "api": Anthropic API with ANTHROPIC_API_KEY. Kept as the explicit fallback.

Public surface used by the pipeline: complete() and complete_json(). Both are
engine-agnostic.
"""
import asyncio
import json
import os
import re
import tempfile
import threading
import time
from collections import deque

# Each CLI subprocess is ~300MB; cap concurrency process-wide.
_SEMAPHORE = threading.Semaphore(
    int(os.environ.get("CAF_VAULT_CC_MAX_SUBPROCESSES", "2")))

_QUOTA_PATTERN = re.compile(
    r"rate.?limit|usage limit|spend limit|resets at", re.IGNORECASE)
_AUTH_PATTERN = re.compile(
    r"authentication|unauthorized|oauth|please run /login|api key", re.IGNORECASE)

# The CLI accepts aliases only; unknown names pass through untouched.
CC_MODEL_ALIASES = {
    "claude-fable-5": "fable",
    "claude-opus-5": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-haiku-4-5": "haiku",
}


class SeatError(RuntimeError):
    """This seat is dead (quota/auth); the call should rotate to the next."""


class _Seat:
    def __init__(self, index, token):
        self.index = index
        self.token = token
        self.dead = False
        self.kind = None          # "quota" | "auth"
        self.reason = ""
        self.latched_at = None

    def trip(self, reason, kind):
        self.dead = True
        self.kind = kind
        self.reason = reason
        self.latched_at = time.time()
        print(f"llm: seat {self.index} latched ({kind}): {reason[:160]}")

    def retry_due(self):
        """Epoch when a quota-latched seat may probe again. Auth never re-arms."""
        if self.kind != "quota":
            return None
        probe = float(os.environ.get("CAF_VAULT_CC_RETRY_PROBE_S", "3600"))
        return (self.latched_at or 0) + probe


_SEATS = None
_SEATS_LOCK = threading.Lock()


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
            _SEATS = seats
        return _SEATS


def _maybe_rearm():
    now = time.time()
    for s in _seats():
        due = s.retry_due()
        if s.dead and due is not None and now >= due:
            s.dead = False
            s.kind = None
            print(f"llm: seat {s.index} re-armed for probe")


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


async def _cc_call(seat, prompt, model, max_tokens):
    import anyio
    import claude_agent_sdk as sdk
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage

    stderr_lines = deque(maxlen=50)
    options = ClaudeAgentOptions(
        model=CC_MODEL_ALIASES.get(model, model),
        tools=[],                       # one-shot text completion, no tools
        permission_mode="bypassPermissions",
        setting_sources=[],             # isolation from filesystem settings
        max_turns=4,
        cwd=_workdir(),
        stderr=stderr_lines.append,
        env=_call_env(seat, max_tokens),
    )
    timeout_s = float(os.environ.get("CAF_VAULT_CC_CALL_TIMEOUT_S", "1200"))
    result = None
    err = None
    try:
        # Timeout inside the coroutine (anyio): raw cancellation around
        # asyncio.run skips the SDK's terminate->SIGKILL and leaks the child.
        with anyio.fail_after(timeout_s):
            async for msg in sdk.query(prompt=prompt, options=options):
                if isinstance(msg, ResultMessage):
                    result = msg
    except TimeoutError:
        raise RuntimeError(f"claude code call timed out after {timeout_s:.0f}s")
    except BaseExceptionGroup as eg:
        err = eg.exceptions[0] if len(eg.exceptions) == 1 else eg
    except Exception as exc:
        err = exc

    def _latch_check(text):
        if _QUOTA_PATTERN.search(text):
            seat.trip(text[:200], "quota")
            raise SeatError(f"seat {seat.index} quota exhausted")
        if _AUTH_PATTERN.search(text):
            seat.trip(text[:200], "auth")
            raise SeatError(f"seat {seat.index} auth failed")

    if result is None:
        text = f"{err or ''}\n" + "\n".join(stderr_lines)
        _latch_check(text)
        if isinstance(err, Exception):
            raise err
        raise RuntimeError("claude code call ended without a result: "
                           + "\n".join(list(stderr_lines)[-5:]))
    if getattr(result, "is_error", False):
        text = (result.result or "") + "\n" + "\n".join(stderr_lines)
        _latch_check(text)
        raise RuntimeError(f"claude code call failed: {(result.result or '')[:300]}")
    return result.result or ""


def _cc_complete(prompt, model, max_tokens):
    seats = _seats()
    _maybe_rearm()
    with _SEMAPHORE:
        last = None
        for seat in seats:
            if seat.dead:
                continue
            try:
                return asyncio.run(_cc_call(seat, prompt, model, max_tokens))
            except SeatError as e:
                last = e
                continue
        reasons = "; ".join(
            f"seat {s.index}: {s.kind or 'dead'}" for s in seats if s.dead)
        raise RuntimeError(
            f"all {len(seats)} claude code seat(s) unavailable ({reasons})"
            + (f"; last: {last}" if last else ""))


def _api_complete(prompt, model, max_tokens):
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text")


def complete(prompt: str, model: str, max_tokens: int = 8000) -> str:
    engine = os.environ.get("CAF_VAULT_LLM_ENGINE", "claude_code").strip()
    if engine == "claude_code":
        return _cc_complete(prompt, model, max_tokens)
    if engine == "api":
        return _api_complete(prompt, model, max_tokens)
    raise ValueError(f"unknown CAF_VAULT_LLM_ENGINE: {engine!r}")


def complete_json(prompt: str, model: str, max_tokens: int = 8000, retries: int = 1):
    """Complete and parse a JSON object from the response; one retry on failure."""
    last = None
    for attempt in range(retries + 1):
        text = complete(prompt, model, max_tokens)
        try:
            return _extract_json(text)
        except Exception as e:
            last = e
            prompt = (prompt + "\n\nYour previous output was not valid JSON "
                      f"({e}). Output ONLY the JSON object, nothing else.")
    raise ValueError(f"no valid JSON after {retries + 1} attempts: {last}")


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON object")
