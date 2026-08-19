"""Sessions for Substack custom domains (build spec v4 §3.4).

The pasted substack.sid signs in on substack.com and on *.substack.com
publication hosts. A publication on its own domain (newsletter.semianalysis.com,
www.newcomer.co) is the same backend but a separate session: its API answers
401 to the substack.com sid, and a browser that is signed in on substack.com
gets in through Substack's cross-domain sign-in, which mints a cookie for the
publication host. This module does what the browser does, once per host, and
keeps the result in credential_session:

  1. GET <origin>/account/login?redirect=/        -> 301 to substack.com/sign-in
     whose `for_pub` query is the publication's Substack slug
  2. GET substack.com/sign-in?redirect=<origin>/&for_pub=<slug> with the
     stored sid -> 303 to <origin>/api/v1/sign-in/local/complete?token=...
     (a 200 here is the sign-in page: the sid is dead)
  3. GET that completion URL -> 303 to <origin>/ and Set-Cookie connect.sid
     for the publication host, which is the session

Verified live against newsletter.semianalysis.com on 2026-08-19: with the
connect.sid, /api/v1/user/profile/self answers 200 for the same user,
/api/v1/subscription says `subscribed`, and a paid post comes back whole
(10,538 of 10,638 words against 8,463 for the preview).

Nothing here logs or returns a cookie value. fetch._cookies_for reads the
stored session for the host; rss.substack_post_json calls ensure() before a
signed post fetch and refresh() when the post came back cut; the Sign-ins
Test (probes.py) calls mint() itself, so a human retry never waits on the
backoff below.
"""
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

from . import credentials, fetch

SITE = "substack"
SIGN_IN = "https://substack.com/sign-in"
# a mint that failed with no session to fall back on is not retried before
# this (the sid is dead, or Substack did not answer): the feed keeps polling
# and would otherwise repeat the handshake on every paid post. A session under
# an hour old (credentials.session_get's `fresh`) is not re-minted either.
RETRY_MINUTES = 15
# per hop; three hops, inside a poll or a Test request
TIMEOUT = 15


def is_custom(host_or_origin: str) -> bool:
    """True for a publication on its own domain; substack.com hosts take the
    pasted sid as is."""
    host = (urlsplit(host_or_origin).hostname if "://" in host_or_origin
            else host_or_origin) or ""
    host = host.lower().rstrip(".")
    return bool(host) and host != "substack.com" and not host.endswith(".substack.com")


def host_of(origin: str) -> str:
    return (urlsplit(origin).hostname or "").lower()


def ensure(con, origin: str) -> bool:
    """Make sure a custom domain has a session when one can be had: mint one
    when nothing is stored for the host or what is stored has expired. True
    when a live session is stored afterwards."""
    if con is None or not is_custom(origin):
        return False
    host = host_of(origin)
    row = credentials.session_get(con, SITE, host)
    if row and row["current"]:
        return bool(row["live"])
    return mint(con, origin)


def refresh(con, origin: str) -> bool:
    """A signed fetch on the host came back cut: mint the session again
    unless it was minted within the hour (then the account does not subscribe
    there, and minting again would only repeat the handshake every poll).
    True when a new live session was stored, so the caller fetches again."""
    if con is None or not is_custom(origin):
        return False
    row = credentials.session_get(con, SITE, host_of(origin))
    if row and row["fresh"]:
        return False
    return mint(con, origin)


def mint(con, origin: str) -> bool:
    """Run the cross-domain sign-in for the publication at `origin` with the
    stored sid and store the host's session. When a step does not answer as
    expected the failure is recorded so the next fetch does not repeat it at
    once: a session that is still live stays as it is (only its `fresh` clock
    restarts; one bad hop must not cost a working cookie), otherwise an empty
    row retried after RETRY_MINUTES. The table write runs in its own
    savepoint: a failure there (the credential deleted mid-cycle, say) is
    logged and must not abort the caller's transaction."""
    host = host_of(origin)
    origin = f"https://{host}"
    value = credentials.value(con, SITE)
    jar = credentials.substack_jar(value) if value else {}
    if "substack.sid" not in jar:
        return False
    session = None
    try:
        session = _handshake(origin, host, jar)
    except Exception as e:
        print(f"substack: sign-in to {host} failed ({type(e).__name__})")
    try:
        with con.transaction():
            if session:
                name, val, expires_at = session
                credentials.session_set(con, SITE, host, f"{name}={val}", expires_at)
                return True
            row = credentials.session_get(con, SITE, host)
            if row and row["live"]:
                credentials.session_touch(con, SITE, host)
            else:
                credentials.session_set(con, SITE, host, "", retry_minutes=RETRY_MINUTES)
    except Exception as e:
        print(f"substack: could not store the session for {host} ({type(e).__name__})")
    return False


def _handshake(origin: str, host: str, jar: dict) -> tuple[str, str, datetime | None] | None:
    """The three requests; (cookie name, value, expiry) or None. Every URL
    goes through fetch.get, so the private-network guard runs on each, and
    redirects are never followed blind: each Location is checked for the host
    it must be on before it is requested."""
    # 1. the publication's own login redirect names its Substack slug
    r = fetch.get(f"{origin}/account/login?redirect=%2F", timeout=TIMEOUT,
                  allow_wall=True, allow_redirects=False)
    location = r.headers.get("location") or ""
    parts = urlsplit(location)
    if r.status not in (301, 302, 303, 307, 308) or \
            (parts.hostname or "").lower() not in ("substack.com", "www.substack.com"):
        return None
    for_pub = (parse_qs(parts.query).get("for_pub") or [""])[0]
    if not for_pub or not for_pub.replace("-", "").replace("_", "").isalnum():
        return None
    # 2. substack.com signs the session over to the publication host
    r = fetch.get(f"{SIGN_IN}?redirect={quote(origin + '/', safe='')}&for_pub={for_pub}",
                  cookies=jar, timeout=TIMEOUT, allow_wall=True, allow_redirects=False)
    location = r.headers.get("location") or ""
    parts = urlsplit(location)
    if r.status not in (301, 302, 303, 307, 308) or \
            (parts.hostname or "").lower() != host or \
            not parts.path.startswith("/api/v1/sign-in/"):
        # 200 is the sign-in page: the sid no longer signs in
        return None
    # 3. the publication host completes it and sets its own session cookie
    r = fetch.get(location, timeout=TIMEOUT, allow_wall=True, allow_redirects=False)
    for name in ("connect.sid", "substack.sid"):
        val = (r.cookies or {}).get(name)
        if val:
            return name, val, _expiry((r.cookie_expires or {}).get(name))
    return None


def _expiry(epoch) -> datetime | None:
    """The cookie's expiry as an aware datetime, None when it has none (the
    session is then re-minted when it stops working)."""
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
