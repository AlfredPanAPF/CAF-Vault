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

Substack subscriptions are per publication, so no post this module could pick
proves anything about the account: the analyst pastes a link to a paid post
from a publication they subscribe to (the Sign-ins card's link box), and the
probe fetches that post with the stored cookie and once more without it. The
proof is that the cookie unlocks text an anonymous reader does not get; when
it does not, Substack's reader API (401 without a live session) tells a dead
session from a live one that simply does not subscribe there. A link that
cannot test anything (missing, not a Substack post, a free post, nothing at
that address, a post that reads the same without the sign-in) raises BadLink,
and the endpoint answers 400 without recording a check, since nothing was
learned about the sign-in.
"""
import json
from urllib.parse import urlsplit, urlunsplit

from . import credentials, fetch, router, substack_session
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

# Substack: what the link box must hold, and what a live session that does not
# subscribe to the pasted publication reads as
NEEDS_LINK = "Paste a link to a paid post you subscribe to."
NOT_A_POST = ("That is not a link to a Substack post. Paste a link to a paid "
              "post you subscribe to.")
FREE_POST = ("That post is free to read, so it cannot show whether the sign-in "
             "works. Paste a link to a paid post you subscribe to.")
NO_POST = "Substack has no post at that link. Check the link and try again."
UNREACHABLE = "Could not reach {host} to check the link. Try again in a moment."
NOT_SUBSCRIBED = ("Signed in, but {publication} still sent the preview: this "
                  "account has no paid subscription there. Test with a post "
                  "from a publication it pays for.")
# a publication on its own domain takes a session of its own, minted from the
# sid (substack_session.py); when that did not take while substack.com still
# signs in, the verdict is about the handshake, not the subscription
NO_HOST_SESSION = ("Signed in on substack.com, but {publication} did not accept "
                   "the sign-in this time. Try again in a moment.")
SAME_WITHOUT = ("That post reads the same without the sign-in, so it cannot show "
                "whether the sign-in works. Paste a link to a post with text "
                "behind the paywall.")


class BadLink(ValueError):
    """The pasted link cannot test the sign-in; str() says why."""


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


def substack_post_ref(url: str, con=None) -> tuple[str, str]:
    """A pasted Substack post link -> (publication origin, slug). Share links
    (`open.substack.com/pub/<pub>/p/<slug>`) are rewritten onto the
    publication; a custom domain is recognised the way the router does it
    (§4 rule 20, one small fetch). Raises BadLink when it is not a post."""
    text = (url or "").strip()
    if not text:
        raise BadLink(NEEDS_LINK)
    if "://" not in text:
        text = "https://" + text
    try:
        # urlsplit itself refuses some pastes (an unclosed bracket, a host
        # with characters that fail NFKC normalisation): not a post either
        text = router.substack_share(text)
        parts = urlsplit(text)
    except ValueError:
        raise BadLink(NOT_A_POST) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BadLink(NOT_A_POST)
    slug = rss.slug_of(text)
    if not slug:
        raise BadLink(NOT_A_POST)
    # the publication is reached over https whatever was pasted, and only on a
    # public host with a readable port: the fetch guard says no to the rest
    # here, before a fetch could turn a typo into a recorded failure
    site = urlunsplit(("https", parts.netloc, "", "", ""))
    try:
        fetch.guard_url(site)
    except fetch.BlockedAddress:
        raise BadLink(NOT_A_POST) from None
    # the router's own flag tells a host that is not a Substack from one that
    # did not answer
    state = {"failed": False}
    origin = router.substack_origin(site, con=con, state=state)
    if not origin:
        if state["failed"]:
            raise BadLink(UNREACHABLE.format(host=parts.hostname))
        raise BadLink(NOT_A_POST)
    return origin, slug


def _substack_session_live(con) -> bool | None:
    """True/False from Substack's reader API, which answers 200 JSON for a
    signed-in session and 401 for anyone else; None when it did not say (a
    403 is left there too: from a server that is an edge block, not a verdict
    on the cookie)."""
    try:
        r = fetch.get(READER_API, site="substack", con=con, timeout=TIMEOUT,
                      allow_wall=True, headers={"Accept": "application/json"})
    except Exception:
        return None
    if r.status == 200:
        try:
            json.loads(r.text)
            return True
        except ValueError:
            return None
    if r.status == 401:
        return False
    return None


def _substack_probe(con, url) -> tuple[bool, str]:
    """Fetch the pasted paid post with the stored cookie and once more without
    it. The proof of the sign-in is that the cookie unlocks text an anonymous
    reader does not get: a body that looks whole proves nothing on its own,
    since Substack hands some paid posts out whole (a discussion thread whose
    paid part is the comments, a post whose paywalled tail is a few links) and
    its word count cannot tell those from an unlocked post. When the cookie
    unlocked nothing, the reader API says whether the session itself is dead,
    and the body's length against the post's word count whether the account is
    simply not subscribed here or the post has no text behind its wall."""
    origin, slug = substack_post_ref(url, con=con)
    host = urlsplit(origin).hostname or origin
    # a publication on its own domain takes a session minted from the sid
    # (substack_session.py); Test is a human asking, so it runs the hand-over
    # itself when none is stored, past the backoff a failed one leaves behind
    if substack_session.is_custom(host) and \
            not credentials.session_jar(con, "substack", host):
        substack_session.mint(con, origin)
    try:
        signed = rss.substack_post_json(con, origin, slug)
    except RuntimeError as e:
        if str(e) == "HTTP 404":
            raise BadLink(NO_POST) from None
        raise
    if (signed.get("audience") or "everyone") == "everyone":
        raise BadLink(FREE_POST)
    try:
        anonymous = rss.substack_post_json(None, origin, slug, anonymous=True)
    except Exception:
        # the second fetch is the comparison; without it nothing was learned
        raise BadLink(UNREACHABLE.format(host=host)) from None
    if rss.html_words(signed.get("body_html") or "") > \
            rss.html_words(anonymous.get("body_html") or ""):
        return True, OK
    session = _substack_session_live(con)
    if session is False:
        return failed("Substack says this session is no longer valid; sign in "
                      "again and paste a fresh substack.sid")
    if rss.substack_preview(signed):
        if session is True:
            if substack_session.is_custom(host) and \
                    not credentials.session_jar(con, "substack", host):
                return False, NO_HOST_SESSION.format(publication=host)
            return False, NOT_SUBSCRIBED.format(publication=host)
        return failed("the post came back as a preview")
    raise BadLink(SAME_WITHOUT)


# every probe takes (con, url); only Substack reads the link
PROBES = {
    "ft": lambda con, url: _article_probe(con, "ft"),
    "wsj": lambda con, url: _article_probe(con, "wsj"),
    "substack": _substack_probe,
    "x": lambda con, url: x_connector.check(con),
    "youtube": lambda con, url: youtube_connector.check(con),
}


def run(con, site: str, url: str | None = None) -> tuple[bool, str]:
    """Check one site's sign-in, against the pasted link where the site takes
    one. Never raises, except BadLink for a link that cannot test anything."""
    probe = PROBES.get(site)
    if probe is None:
        return failed("nothing here checks that site")
    if not credentials.is_set(con, site):
        return False, "Nothing is saved for this sign-in yet."
    try:
        return probe(con, url)
    except BadLink:
        raise
    except fetch.SignInNeeded as e:
        return failed(e.detail or "the site asked for a sign-in")
    except Exception as e:
        return failed(str(e) or e.__class__.__name__)
