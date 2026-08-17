"""Fast-path materiality triage (design §3, §11.3; build spec v3 §4.1-4.2).

Runs right after the connectors: pending, untriaged events get a heuristic
materiality score — no LLM, so this stage never pauses and works with no seat
token. Fast-route events for watchlisted tickers emit a material_event alert;
the slow path (extract onward) is untouched either way.
"""
import re

from psycopg.types.json import Jsonb

from .. import alerts, config

SCORER = "heuristic-v1"

# 8-K item numbers (spec §4.1): M&A/agreements/control changes outrank
# officer/bankruptcy/reg-FD items; anything else on an 8-K is middling.
ITEM_SCORES = {"2.01": 0.9, "1.01": 0.9, "5.01": 0.9,
               "5.02": 0.7, "1.03": 0.7, "7.01": 0.7}
OTHER_8K = 0.5
OTHER_FORM = 0.4

# Substring match on the lowercased title; stems on purpose ("acquire" catches
# acquires/acquired, "appoint" catches appointment).
KEYWORDS = ("acquisition", "acquire", "merger", "buyout", "bankruptcy",
            "chapter 11", "resign", "appoint", "guidance", "lawsuit", "sues",
            "recall", "investigation", "halt", "default")
KEYWORD_SCORE = 0.7
DULL_SCORE = 0.3


def materiality(connector, meta):
    if connector == "edgar":
        # meta 'items' may be a list or a comma-joined string ("2.02,9.01")
        items = re.findall(r"\d+\.\d+", str(meta.get("items") or ""))
        if items:
            return max(ITEM_SCORES.get(i, OTHER_8K) for i in items)
        return OTHER_8K if meta.get("form") == "8-K" else OTHER_FORM
    title = (meta.get("title") or meta.get("filename") or "").lower()
    return KEYWORD_SCORE if any(k in title for k in KEYWORDS) else DULL_SCORE


def run(con, limit=500):
    """Score every pending, untriaged event, draining in batches of `limit`
    (no LLM, so a bulk backlog is fully scored in the cycle it arrives —
    extract only consumes triaged events, so nothing may be left behind)."""
    totals = {"scored": 0, "fast": 0, "alerts": 0}
    while True:
        out = _run_batch(con, limit)
        for k in totals:
            totals[k] += out[k]
        if out["scored"] < limit:
            break
    print(f"triage: {totals['scored']} scored, {totals['fast']} fast, "
          f"{totals['alerts']} alerts")
    return totals


def _run_batch(con, limit):
    events = con.execute(
        "select event_id, connector, meta from event "
        "where status = 'pending' and triage is null "
        "order by fetched_at limit %s", (limit,)).fetchall()
    watchlist = {r["ticker"] for r in
                 con.execute("select ticker from watchlist where active")}

    scored = fast = n_alerts = 0
    for ev in events:
        meta = ev["meta"] or {}
        x = materiality(ev["connector"], meta)
        route = "fast" if x >= config.ALERT_MATERIALITY_MIN else "slow"
        con.execute(
            "update event set triage = %s where event_id = %s",
            (Jsonb({"materiality": x, "route": route, "scorer": SCORER}),
             ev["event_id"]))
        scored += 1
        if route != "fast":
            continue
        fast += 1
        ticker = meta.get("ticker")
        if ticker not in watchlist:
            continue
        # ticker is not unique-indexed on entity (cik/lei are); first match wins
        entity = con.execute(
            "select entity_id from entity where registry_refs->>'ticker' = %s "
            "order by created_at limit 1", (ticker,)).fetchone()
        title = meta.get("title")
        alerts.emit(con, "material_event",
                    f"{ticker}: {meta.get('form') or title}", body=title,
                    entity_ids=[entity["entity_id"]] if entity else [],
                    event_id=ev["event_id"])
        n_alerts += 1

    return {"scored": scored, "fast": fast, "alerts": n_alerts}
