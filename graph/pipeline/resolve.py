"""ER blocking, per mention (design §6 stage 1; ported from spike/resolve_block.py
after the per-surface run proved global resolution wrong — "Constellation" means
different registrants in different documents).

Resolution runs per (event, surface) with document context, in tier order:
  1. filer coreference — "the Company"/"the Registrant" in a filing -> the filer
  2. defined terms from extraction output ("CBI" -> "Constellation Brands, Inc.")
  3. seed alias table
  4. exact ticker (with registrant-name-in-doc-text guard)
  5. exact registrant name / suffix-stripped unique name
  6. filer initials — an acronym matching the filer's name initials in its own filing
  7. filer context — filer among the fuzzy candidates
  8. GLEIF exact / stripped / prefix (single-token guards)
  9. fuzzy candidates -> er_queue for adjudication

Entities are created lazily, keyed on registry identifiers (cik / lei).
"""
import difflib
import re
import sqlite3
from collections import Counter, defaultdict

from psycopg.types.json import Jsonb

from .. import artifacts, config
from ..er_norm import norm, strip_suffix

FILER_COREF = {"THE COMPANY", "COMPANY", "THE CORPORATION", "THE REGISTRANT",
               "REGISTRANT", "THE FIRM"}


def initials(name_norm):
    return "".join(w[0] for w in name_norm.split())


def entity_for_sec(con, cik, ticker, title):
    """Lazy entity keyed on CIK (entity_cik_uniq)."""
    con.execute(
        "insert into entity (kind, canonical_name, registry_refs) "
        "values ('company', %s, %s) "
        "on conflict ((registry_refs->>'cik')) where registry_refs ? 'cik' do nothing",
        (title, Jsonb({"cik": str(cik), "ticker": ticker})))
    return con.execute(
        "select entity_id from entity where registry_refs->>'cik' = %s",
        (str(cik),)).fetchone()["entity_id"]


def entity_for_gleif(con, lei, name, country):
    """Lazy entity keyed on LEI (entity_lei_uniq)."""
    con.execute(
        "insert into entity (kind, canonical_name, registry_refs) "
        "values ('company', %s, %s) "
        "on conflict ((registry_refs->>'lei')) where registry_refs ? 'lei' do nothing",
        (name, Jsonb({"lei": lei, "country": country})))
    return con.execute(
        "select entity_id from entity where registry_refs->>'lei' = %s",
        (lei,)).fetchone()["entity_id"]


def entity_for_rec(con, rec):
    if "cik" in rec:
        return entity_for_sec(con, rec["cik"], rec.get("ticker"), rec["title"])
    return entity_for_gleif(con, rec["lei"], rec["title"], rec.get("country"))


def link_claims(con):
    """Link resolved mentions into their event's claims by surface form.
    Merged-away entities map to their canonical target at link time (design
    §2 rule 5, single-hop like db.entity_canonical_for); mentions keep the
    raw resolved_entity for audit. Shared with adjudicate.py; safe to
    re-run — it only writes changed links."""
    a = con.execute(
        "update claim c set subject_entity = coalesce(s.b, m.resolved_entity) "
        "from mention m left join entity_same_as s "
        "on s.a = m.resolved_entity and s.status = 'active' "
        "where m.event_id = c.event_id and m.surface = c.subject_surface "
        "and m.resolved_entity is not null "
        "and c.subject_entity is distinct from coalesce(s.b, m.resolved_entity)"
    ).rowcount
    b = con.execute(
        "update claim c set object_entity = coalesce(s.b, m.resolved_entity) "
        "from mention m left join entity_same_as s "
        "on s.a = m.resolved_entity and s.status = 'active' "
        "where m.event_id = c.event_id and m.surface = c.object_surface "
        "and m.resolved_entity is not null "
        "and c.object_entity is distinct from coalesce(s.b, m.resolved_entity)"
    ).rowcount
    return a + b


def run(con, limit=1000):
    rows = con.execute(
        "select m.mention_id, m.surface, m.event_id, e.meta, e.artifact_uri "
        "from mention m join event e on e.event_id = m.event_id "
        "where m.resolver = 'pending' order by m.created_at limit %s",
        (limit,)).fetchall()

    # in-memory indexes, built once per run from the registry seeds
    by_ticker, by_name, by_stripped = {}, {}, defaultdict(list)
    for r in con.execute("select cik, ticker, title from registry_sec"):
        rec = {"cik": r["cik"], "ticker": r["ticker"], "title": r["title"]}
        by_ticker.setdefault(rec["ticker"].upper(), rec)
        by_name.setdefault(norm(rec["title"]), rec)
        by_stripped[strip_suffix(rec["title"])].append(rec)
    stripped_keys = list(by_stripped)

    aliases = {}
    for r in con.execute("select alias_norm, ticker from alias_seed"):
        if r["ticker"].upper() in by_ticker:
            aliases[r["alias_norm"]] = by_ticker[r["ticker"].upper()]

    # GLEIF global registry (2.5M entities) — exact and stripped-name lookups,
    # plus a prefix probe for bare short forms ("Vestas" -> VESTAS WIND SYSTEMS A/S)
    gleif = sqlite3.connect(config.GLEIF_SQLITE) if config.GLEIF_SQLITE.exists() else None
    if gleif is None:
        print(f"NOTE: {config.GLEIF_SQLITE} missing — skipping GLEIF tiers")

    _doc_cache = {}

    def doc_text(event_id, artifact_uri):
        if event_id not in _doc_cache:
            try:
                _doc_cache[event_id] = artifacts.get(artifact_uri).decode(
                    "utf-8", errors="replace").lower()
            except OSError:
                _doc_cache[event_id] = ""
        return _doc_cache[event_id]

    def name_lookup(surface):
        """exact/stripped-unique name match -> (rec, tier) or (None, None)"""
        ns, ss = norm(surface), strip_suffix(surface)
        if ns in by_name:
            return by_name[ns], "name_exact"
        if ss and ss in by_stripped and len(by_stripped[ss]) == 1:
            return by_stripped[ss][0], "name_stripped"
        return None, None

    def candidates_for(surface):
        ss = strip_suffix(surface)
        if not ss:
            return []
        cands = []
        if ss in by_stripped:
            cands = list(by_stripped[ss])
        else:
            for key in difflib.get_close_matches(ss, stripped_keys, n=5, cutoff=0.82):
                cands.extend(by_stripped[key])
            if not cands and " " not in ss and len(ss) >= 4:
                cands = [r for k, rs in by_stripped.items()
                         if k.startswith(ss + " ") or k == ss for r in rs][:5]
        return cands

    def gleif_rec(row):
        return {"lei": row[0], "title": row[1], "country": row[2],
                "status": row[3], "registry": "gleif"}

    def gleif_lookup(surface):
        """-> (rec, tier) | ('candidates', rows) | None

        Precision-first: a single-token surface matched by stripped/prefix name
        never auto-resolves ("Cursor" is also a Spanish company's legal name);
        it goes to adjudication as candidates. Only exact full legal-name
        matches and multi-token stripped matches resolve directly."""
        if gleif is None:
            return None
        ns, ss = norm(surface), strip_suffix(surface)
        single_token = " " not in ss
        probes = [("n", ns, "gleif_exact"), ("s", ss, "gleif_stripped")]
        for col, val, tier in probes:
            if not val:
                continue
            g_rows = gleif.execute(
                f"select distinct lei,name,country,status from lei where {col}=? limit 12",
                (val,)).fetchall()
            active = [r for r in g_rows if r[3] == "ACTIVE"] or g_rows
            leis = {r[0] for r in active}
            if len(leis) == 1:
                if tier == "gleif_stripped" and single_token:
                    return "candidates", [gleif_rec(active[0])]
                return gleif_rec(active[0]), tier
            if 1 < len(leis) <= 5:
                return "candidates", [gleif_rec(r) for r in active[:5]]
        if ss and len(ss) >= 4:
            g_rows = gleif.execute(
                "select distinct lei,name,country,status from lei where s like ? limit 6",
                (ss + " %",)).fetchall()
            active = [r for r in g_rows if r[3] == "ACTIVE"] or g_rows
            leis = {r[0] for r in active}
            if len(leis) == 1 and not single_token:
                return gleif_rec(active[0]), "gleif_prefix"
            if 1 <= len(leis) <= 5:
                return "candidates", [gleif_rec(r) for r in active[:5]]
        return None

    def resolve_one(surface, filer, defined, event_id, artifact_uri):
        """-> ('hit', rec, tier) | ('queue', candidates, None) | ('unresolved', None, None)"""
        ns = norm(surface)
        if filer and ns in FILER_COREF:
            return "hit", filer, "filer_coref"
        if ns in defined:
            rec, _ = name_lookup(defined[ns])
            if rec:
                return "hit", rec, "defined_term"
            if filer and strip_suffix(defined[ns]) == strip_suffix(filer["title"]):
                return "hit", filer, "defined_term"
        if ns in aliases:
            return "hit", aliases[ns], "alias"
        if re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", surface) and surface.upper() in by_ticker:
            # ticker strings collide across markets ("BYD" is Boyd Gaming's
            # NYSE ticker and a Chinese carmaker) — require the registrant's
            # name to actually appear in the document, else adjudicate
            rec = by_ticker[surface.upper()]
            stripped = strip_suffix(rec["title"])
            name_tok = stripped.split()[0].lower() if stripped else ""
            if name_tok and name_tok in doc_text(event_id, artifact_uri):
                return "hit", rec, "ticker"
            g = gleif_lookup(surface)
            extra = g[1] if g and g[0] == "candidates" else ([g[0]] if g else [])
            return "queue", [rec] + extra, None
        rec, tier = name_lookup(surface)
        if rec:
            return "hit", rec, tier
        if (filer and re.fullmatch(r"[A-Z]{2,5}", surface)
                and surface in (initials(norm(filer["title"])),
                                initials(strip_suffix(filer["title"])))):
            return "hit", filer, "filer_initials"
        cands = candidates_for(surface)
        if filer and any(c["cik"] == filer["cik"] for c in cands):
            return "hit", filer, "filer_context"
        g = gleif_lookup(surface)
        if g and g[0] != "candidates":
            rec, tier = g
            # exact legal-name match trumps SEC fuzzy guesses; a stripped/prefix
            # match only auto-resolves when SEC offered nothing at all
            if not cands or tier == "gleif_exact":
                return "hit", rec, tier
            return "queue", cands + [rec], None
        if g:
            cands = cands + g[1]
        if cands:
            return "queue", cands, None
        return "unresolved", None, None

    tiers = Counter()
    queued = unresolved = 0
    for row in rows:
        surface = row["surface"].strip()
        meta = row["meta"] or {}
        filer = by_ticker.get((meta.get("ticker") or "").upper())
        defined = {norm(k): v for k, v in (meta.get("defined_terms") or {}).items()}
        outcome, payload, tier = resolve_one(
            surface, filer, defined, row["event_id"], row["artifact_uri"])
        if outcome == "hit":
            entity_id = entity_for_rec(con, payload)
            confidence = 0.85 if tier.startswith(("gleif", "filer")) else 0.95
            con.execute(
                "update mention set resolved_entity=%s, resolver=%s, confidence=%s "
                "where mention_id=%s",
                (entity_id, f"blocking-v1:{tier}", confidence, row["mention_id"]))
            tiers[tier] += 1
        elif outcome == "queue":
            con.execute(
                "insert into er_queue (mention_id, candidates) values (%s, %s) "
                "on conflict (mention_id) do nothing",
                (row["mention_id"], Jsonb(payload)))
            con.execute("update mention set resolver='queued' where mention_id=%s",
                        (row["mention_id"],))
            queued += 1
        else:
            con.execute("update mention set resolver='unresolved-v1' where mention_id=%s",
                        (row["mention_id"],))
            unresolved += 1
    if gleif is not None:
        gleif.close()

    linked = link_claims(con)
    resolved = sum(tiers.values())
    print(f"resolve: {len(rows)} mentions | {resolved} resolved | {queued} queued "
          f"| {unresolved} unresolved | {linked} claim links "
          f"| tiers: {dict(tiers.most_common())}")
    return {"resolved": resolved, "queued": queued, "unresolved": unresolved,
            "claims_linked": linked, "tiers": dict(tiers)}
