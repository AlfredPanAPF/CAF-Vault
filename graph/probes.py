"""Live credential checks for POST /api/credentials/{site}/test (build spec
v4 §7).

Each probe does the smallest real fetch that proves the stored sign-in works.
`run()` wraps every probe, so a failure of any kind comes back as
(False, "Sign-in failed: <reason>.") rather than an exception, and nothing
here returns or logs a credential value.

FT and WSJ need two answers, not one (docs/build-spec-v4-sources.md, hard
facts): whether the pasted session is live, and whether article pages are
reachable from this server at all. ft.com sits behind a Cloudflare JS
challenge and wsj.com behind DataDome, both of which reject non-browser
clients at the edge before any cookie is read. So the FT probe first asks
FT's own session service (`session-next.ft.com/<FTSession_s>`, no challenge)
whether the cookie is valid, then tries one article; a live session behind a
wall is reported as exactly that, so the operator does not re-paste a cookie
that is fine.
"""
import json

from . import credentials, fetch, router
from .connectors import manual, rss
from .connectors import x as x_connector
from .connectors import youtube as youtube_connector

TIMEOUT = 20
MIN_BODY = 400        # §7: the article body must arrive, not the teaser
OK = "Signed in."
WALLED = ("Signed in, but the article page came back behind {site}'s wall this "
          "time. Headlines still flow from the feeds; article text arrives when "
          "the browser clears the wall.")

# the public feeds the article probes read their newest item from
FEEDS = {"ft": "https://www.ft.com/rss/home/international",
         "wsj": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"}
FT_SESSION = "https://session-next.ft.com/"
READER_API = "https://substack.com/api/v1/reader/posts?limit=1"


def failed(reason) -> tuple[bool, str]:
    return False, f"Sign-in failed: {str(reason).strip().rstrip('.')[:200]}."


def _ft_session_live(con) -> bool | None:
    """True/False from FT's session service, None when it cannot be asked
    (no FTSession_s in the paste, or the service did not answer)."""
    value = credentials.value(con, "ft") or ""
    try:
        jar = credentials.cookie_jar(value, host="www.ft.com", site="ft")
    except credentials.InvalidCredential:
        return None
    token = jar.get("FTSession_s") or jar.get("FTSession")
    if not token:
        return None
    try:
        r = fetch.get(FT_SESSION + token, timeout=TIMEOUT, allow_wall=True)
    except Exception:
        return None
    if r.status == 200:
        return True
    if r.status == 404 and "not valid" in r.text:
        return False
    return None


def _article_probe(con, site: str) -> tuple[bool, str]:
    """FT / WSJ: newest item from the public feed, then the article page with
    the cookies attached. A wall raises SignInNeeded; a body under 400
    characters means the page came back without its text."""
    label = credentials.SITES[site]["label"]
    session = _ft_session_live(con) if site == "ft" else None
    if session is False:
        return failed("FT says this session is no longer valid; sign in again "
                      "and paste a fresh cookies.txt")
    text = fetch.get(FEEDS[site], site=site, con=con, timeout=TIMEOUT,
                     allow_wall=True).text
    item = next(iter(rss.items(text)), None)
    if item is None:
        return failed("the feed came back empty")
    try:
        html = fetch.get(item["link"], site=site, con=con, timeout=TIMEOUT).text
    except fetch.SignInNeeded as e:
        if session is True:
            return False, WALLED.format(site=label)
        raise e
    _title, _published, body = manual.extract_article(html, item["link"])
    if len(body) < MIN_BODY:
        if session is True:
            return False, WALLED.format(site=label)
        return failed("the article came back without its text")
    return True, OK


def substack_origins(con) -> list[str]:
    """Publication origins of the Substack sources we already follow."""
    return [r["origin"] for r in con.execute(
        "select distinct config->'substack'->>'origin' as origin from source "
        "where connector='rss' and status <> 'dropped' "
        "and config->'substack'->>'origin' is not null")]


def _substack_probe(con) -> tuple[bool, str]:
    """The reader API answers 200 with JSON for a signed-in session; if it
    does not, fall back to a paid post from a publication we follow."""
    r = fetch.get(READER_API, site="substack", con=con, timeout=TIMEOUT,
                  allow_wall=True, headers={"Accept": "application/json"})
    if r.ok:
        try:
            json.loads(r.text)
            return True, OK
        except ValueError:
            pass
    for origin in substack_origins(con):
        for post in router.substack_archive(origin, con=con, limit=5):
            if (post.get("audience") or "everyone") == "everyone":
                continue
            slug = post.get("slug") or rss.slug_of(post.get("canonical_url") or "")
            if not slug:
                continue
            # raises SignInNeeded when the paid post comes back as a preview
            rss.substack_post(con, origin, slug)
            return True, OK
    # both tails are the same outcome: the sign-in could not be proven. §7 fixes
    # the wording as "Sign-in failed: <reason>.", and the reason says what to
    # add rather than blaming the pasted cookie.
    if not substack_origins(con):
        return failed(f"the reader API answered {r.status} and there is no "
                      "Substack source yet to test a paid post against")
    return failed(f"the reader API answered {r.status} and no followed "
                  "publication has a paid post to test with")


PROBES = {
    "ft": lambda con: _article_probe(con, "ft"),
    "wsj": lambda con: _article_probe(con, "wsj"),
    "substack": _substack_probe,
    "x": lambda con: x_connector.check(con),
    "youtube": lambda con: youtube_connector.check(con),
}


def run(con, site: str) -> tuple[bool, str]:
    """Check one site's sign-in. Never raises."""
    probe = PROBES.get(site)
    if probe is None:
        return failed("nothing here checks that site")
    if not credentials.is_set(con, site):
        return False, "Nothing is saved for this sign-in yet."
    try:
        return probe(con)
    except fetch.SignInNeeded as e:
        return failed(e.detail or "the site asked for a sign-in")
    except Exception as e:
        return failed(str(e) or e.__class__.__name__)
