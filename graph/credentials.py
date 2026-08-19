"""Credentials for premium sites (build spec v4 §3.1).

One row per site in the `credential` table: a cookies.txt export (FT, WSJ,
Substack, YouTube) or a bearer token (X). Values are read by the fetchers and
connectors; the API only ever reports whether a value is set and how the last
live check went. Nothing here logs or returns a value.

`credential_session` holds what a credential is turned into per host (build
spec v4 §3.4): a Substack custom domain runs its own session, minted from the
stored sid by graph/substack_session.py and kept here under the publication
host. Same rules: never logged, never returned, scrubbed from every message.
"""
from datetime import timezone

SITES = {
    "ft": {
        "label": "Financial Times", "kind": "cookies",
        "help": "Paste a cookies.txt export for ft.com from a signed-in browser.",
    },
    "wsj": {
        "label": "Wall Street Journal", "kind": "cookies",
        "help": "Paste a cookies.txt export for wsj.com from a signed-in browser.",
    },
    "substack": {
        # §3.1 fixes the help string; `set()` below also takes a bare
        # substack.sid. Subscriptions are per publication, so the Test button
        # asks for a link to a paid post the account subscribes to (probes.py)
        # instead of choosing one; `test_link` is the link box's placeholder.
        "label": "Substack", "kind": "cookies",
        "help": "Paste a cookies.txt export for substack.com. Paid posts need it.",
        "test_link": "Link to a paid post you subscribe to",
    },
    "x": {
        "label": "X", "kind": "bearer",
        "help": "Paste the bearer token from the X developer portal.",
    },
    "youtube": {
        "label": "YouTube", "kind": "cookies",
        "help": "Paste a cookies.txt export for youtube.com from a signed-in "
                "browser. Needed for transcripts.",
    },
}

# hosts a site's cookies apply to when the pasted value carries no domain
# column (raw "a=b; c=d" header form)
SITE_HOSTS = {
    "ft": ("ft.com",),
    "wsj": ("wsj.com", "dowjones.com"),
    "substack": ("substack.com",),
    "youtube": ("youtube.com", "google.com"),
    "x": ("x.com", "twitter.com"),
}


class InvalidCredential(ValueError):
    pass


def substack_jar(value: str) -> dict[str, str]:
    """The cookies a stored Substack paste sends to substack.com hosts: the
    substack.com cookies plus the two flags the rss-bridge SubstackBridge
    sends alongside the sid (docs/rss-bridge-brief)."""
    jar = cookie_jar(value, host="substack.com", site="substack")
    if "substack.sid" in jar:
        jar.setdefault("substack.lli", "1")
        jar.setdefault("ab_experiment_sampled", "%22false%22")
    return jar


# ---------------------------------------------------------------- parsing


def parse_cookies(value: str) -> list[dict]:
    """Netscape cookies.txt (7 tab-separated columns, '#HttpOnly_' prefixes,
    comment lines) or a raw 'Cookie:' header line ('a=b; c=d') ->
    [{name, value, domain|None, path}]. Raises InvalidCredential when nothing
    parses."""
    out = []
    text = value.replace("\r\n", "\n").strip()
    if not text:
        raise InvalidCredential("Nothing to save.")
    lines = text.split("\n")
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_"):]
        elif raw.startswith("#"):
            continue
        cols = raw.split("\t")
        if len(cols) >= 7:
            domain = cols[0].lstrip(".").lower() or None
            out.append({"name": cols[5], "value": "\t".join(cols[6:]),
                        "domain": domain, "path": cols[2] or "/"})
            continue
        # header form, possibly with a leading "Cookie:" label
        if raw.lower().startswith("cookie:"):
            raw = raw[len("cookie:"):].strip()
        for part in raw.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, val = part.partition("=")
            name = name.strip()
            if name:
                out.append({"name": name, "value": val.strip(),
                            "domain": None, "path": "/"})
    if not out:
        raise InvalidCredential(
            "Could not read any cookies. Paste a cookies.txt export or a "
            "Cookie header line.")
    return out


def cookie_jar(value: str, host: str | None = None,
               site: str | None = None) -> dict[str, str]:
    """{name: value} for one host. Cookies with a domain column apply when
    the host equals it or ends with '.<domain>'; cookies without a domain
    apply when the host is one of the site's known hosts (or no host given)."""
    jar = {}
    host = (host or "").lower()
    site_hosts = SITE_HOSTS.get(site or "", ())
    for c in parse_cookies(value):
        dom = c["domain"]
        if not host:
            ok = True
        elif dom:
            ok = host == dom or host.endswith("." + dom)
        else:
            ok = any(host == h or host.endswith("." + h) for h in site_hosts) \
                or not site_hosts
        if ok:
            jar[c["name"]] = c["value"]
    return jar


def netscape_text(value: str, site: str | None = None) -> str:
    """Re-serialize a pasted value as a Netscape cookies.txt file body (for
    tools that want a cookiefile, e.g. yt-dlp). Header-form cookies get the
    site's first known host as their domain."""
    lines = ["# Netscape HTTP Cookie File"]
    default_domain = (SITE_HOSTS.get(site or "") or ("",))[0]
    for c in parse_cookies(value):
        dom = c["domain"] or default_domain
        if not dom:
            continue
        lines.append("\t".join([
            "." + dom, "TRUE", c["path"] or "/", "TRUE", "2147483647",
            c["name"], c["value"]]))
    return "\n".join(lines) + "\n"


def bearer_token(value: str) -> str:
    """The token as it goes into an Authorization header. Everything outside
    printable ASCII is refused: a stray carriage return in a pasted token is
    accepted by `strip()` but rejected by the HTTP client, and the rejection
    message carries the whole token into logs and into the check message the
    sources page shows."""
    token = value.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(c < "!" or c > "~" for c in token):
        raise InvalidCredential("A bearer token is a single line with no spaces.")
    return token


def scrub(con, text: str) -> str:
    """Replace any stored credential value (and each cookie inside it) with
    '***'. Every string that reaches a user-visible field or a log line goes
    through this: a fetch client's exception can quote the header or the URL it
    choked on, and those carry the values this module exists to keep private."""
    out = str(text or "")
    if con is None or not out:
        return out
    rows = []
    for table in ("credential", "credential_session"):
        try:
            # own savepoint per table: one missing (pre-migration) must not
            # stop the other being scrubbed nor abort the caller's transaction
            with con.transaction():
                rows.extend(con.execute(f"select value from {table}").fetchall())
        except Exception:
            continue
    secrets = []
    for row in rows:
        value = (row["value"] or "").strip()
        if not value:
            continue
        secrets.append(value)
        try:
            secrets.extend(c["value"] for c in parse_cookies(value))
        except InvalidCredential:
            pass
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 8 and secret in out:
            out = out.replace(secret, "***")
    return out


# ---------------------------------------------------------------- storage


def get(con, site: str) -> dict | None:
    """The stored row (value included; internal use only)."""
    return con.execute("select * from credential where site=%s", (site,)).fetchone()


def value(con, site: str) -> str | None:
    row = get(con, site)
    return row["value"] if row else None


def set(con, site: str, value: str, note: str | None = None) -> None:  # noqa: A001
    if site not in SITES:
        raise InvalidCredential(f"Unknown site {site}.")
    kind = SITES[site]["kind"]
    if kind == "cookies":
        stored = value.strip()
        # Substack: a bare session id pasted from DevTools becomes the one
        # cookie that matters (a sid has no '=' and no tabs).
        if (site == "substack" and stored and "=" not in stored
                and "\t" not in stored and "\n" not in stored):
            stored = f"substack.sid={stored}"
        parse_cookies(stored)
    else:
        stored = bearer_token(value)
    # sessions minted from the previous value go with it: the next fetch that
    # needs one mints it from the new paste
    con.execute("delete from credential_session where site=%s", (site,))
    con.execute(
        "insert into credential (site, kind, value, note, updated_at, "
        "checked_at, check_ok, check_message) "
        "values (%s, %s, %s, %s, now(), null, null, null) "
        "on conflict (site) do update set kind=excluded.kind, "
        "value=excluded.value, note=excluded.note, updated_at=now(), "
        "checked_at=null, check_ok=null, check_message=null",
        (site, kind, stored, note))


def delete(con, site: str) -> bool:
    # credential_session rows go with the credential (on delete cascade)
    cur = con.execute("delete from credential where site=%s", (site,))
    return cur.rowcount > 0


# ------------------------------------------------------- derived sessions


def session_get(con, site: str, host: str) -> dict | None:
    """The host's derived session row with three verdicts computed in SQL:
    `live` (a value that has not expired), `current` (live, or a recorded
    failure whose retry time has not come) and `fresh` (written or touched
    within the last hour: a paid post that comes back cut that soon after is
    the account not subscribing, not the session, so it is not re-minted)."""
    return con.execute(
        "select value, updated_at, expires_at, "
        "(value <> '' and (expires_at is null or expires_at > now())) as live, "
        "(expires_at is null or expires_at > now()) as current, "
        "updated_at > now() - interval '60 minutes' as fresh "
        "from credential_session where site=%s and host=%s",
        (site, host.lower())).fetchone()


def session_set(con, site: str, host: str, value: str,
                expires_at=None, retry_minutes: int | None = None) -> None:
    """Store a host's derived session (cookie header form), or with `value`
    empty the fact that minting one failed, to be retried after
    `retry_minutes`."""
    if value:
        con.execute(
            "insert into credential_session (site, host, value, updated_at, expires_at) "
            "values (%s, %s, %s, now(), %s) on conflict (site, host) do update set "
            "value=excluded.value, updated_at=now(), expires_at=excluded.expires_at",
            (site, host.lower(), value, expires_at))
    else:
        con.execute(
            "insert into credential_session (site, host, value, updated_at, expires_at) "
            "values (%s, %s, '', now(), now() + make_interval(mins => %s)) "
            "on conflict (site, host) do update set value='', updated_at=now(), "
            "expires_at=excluded.expires_at",
            (site, host.lower(), int(retry_minutes or 15)))


def session_touch(con, site: str, host: str) -> None:
    """Mark the host's stored session as just looked at (`fresh` again for
    an hour) without touching its value: a re-mint that failed must not cost
    a session that still works, nor be repeated every poll."""
    con.execute("update credential_session set updated_at=now() "
                "where site=%s and host=%s", (site, host.lower()))


def session_jar(con, site: str, host: str) -> dict[str, str]:
    """{name: value} of the host's live derived session, else {}."""
    row = session_get(con, site, host)
    if not row or not row["live"]:
        return {}
    try:
        # the value was stored for this host: no domain filtering
        return cookie_jar(row["value"])
    except InvalidCredential:
        return {}


def record_check(con, site: str, ok: bool, message: str) -> None:
    con.execute(
        "update credential set checked_at=now(), check_ok=%s, check_message=%s "
        "where site=%s", (ok, message[:300], site))


def status(con) -> list[dict]:
    """Per-site status for the API. Never includes the value."""
    rows = {r["site"]: r for r in con.execute(
        "select site, kind, updated_at, checked_at, check_ok, check_message "
        "from credential")}
    out = []
    for site, spec in SITES.items():
        r = rows.get(site)
        out.append({
            "site": site, "label": spec["label"], "kind": spec["kind"],
            "help": spec["help"], "test_link": spec.get("test_link"),
            "set": r is not None,
            "updated_at": _iso(r["updated_at"]) if r else None,
            "checked_at": _iso(r["checked_at"]) if r else None,
            "check_ok": r["check_ok"] if r else None,
            "check_message": r["check_message"] if r else None,
        })
    return out


def _iso(v):
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc).isoformat()


def is_set(con, site: str) -> bool:
    return con.execute("select 1 from credential where site=%s",
                       (site,)).fetchone() is not None
