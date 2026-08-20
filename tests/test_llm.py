"""Seat-engine unit tests (build spec v6 §8) — claude_agent_sdk.query
monkeypatched, on the seam Filter proved (tests/test_cc_backend.py, same SDK
version). Covers structured output, the RateLimitEvent quota latch, the
narrowed text matching, resets_at re-arm, the transient-error taxonomy,
per-call usage records, token scrubbing, and boot model validation.

No database: the app_kv latch mirror is stubbed out by the autouse fixture;
the one persistence test puts the real helpers back and uses the shared DB.
"""
import asyncio
import time

import claude_agent_sdk
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
)

from graph import llm

# captured before any monkeypatching, for the persistence test
_REAL_STORE = llm._store_latches
_REAL_LOAD = llm._load_latches

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}},
          "required": ["a"], "additionalProperties": False}


@pytest.fixture(autouse=True)
def _fresh_seats(monkeypatch):
    for i in range(1, 6):
        monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_VAULT_{i}", raising=False)
    monkeypatch.delenv("CAF_VAULT_LLM_ENGINE", raising=False)
    # the engine must run without a DB in unit tests; the mirror helpers are
    # best-effort and stubbed here so no test depends on app_kv state
    monkeypatch.setattr(llm, "_store_latches", lambda: None)
    monkeypatch.setattr(llm, "_load_latches", lambda seats: None)
    llm._reset_seats()
    yield
    llm._reset_seats()


def _result(*, subtype="success", is_error=False, structured_output=None,
            result="done", usage=None, total_cost_usd=0.42, model_usage=None):
    return ResultMessage(
        subtype=subtype, duration_ms=1234, duration_api_ms=1100,
        is_error=is_error, num_turns=1, session_id="sess-1",
        total_cost_usd=total_cost_usd, usage=usage, result=result,
        structured_output=structured_output, model_usage=model_usage)


def _rate_event(status="rejected", resets_at=None, utilization=None):
    return RateLimitEvent(
        rate_limit_info=RateLimitInfo(status=status, resets_at=resets_at,
                                      utilization=utilization),
        uuid="u-1", session_id="sess-1")


def _install_query(monkeypatch, messages, stderr_lines=(), raise_after=None):
    """Monkeypatch claude_agent_sdk.query with a fake yielding *messages*.

    `messages` is one list (every call) or a list of per-call lists.
    `raise_after` reproduces the real CLI contract: the process exits
    non-zero after an is_error result, so query() raises AFTER yielding the
    ResultMessage. `stderr_lines` are pushed through options.stderr."""
    calls = []
    scripts = (list(messages) if messages and isinstance(messages[0], list)
               else None)

    async def fake_query(*, prompt, options=None, transport=None):
        calls.append((prompt, options))
        script = scripts.pop(0) if scripts else messages
        if stderr_lines and options is not None and options.stderr:
            for line in stderr_lines:
                options.stderr(line)
        for message in script:
            yield message
        if raise_after is not None:
            raise raise_after

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    return calls


# ── structured output (§2) ───────────────────────────────────────


def test_structured_success_options_shape_and_no_retry(monkeypatch):
    calls = _install_query(monkeypatch, [_result(structured_output={"a": 1})])
    out = llm.complete_json("user turn", "claude-sonnet-5",
                            schema=SCHEMA, system="system turn")
    assert out == {"a": 1}
    assert len(calls) == 1                       # no retry loop entered
    prompt, options = calls[0]
    assert prompt == "user turn"
    assert options.system_prompt == "system turn"
    assert options.output_format == {"type": "json_schema", "schema": SCHEMA}
    assert options.thinking == {"type": "adaptive"}
    assert options.model == "sonnet"
    assert options.tools == []
    assert options.max_turns == 4
    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == []
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == ""


def test_structured_output_none_raises_with_subtype(monkeypatch):
    calls = _install_query(monkeypatch, [_result(
        subtype="error_max_structured_output_retries", is_error=False,
        structured_output=None)])
    with pytest.raises(RuntimeError, match="error_max_structured_output_retries"):
        llm.complete_json("p", "claude-sonnet-5", schema=SCHEMA)
    assert len(calls) == 1                       # terminal for the attempt
    assert not any(s.dead for s in llm._seats())


def test_legacy_json_path_rebuilds_retry_from_original_prompt(monkeypatch):
    calls = _install_query(monkeypatch, [
        [_result(result="that is not json")],
        [_result(result='{"a": 1}')],
    ])
    assert llm.complete_json("ORIGINAL", "claude-sonnet-5") == {"a": 1}
    assert len(calls) == 2
    assert calls[0][0] == "ORIGINAL"
    retry = calls[1][0]
    assert retry.startswith("ORIGINAL")
    assert retry.count("Output ONLY the JSON object") == 1


# ── quota latch (§4.1) ───────────────────────────────────────────


def test_rate_limit_event_latches_without_any_regex_match(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_1", "tok1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_2", "tok2")
    llm._reset_seats()
    reset_epoch = int(time.time()) + 7200
    calls = _install_query(monkeypatch, [
        [_rate_event(resets_at=reset_epoch, utilization=1.0),
         _result(is_error=True, result="internal error")],   # no quota words
        [_result(result="ok from seat 2")],
    ])
    assert llm.complete("p", "claude-sonnet-5") == "ok from seat 2"
    assert len(calls) == 2                       # rotation carried the call
    seats = llm._seats()
    assert seats[0].dead and seats[0].kind == "quota"
    assert seats[0].resets_at == reset_epoch     # carried onto the latch
    assert not seats[1].dead
    assert calls[1][1].env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok2"


def test_quota_regex_on_stderr_latches_the_2026_08_20_string(monkeypatch):
    message = "You've hit your weekly limit · resets Aug 21, 3am (UTC)"
    _install_query(monkeypatch, [], stderr_lines=[message])
    with pytest.raises(llm.EngineUnavailable):
        llm.complete("p", "claude-sonnet-5")
    seat = llm._seats()[0]
    assert seat.dead and seat.kind == "quota"
    assert "weekly limit" in seat.reason


def test_quota_vocabulary_in_a_successful_result_does_not_latch(monkeypatch):
    text = ("The article covers API rate limits: the usage limit resets at "
            "3am and the spend limit was raised.")
    _install_query(monkeypatch, [_result(result=text)])
    assert llm.complete("p", "claude-sonnet-5") == text
    assert not any(s.dead for s in llm._seats())


# ── auth latch, narrow (§4.1) ────────────────────────────────────


def test_auth_latches_only_on_is_error_with_literal_cli_string(monkeypatch):
    _install_query(monkeypatch, [_result(is_error=True, result="Not logged in")])
    with pytest.raises(llm.EngineUnavailable):
        llm.complete("p", "claude-sonnet-5")
    seat = llm._seats()[0]
    assert seat.dead and seat.kind == "auth"


def test_authentication_in_stderr_with_healthy_result_does_not_latch(monkeypatch):
    _install_query(monkeypatch, [_result(result="fine")],
                   stderr_lines=["TLS authentication handshake retried"])
    assert llm.complete("p", "claude-sonnet-5") == "fine"
    assert not any(s.dead for s in llm._seats())


# ── unknown errors and the dead pool (§4.1) ──────────────────────


def test_unknown_error_no_latch_no_rotation_raises(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_1", "tok1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_2", "tok2")
    llm._reset_seats()
    calls = _install_query(monkeypatch, [_result(
        is_error=True, result="some strange internal failure")],
        stderr_lines=["stack line one"])
    with pytest.raises(RuntimeError, match="claude code call failed") as exc:
        llm.complete("p", "claude-sonnet-5")
    assert "stderr tail" in str(exc.value)
    assert len(calls) == 1
    assert not any(s.dead for s in llm._seats())


def test_all_seats_latched_refuses_without_spawning(monkeypatch):
    for s in llm._seats():
        s.trip("earlier quota", "quota")
    calls = _install_query(monkeypatch, [_result()])
    with pytest.raises(llm.EngineUnavailable):
        llm.complete("p", "claude-sonnet-5")
    assert calls == []


# ── resets_at re-arm (§4.2) ──────────────────────────────────────


def test_retry_due_honours_future_reset_with_grace():
    seat = llm._Seat(1, "")
    seat.last_resets_at = time.time() + 1000
    seat.trip("weekly limit", "quota", resets_at=seat.fresh_resets_at())
    assert seat.retry_due() == pytest.approx(seat.resets_at + 30, abs=1)


def test_stale_reset_falls_back_to_probe_interval(monkeypatch):
    monkeypatch.setenv("CAF_VAULT_CC_RETRY_PROBE_S", "3600")
    seat = llm._Seat(1, "")
    seat.last_resets_at = time.time() - 5        # sticky, already elapsed
    assert seat.fresh_resets_at() is None
    seat.trip("weekly limit", "quota", resets_at=seat.fresh_resets_at())
    assert seat.retry_due() == pytest.approx(seat.latched_at + 3600, abs=1)


def test_auth_rearms_after_auth_probe_interval(monkeypatch):
    monkeypatch.setenv("CAF_VAULT_CC_AUTH_RETRY_PROBE_S", "21600")
    seat = llm._seats()[0]
    seat.trip("Not logged in", "auth")
    assert seat.retry_due() == pytest.approx(seat.latched_at + 21600, abs=1)
    # a replaced token must not need a container bounce to be noticed
    monkeypatch.setenv("CAF_VAULT_CC_AUTH_RETRY_PROBE_S", "0")
    llm._maybe_rearm()
    assert not seat.dead
    # the re-arm clears the stale latch fields seat_status used to report
    assert seat.kind is None and seat.reason == "" and seat.latched_at is None
    status = llm.seat_status()[0]
    assert status["latched"] is False and status["reason"] == ""
    assert status["retry_at"] is None


# ── transient taxonomy (§5) ──────────────────────────────────────


def test_timeout_raises_transient(monkeypatch):
    monkeypatch.setenv("CAF_VAULT_CC_CALL_TIMEOUT_S", "0.2")

    async def slow_query(*, prompt, options=None, transport=None):
        await asyncio.sleep(30)
        yield None

    monkeypatch.setattr(claude_agent_sdk, "query", slow_query)
    with pytest.raises(llm.TransientError, match="timed out"):
        llm.complete("p", "claude-sonnet-5")
    assert not any(s.dead for s in llm._seats())


def test_died_without_result_raises_transient(monkeypatch):
    _install_query(monkeypatch, [], raise_after=RuntimeError("process died"))
    with pytest.raises(llm.TransientError, match="process died"):
        llm.complete("p", "claude-sonnet-5")
    assert not any(s.dead for s in llm._seats())


def test_latch_match_wins_over_transient(monkeypatch):
    _install_query(monkeypatch, [],
                   stderr_lines=["usage limit reached for this seat"])
    with pytest.raises(llm.EngineUnavailable):
        llm.complete("p", "claude-sonnet-5")
    assert llm._seats()[0].kind == "quota"


# ── per-call usage (§4.4) ────────────────────────────────────────


def test_failed_call_still_appends_to_the_call_log(monkeypatch):
    _install_query(monkeypatch, [_result(
        is_error=True, result="boom",
        usage={"input_tokens": 5, "output_tokens": 7},
        total_cost_usd=0.01)])
    with pytest.raises(RuntimeError):
        llm.complete("p", "claude-sonnet-5")
    log = llm.take_call_log()
    assert len(log) == 1
    entry = log[0]
    assert entry["ok"] is False
    assert entry["input_tokens"] == 5 and entry["output_tokens"] == 7
    assert entry["total_cost_usd"] == 0.01
    assert entry["seat"] == 1
    assert llm.take_call_log() == []             # drained


def test_resolved_model_comes_from_assistant_message(monkeypatch):
    _install_query(monkeypatch, [
        AssistantMessage(content=[], model="claude-sonnet-5-20260203"),
        _result(structured_output={"a": 1})])
    llm.complete_json("p", "claude-sonnet-5", schema=SCHEMA)
    info = llm.last_call_info()
    assert info["model"] == "claude-sonnet-5-20260203"   # not "sonnet"
    assert info["ok"] is True
    llm.take_call_log()


# ── token scrub (§4.5) ───────────────────────────────────────────


def test_seat_token_never_leaves_the_process(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_VAULT_1", "tok-secret-abc")
    llm._reset_seats()
    _install_query(monkeypatch, [_result(
        is_error=True,
        result="usage limit hit; env CLAUDE_CODE_OAUTH_TOKEN=tok-secret-abc")])
    with pytest.raises(llm.EngineUnavailable) as exc:
        llm.complete("p", "claude-sonnet-5")
    assert "tok-secret-abc" not in str(exc.value)
    seat = llm._seats()[0]
    assert "tok-secret-abc" not in seat.reason
    assert "<seat-token>" in seat.reason
    assert "tok-secret-abc" not in str(llm.seat_status())


def test_teardown_error_after_valid_result_keeps_the_result(monkeypatch):
    _install_query(monkeypatch, [_result(structured_output={"a": 1})],
                   raise_after=RuntimeError("transport teardown"))
    assert llm.complete_json("p", "claude-sonnet-5", schema=SCHEMA) == {"a": 1}
    assert not any(s.dead for s in llm._seats())


# ── boot validation (§4.6) ───────────────────────────────────────


def test_validate_models_rejects_a_typo_naming_the_key():
    with pytest.raises(ValueError) as exc:
        llm.validate_models({"extract": "claude-sonet-5"})
    assert "extract" in str(exc.value)
    assert "claude-sonet-5" in str(exc.value)
    assert "CAF_MODEL_EXTRACT" in str(exc.value)
    # every configured default is accepted
    llm.validate_models({
        "triage": "claude-haiku-4-5-20251001",
        "extract": "claude-sonnet-5",
        "garden": "claude-opus-4-8",
    })


def test_validate_models_skips_the_api_engine(monkeypatch):
    monkeypatch.setenv("CAF_VAULT_LLM_ENGINE", "api")
    llm.validate_models({"extract": "claude-sonnet-5-20260203"})


# ── latch persistence (§4.3) ─────────────────────────────────────


def test_latch_state_survives_a_seat_pool_reset(con, monkeypatch):
    from graph import db

    monkeypatch.setattr(llm, "_store_latches", _REAL_STORE)
    monkeypatch.setattr(llm, "_load_latches", _REAL_LOAD)
    llm._reset_seats()
    try:
        reset_epoch = time.time() + 7200
        llm._seats()[0].trip("weekly limit", "quota", resets_at=reset_epoch)
        llm._reset_seats()                       # a deploy or OOM
        seat = llm._seats()[0]
        assert seat.dead and seat.kind == "quota"
        assert seat.resets_at == pytest.approx(reset_epoch, abs=1)
        assert seat.reason == "weekly limit"
        # an elapsed latch is NOT re-applied
        seat.trip("weekly limit", "quota", resets_at=time.time() - 7200)
        seat.latched_at = time.time() - 7200
        _REAL_STORE()
        llm._reset_seats()
        assert not llm._seats()[0].dead
    finally:
        with db.connect() as c:
            c.execute("delete from app_kv where key='seat_latches'")
            c.commit()
