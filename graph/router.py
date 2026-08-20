"""URL router: one pasted link -> what it is and how to poll it (build spec
v4 §4).

The sources page has a single URL box. `resolve()` walks the §4 rule table in
order and returns the first match: a native feed, a podcast, a YouTube channel
or playlist, an X account, a bridge feed, a one-off link for the link queue, or
`unsupported` with a reason the page can show.

Rules 1-19 are pattern matches on the normalized URL (no network except where a
resolution step is named). Rules 20-24 fetch: every network step gets 8 s and
512 KB, a failure falls through to the next rule, and a final failure produces
`unsupported` with "Could not reach that address.". One resolve is also capped
end to end (`BUDGET`): the sources page calls this endpoint on every paste, so
a slow host must not hold an API worker for the sum of every rule's timeout.

The helpers here (feed sniffing, autodiscovery, article detection, YouTube
channel-id resolution, the Substack probes) are shared with the connectors and
the API so the same judgement is made at add time and at poll time.
"""
import json
import re
import time
from dataclasses import asdict, dataclass, field
from html import unescape
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from . import bridge, credentials, fetch

TIMEOUT = 8                      # §4: every network step
BUDGET = 25                      # seconds for one resolve, all steps together
NO_FEED = ("No feed found at this address. Paste an article link to add it "
           "once, or a feed URL.")
UNREACHABLE = "Could not reach that address."

# muted notes shown under the preview line (v4 §8)
NOTES = {
    "youtube": "Videos are transcribed from captions.",
    "x": "Posts are fetched with the X token.",
    "ft": "Full text needs the FT sign-in.",
    "wsj": "Full text needs the WSJ sign-in.",
    "substack": "Paid posts need the Substack sign-in.",
}

YT_FEED = "https://www.youtube.com/feeds/videos.xml"
YT_CONSENT = {"SOCS": "CAI", "CONSENT": "YES+1"}   # skips the consent wall
# Live Dow Jones feed host (verified 2026-08-18). The legacy
# feeds.a.dj.com/rss/<name>.xml host still answers 200 but has been frozen at
# 27 Jan 2025, so pasted legacy URLs are mapped onto this one.
WSJ_FEED_BASE = "https://feeds.content.dowjones.io/public/rss/"
WSJ_LEGACY_HOST = "feeds.a.dj.com"
# §4 rule 13; finance rides with markets (the public WSJ feeds are the only
# targets available; every slug verified live 2026-08-20).
WSJ_SECTION_FEEDS = {
    "": "RSSWorldNews",
    "markets": "RSSMarketsMain",
    "finance": "RSSMarketsMain",
    "business": "WSJcomUSBusiness",
    "economy": "socialeconomyfeed",
    "tech": "RSSWSJD",
    "world": "RSSWorldNews",
    "us-news": "RSSUSnews",
    "politics": "socialpoliticsfeed",
    "opinion": "RSSOpinion",
    "personal-finance": "RSSPersonalFinance",
    "real-estate": "latestnewsrealestate",
    "lifestyle": "RSSLifestyle",
    "style": "RSSStyle",
    "health": "socialhealth",
    "sports": "rsssportsfeed",
    "arts-culture": "RSSArtsCulture",
}
WSJ_FEED_SECTIONS = {"RSSMarketsMain": "markets",
                     "RSSMarkets": "markets",
                     "WSJcomUSBusiness": "business",
                     "socialeconomyfeed": "economy",
                     "RSSWSJD": "tech",
                     "RSSWorldNews": "world",
                     "RSSUSnews": "US news",
                     "socialpoliticsfeed": "politics",
                     "RSSOpinion": "opinion",
                     "RSSPersonalFinance": "personal finance",
                     "latestnewsrealestate": "real estate",
                     "RSSLifestyle": "lifestyle",
                     "RSSStyle": "style",
                     "socialhealth": "health",
                     "rsssportsfeed": "sports",
                     "RSSArtsCulture": "arts and culture"}
# §4 rule 13 (listing half): /news/ pages that list a column, an article type
# or an author have no public feed; the page itself is polled through the
# browser path (rss.wsj_section_items). These /news/ paths are not listings.
WSJ_NEWS_NOT_LISTINGS = {"rss-news-and-feeds", "archive", "types", "author"}
WSJ_ARTICLE_SECTIONS = ("finance", "business", "tech", "economy", "politics",
                        "world", "opinion")
X_RESERVED = {"home", "search", "i", "explore"}


# ---------------------------------------------------------------- resolution


@dataclass
class Resolution:
    """What a pasted URL turned out to be (build spec v4 §4)."""

    kind: str                            # feed|podcast|youtube|x|bridge|link|unsupported
    connector: str | None = None
    label: str = ""                      # user-facing type
    url: str = ""                        # normalized input
    name: str | None = None              # suggested source name
    feed_url: str | None = None
    site: str | None = None              # ft | wsj | substack | youtube | x | None
    config: dict = field(default_factory=dict)
    one_off: bool = False                # True -> link_queue, not a source
    link_kind: str | None = None
    credential: dict | None = None       # {"site", "set"} when the kind needs one
    message: str | None = None           # unsupported reason or a note

    def as_dict(self) -> dict:
        """JSON-safe dict for the API."""
        return asdict(self)


# ---------------------------------------------------------------- normalize

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)
_DROP_PARAMS = {"fbclid", "gclid", "mod", "ref"}
_DROP_PREFIXES = ("utm_", "syn-", "syn_")
_X_HOSTS = {"x.com", "www.x.com", "m.x.com", "mobile.x.com", "twitter.com",
            "www.twitter.com", "m.twitter.com", "mobile.twitter.com"}
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
             "music.youtube.com"}


def _drop_param(key: str) -> bool:
    k = key.lower()
    return k in _DROP_PARAMS or k.startswith(_DROP_PREFIXES)


def normalize(url: str) -> str:
    """Add a scheme, lowercase the host, drop the fragment and tracking
    parameters, strip the trailing slash. Twitter hosts collapse to x.com,
    youtu.be and m.youtube.com to www.youtube.com (build spec v4 §4).

    Anything that is not an http(s) URL comes back as "" so the caller shows
    "That does not look like a link." Returning the pasted string instead would
    skip the rebuild below, and with it the strip of any user:password in the
    address — which then lands in a log line and in source.url."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not _SCHEME_RE.match(raw):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        parts.port                                   # raises on a bad netloc
    except ValueError:
        return ""
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return ""
    # 'postgres.' and 'postgres' are one name to DNS; without this the trailing
    # dot walks past every host check downstream
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port
    path = parts.path or ""
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _drop_param(k)]
    if host in _X_HOSTS:
        host = "x.com"
    elif host in _YT_HOSTS:
        host = "www.youtube.com"
    elif host in ("youtu.be", "www.youtu.be"):
        video = path.strip("/").split("/")[0] if path.strip("/") else ""
        host, path = "www.youtube.com", "/watch"
        query = [("v", video)] if video else []
    netloc = host + (f":{port}" if port and port not in (80, 443) else "")
    if path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


# ---------------------------------------------------------------- small parsers


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf'{name}\s*=\s*"([^"]*)"', tag, re.I) or \
        re.search(rf"{name}\s*=\s*'([^']*)'", tag, re.I)
    return m.group(1) if m else None


def _tag_text(xml: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?"
                  rf"</(?:\w+:)?{name}>", xml, re.S | re.I)
    return unescape(re.sub(r"\s+", " ", m.group(1)).strip()) if m else ""


def parse_feed_head(text: str) -> dict:
    """Feed XML -> {kind, title, is_atom, generator}. `kind` is 'podcast' when
    an item carries an audio enclosure, else 'feed' (build spec v4 §4 rule 21).
    Atom feeds are recognised by their root element."""
    root = text[:4000]
    is_atom = bool(re.search(r"<feed[\s>]", root, re.I)) and \
        not re.search(r"<rss[\s>]", root, re.I)
    podcast = bool(re.search(r"""<enclosure\b[^>]*type\s*=\s*["']audio/""",
                             text, re.I))
    return {"kind": "podcast" if podcast else "feed",
            "title": _tag_text(text, "title"),
            "is_atom": is_atom,
            "generator": _tag_text(text, "generator")}


def autodiscover(html: str, base_url: str) -> list[str]:
    """Feed hrefs from <link rel="alternate" type="application/rss+xml|
    atom+xml"> tags, absolute and in document order (§4 rule 22)."""
    out = []
    for m in re.finditer(r"<link\b[^>]*>", html, re.I):
        tag = m.group(0)
        rel = (_attr(tag, "rel") or "").lower()
        typ = (_attr(tag, "type") or "").lower()
        href = _attr(tag, "href")
        if not href or "alternate" not in rel:
            continue
        if "rss+xml" not in typ and "atom+xml" not in typ:
            continue
        full = urljoin(base_url, unescape(href.strip()))
        if full not in out:
            out.append(full)
    return out


def _p_text_len(block: str) -> int:
    total = 0
    for m in re.finditer(r"<p\b[^>]*>(.*?)</p>", block, re.S | re.I):
        total += len(unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip())
    return total


def is_article_html(html: str) -> bool:
    """True when the page looks like a single article: og:type article, a
    JSON-LD NewsArticle/Article, or an <article> holding more than 1,500
    characters of paragraph text (§4 rule 23)."""
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        tag = m.group(0)
        prop = (_attr(tag, "property") or _attr(tag, "name") or "").lower()
        if prop == "og:type" and (_attr(tag, "content") or "").lower() == "article":
            return True
    for m in re.finditer(r"<script\b[^>]*ld\+json[^>]*>(.*?)</script>",
                         html, re.S | re.I):
        if re.search(r'"@type"\s*:\s*(?:\[[^\]]*)?"(?:NewsArticle|Article|'
                     r'ReportageNewsArticle|BlogPosting)"', m.group(1)):
            return True
    for m in re.finditer(r"<article\b.*?</article>", html, re.S | re.I):
        if _p_text_len(m.group(0)) > 1500:
            return True
    return False


# ---------------------------------------------------------------- network steps


def _left(state: dict | None) -> float:
    """Seconds still available for this resolve (TIMEOUT when no deadline is
    threaded through, e.g. a helper called on its own)."""
    if not state or "deadline" not in state:
        return float(TIMEOUT)
    return state["deadline"] - time.monotonic()


def _head(url: str, con=None, state: dict | None = None, **kw):
    """fetch.head with the §4 budget. Returns None on any transport error, on a
    blocked address, or once the resolve deadline has passed, and remembers
    that a network step failed."""
    left = _left(state)
    if left <= 0:
        if state is not None:
            state["failed"] = True
        return None
    try:
        return fetch.head(url, con=con, timeout=max(1, int(min(TIMEOUT, left))),
                          **kw)
    except Exception as e:                       # transport, TLS, timeout
        # the host only: a pasted URL can carry a token in its query string
        print(f"router: fetch failed for {fetch.host_of(url)} "
              f"({type(e).__name__})")
        if state is not None:
            state["failed"] = True
        return None


def youtube_channel_id(page_html: str) -> tuple[str | None, str | None]:
    """Channel page HTML -> (channel_id, title), read from
    <link rel="canonical">, "externalId", and og:title (§6.1)."""
    channel_id = None
    for m in re.finditer(r"<link\b[^>]*>", page_html, re.I):
        tag = m.group(0)
        if (_attr(tag, "rel") or "").lower() != "canonical":
            continue
        hit = re.search(r"/channel/(UC[\w-]{10,})", _attr(tag, "href") or "")
        if hit:
            channel_id = hit.group(1)
            break
    if not channel_id:
        m = re.search(r'"externalId"\s*:\s*"(UC[\w-]{10,})"', page_html)
        if m:
            channel_id = m.group(1)
    title = None
    for m in re.finditer(r"<meta\b[^>]*>", page_html, re.I):
        tag = m.group(0)
        if (_attr(tag, "property") or _attr(tag, "name") or "").lower() == "og:title":
            title = (_attr(tag, "content") or "").strip() or None
            break
    if not title:
        raw = _tag_text(page_html, "title")
        title = raw.removesuffix(" - YouTube").strip() or None
    return channel_id, unescape(title) if title else None


def _ytdlp_channel(url: str) -> tuple[str | None, str | None]:
    """Fallback for resolve_youtube_channel: yt-dlp extract_flat. Imported
    lazily so tests and the API never pay for it."""
    try:
        import yt_dlp
        opts = {"quiet": True, "skip_download": True, "extract_flat": True,
                "playlist_items": "0", "no_warnings": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        return (info.get("channel_id") or info.get("uploader_id"),
                info.get("channel") or info.get("uploader") or info.get("title"))
    except Exception as e:
        print(f"router: yt-dlp channel lookup failed for {url} ({e})")
        return None, None


def resolve_youtube_channel(url: str, con=None, state: dict | None = None
                            ) -> tuple[str | None, str | None]:
    """Channel URL (@handle, /c/, /user/) -> (channel_id, title). Reads the
    page with the consent cookies first and only falls back to yt-dlp when the
    page will not parse (§6.1)."""
    r = _head(url, con=con, state=state, cookies=dict(YT_CONSENT))
    if r is not None and r.ok:
        channel_id, title = youtube_channel_id(r.text)
        if channel_id:
            return channel_id, title
    return _ytdlp_channel(url)


def _yt_feed_title(feed_url: str, con=None, state: dict | None = None) -> str:
    """The channel or playlist name from the native feed's <title>; "" when
    the feed does not answer (the caller falls back to the id)."""
    for _ in range(2):   # videos.xml answers a stray 404/5xx now and then
        r = _head(feed_url, con=con, state={"failed": False})
        if r is not None and r.ok:
            return parse_feed_head(r.text)["title"] or ""
    return ""


def youtube_feed(channel_id: str | None = None,
                 playlist_id: str | None = None) -> str:
    if playlist_id:
        return f"{YT_FEED}?playlist_id={playlist_id}"
    return f"{YT_FEED}?channel_id={channel_id}"


def itunes_lookup(podcast_id: str, con=None, state: dict | None = None) -> dict:
    """Apple Podcasts id -> the iTunes lookup result ({} on failure); the
    podcast feed is its `feedUrl` (§4 rule 14)."""
    r = _head(f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast",
              con=con, state=state)
    if r is None or not r.ok:
        return {}
    try:
        data = json.loads(r.text)
    except ValueError:
        return {}
    results = data.get("results") if isinstance(data, dict) else None
    return results[0] if results and isinstance(results[0], dict) else {}


def ft_section_feed(url: str) -> str:
    """FT section page -> its `?format=rss` feed URL (§4 rule 10). The caller
    verifies it by fetching."""
    parts = urlsplit(url)
    path = (parts.path or "").rstrip("/")
    if not path:
        return f"{parts.scheme}://{parts.netloc}/rss/home"
    return f"{parts.scheme}://{parts.netloc}{path}?format=rss"


def wsj_section_feed(path: str) -> str | None:
    """WSJ section root -> one of the public dj.com feeds (§4 rule 13)."""
    seg = [s for s in (path or "").split("/") if s]
    if len(seg) > 1:
        return None
    key = seg[0].lower() if seg else ""
    name = WSJ_SECTION_FEEDS.get(key)
    return WSJ_FEED_BASE + name if name else None


def wsj_listing(seg: list[str]) -> str | None:
    """The listing name for a WSJ /news/ page, or None when the path is not
    one (§4 rule 13, listing half). Columns (`/news/heard-on-the-street`) have
    no public feed — Dow Jones's directory was checked 2026-08-20 — so the
    page itself is the thing polled; article types (`/news/types/<slug>`) and
    authors (`/news/author/<slug>`) render the same listing shape."""
    if not seg or seg[0] != "news":
        return None
    if len(seg) == 2 and seg[1].lower() not in WSJ_NEWS_NOT_LISTINGS:
        return seg[1].lower()
    if len(seg) == 3 and seg[1].lower() in ("types", "author"):
        return f"{seg[1].lower()}/{seg[2].lower()}"
    return None


def wsj_listing_name(listing: str) -> str:
    """A suggested source name: "WSJ heard on the street"; author slugs are
    people, so their words are capitalized ("WSJ Greg Ip")."""
    kind, _, slug = listing.rpartition("/")
    words = slug.replace("-", " ")
    if kind == "author":
        words = words.title()
    return f"WSJ {words}"


def substack_share(url: str) -> str:
    """Substack's own share and email form, `open.substack.com/pub/<pub>/p/
    <slug>`, rewritten onto the publication itself:
    `https://<pub>.substack.com/p/<slug>` (§4 rule 20). Any other URL comes
    back unchanged. Without this the host ends with `.substack.com` while the
    path starts with `/pub`, so a pasted post reads as a publication and the
    source added is Substack's own site feed."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in ("open.substack.com", "substack.com", "www.substack.com"):
        return url
    seg = [s for s in (parts.path or "").split("/") if s]
    if len(seg) < 2 or seg[0] != "pub":
        return url
    rest = "/" + "/".join(seg[2:]) if len(seg) > 2 else ""
    return urlunsplit(("https", f"{seg[1]}.substack.com", rest, parts.query, ""))


def substack_origin(url: str, con=None, state: dict | None = None) -> str | None:
    """The publication origin when the site is a Substack: `<origin>/feed`
    carries <generator>Substack</generator>, or `/api/v1/archive?limit=1`
    answers with JSON carrying a publication_id (§4 rule 20). None otherwise."""
    parts = urlsplit(url if "://" in url else "https://" + url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if (parts.hostname or "").lower().endswith(".substack.com"):
        return origin
    r = _head(origin + "/feed", con=con, state=state)
    if r is not None:
        # every Substack publication, custom domain or not, answers with
        # `x-cluster: substack` (and `x-sub: <slug>`); the feed's generator
        # tag is the second signal (docs/rss-bridge-brief.md)
        if (r.headers.get("x-cluster") or "").lower() == "substack":
            return origin
        if r.ok and "substack" in (parse_feed_head(r.text)["generator"] or "").lower():
            return origin
    # the host answered /feed, so a failed archive probe is not "unreachable":
    # its own failure flag, the deadline still shared (as the well-known paths
    # in resolve do)
    for post in substack_archive(origin, con=con, limit=1,
                                 state=dict(state, failed=False)
                                 if state is not None else None):
        if post.get("publication_id"):
            return origin
    return None


def substack_archive(origin: str, con=None, state: dict | None = None,
                     limit: int = 12) -> list[dict]:
    """`<origin>/api/v1/archive?sort=new&limit=N` -> the post list ([] on any
    failure). Used to tell a paid publication from a free one."""
    r = _head(f"{origin}/api/v1/archive?sort=new&limit={limit}", con=con,
              state=state)
    if r is None or not r.ok:
        return []
    try:
        data = json.loads(r.text)
    except ValueError:
        return []
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def substack_has_paid(origin: str, con=None, state: dict | None = None) -> bool:
    """True when a recent post is not open to everyone; only then does the
    Substack cookie matter for this publication (§4 credential note)."""
    return any((p.get("audience") or "everyone") != "everyone"
               for p in substack_archive(origin, con=con, state=state))


# ---------------------------------------------------------------- builders

_AUTO = object()


def _credential(con, site: str | None) -> dict | None:
    if not site or site not in credentials.SITES:
        return None
    is_set = bool(con is not None and credentials.is_set(con, site))
    return {"site": site, "set": is_set}


def _note(site: str | None, cred: dict | None) -> str | None:
    if site in ("youtube", "x"):
        return NOTES[site]
    if cred and not cred["set"] and site in NOTES:
        return NOTES[site]
    return None


def _build(url, kind, connector, label, *, name=None, feed_url=None, site=None,
           config=None, one_off=False, link_kind=None, con=None,
           credential=_AUTO, message=_AUTO) -> Resolution:
    cfg = dict(config or {})
    if site and not one_off:
        cfg.setdefault("site", site)
    cred = _credential(con, site) if credential is _AUTO else credential
    note = _note(site, cred) if message is _AUTO else message
    return Resolution(kind=kind, connector=connector, label=label, url=url,
                      name=name, feed_url=feed_url, site=site, config=cfg,
                      one_off=one_off, link_kind=link_kind, credential=cred,
                      message=note)


def _dedupe(seq):
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _unsupported(url: str, message: str) -> Resolution:
    return Resolution(kind="unsupported", connector=None, label="Unsupported",
                      url=url, message=message)


def _host_name(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _bridge_display(name: str) -> str:
    return name[:-6] if name.endswith("Bridge") and len(name) > 6 else name


def _wsj_slug(last: str) -> bool:
    """WSJ article slugs end in a short hex/word id (§4 rule 12)."""
    m = re.search(r"-([0-9a-z]{6,})$", last, re.I)
    return bool(m and any(c.isdigit() for c in m.group(1)))


def _first_param(params: dict) -> str:
    for key, value in (params or {}).items():
        if key in ("context", "format", "action") or value in (None, ""):
            continue
        return str(value)
    return ""


def _youtube_from_feed(url, feed_url, title, con) -> Resolution | None:
    """A videos.xml feed (pasted directly, or found by autodiscovery on a
    channel page) belongs to the youtube connector, not rss: transcripts come
    from the video ids, not from the feed."""
    if not feed_url.startswith(YT_FEED):
        return None
    q = dict(parse_qsl(urlsplit(feed_url).query))
    channel_id, playlist_id = q.get("channel_id"), q.get("playlist_id")
    if not (channel_id or playlist_id):
        return None
    cfg = {"channel_id": channel_id} if channel_id else {"playlist_id": playlist_id}
    if title:
        cfg["title"] = title
    return _build(url, "youtube", "youtube",
                  "YouTube channel" if channel_id else "YouTube playlist",
                  name=title or channel_id or playlist_id,
                  site="youtube",
                  feed_url=youtube_feed(channel_id=channel_id,
                                        playlist_id=playlist_id),
                  config=cfg, con=con)


def _feed_from_text(url, feed_url, text, con, state, *, site=None,
                    label=None, host="", headers=None):
    """Rule 21/22 tail: a fetched feed body -> a feed or podcast Resolution,
    with the Substack checks folded in. Both of rule 20's cheap signals are
    read here — the `<generator>` tag and the `x-cluster: substack` response
    header — so a publication on a custom domain is recognised from the fetch
    the rules already made, with no extra probe."""
    info = parse_feed_head(text)
    title = info["title"]
    yt = _youtube_from_feed(url, feed_url, title, con)
    if yt is not None:
        return yt
    if not site and (headers or {}).get("x-cluster", "").lower() == "substack":
        site = "substack"
    if not site and "substack" in (info["generator"] or "").lower():
        site = "substack"
    if site == "substack":
        parts = urlsplit(feed_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cred = (_credential(con, "substack")
                if substack_has_paid(origin, con=con, state=state) else None)
        return _build(url, "feed", "rss", label or "Substack",
                      name=title or _host_name(urlsplit(feed_url).hostname or host),
                      feed_url=feed_url, site="substack",
                      config={"substack": {"origin": origin}}, con=con,
                      credential=cred, message=_note("substack", cred))
    if info["kind"] == "podcast":
        return _build(url, "podcast", "podcast", "Podcast",
                      name=f"podcast:{title}" if title else f"podcast:{host}",
                      feed_url=feed_url, site=site, con=con)
    return _build(url, "feed", "rss", label or "News feed",
                  name=title or _host_name(host), feed_url=feed_url, site=site,
                  con=con)


# ---------------------------------------------------------------- the router


def resolve(con, raw_url: str) -> Resolution:
    """A pasted URL -> the first matching rule in the build spec v4 §4 table."""
    url = substack_share(normalize(raw_url))
    state = {"failed": False, "deadline": time.monotonic() + BUDGET}
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return _unsupported(url, NO_FEED)
    # a private or single-label address is answered like any other address we
    # cannot use, and never fetched (the rules below reach the network)
    if not url or not host or fetch.blocked_host(host):
        return _unsupported(url, NO_FEED)
    bare = _host_name(host)
    path = parts.path or ""
    seg = [s for s in path.split("/") if s]
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    origin = f"{parts.scheme}://{parts.netloc}"
    yt = host == "www.youtube.com"

    # 1 youtube video
    if yt and path == "/watch" and query.get("v"):
        video_id = query["v"]
        return _build(url, "link", "link", "YouTube video", name=video_id,
                      site="youtube", one_off=True, link_kind="youtube_video",
                      config={"video_id": video_id}, con=con)
    # 2 youtube short
    if yt and len(seg) >= 2 and seg[0] == "shorts":
        return _build(url, "link", "link", "YouTube video", name=seg[1],
                      site="youtube", one_off=True, link_kind="youtube_video",
                      config={"video_id": seg[1]}, con=con)
    # 3 youtube playlist
    if yt and path == "/playlist" and query.get("list"):
        list_id = query["list"]
        feed_url = youtube_feed(playlist_id=list_id)
        return _build(url, "youtube", "youtube", "YouTube playlist",
                      name=_yt_feed_title(feed_url, con, state) or f"Playlist {list_id}",
                      site="youtube", feed_url=feed_url,
                      config={"playlist_id": list_id}, con=con)
    # 4 youtube channel id
    if yt and len(seg) >= 2 and seg[0] == "channel":
        channel_id = seg[1]
        feed_url = youtube_feed(channel_id=channel_id)
        return _build(url, "youtube", "youtube", "YouTube channel",
                      name=_yt_feed_title(feed_url, con, state) or channel_id,
                      site="youtube", feed_url=feed_url,
                      config={"channel_id": channel_id}, con=con)
    # 5 youtube handle / custom name
    if yt and seg and (seg[0].startswith("@") or
                       (seg[0] in ("c", "user") and len(seg) > 1)):
        named = seg[:1] if seg[0].startswith("@") else seg[:2]
        handle = named[0] if named[0].startswith("@") else named[1]
        page = "https://www.youtube.com/" + "/".join(named)
        channel_id, title = resolve_youtube_channel(page, con=con, state=state)
        if channel_id:
            cfg = {"channel_id": channel_id}
            if handle:
                cfg["handle"] = handle
            if title:
                cfg["title"] = title
            return _build(url, "youtube", "youtube", "YouTube channel",
                          name=title or handle or channel_id,
                          site="youtube", feed_url=youtube_feed(channel_id=channel_id),
                          config=cfg, con=con)
    # 6 x post
    if host == "x.com" and len(seg) >= 3 and seg[1] == "status":
        return _build(url, "link", "link", "X post", name=f"@{seg[0]}", site="x",
                      one_off=True, link_kind="x_post",
                      config={"username": seg[0], "post_id": seg[2]}, con=con)
    # 7 x account
    if host == "x.com" and len(seg) == 1 and seg[0].lower() not in X_RESERVED:
        user = seg[0].lstrip("@")
        return _build(url, "x", "x", "X account", name=f"@{user}", site="x",
                      config={"username": user}, con=con)
    # 8 ft article
    if bare == "ft.com" and len(seg) >= 2 and seg[0] == "content":
        return _build(url, "link", "link", "Article", name=bare, site="ft",
                      one_off=True, link_kind="article", con=con)
    # 9 ft feed
    if bare == "ft.com" and (seg[:1] == ["rss"] or query.get("format") == "rss"):
        section = seg[1] if seg[:1] == ["rss"] and len(seg) > 1 else (seg[-1] if seg else "")
        return _build(url, "feed", "rss", "FT feed",
                      name=f"FT {section}" if section else "FT", feed_url=url,
                      site="ft", con=con)
    # 10 ft section page
    if bare == "ft.com":
        feed_url = ft_section_feed(url)
        r = _head(feed_url, con=con, state=state)
        if r is not None and r.ok and fetch.looks_like_feed(r):
            info = parse_feed_head(r.text)
            section = seg[-1] if seg else "home"
            return _build(url, "feed", "rss", "FT feed",
                          name=info["title"] or f"FT {section}",
                          feed_url=feed_url, site="ft", con=con)
    # 11 wsj public feed hosts: the live one as pasted, the frozen legacy
    # one remapped onto it (same slugs, no .xml)
    if host in ("feeds.content.dowjones.io", WSJ_LEGACY_HOST):
        slug = (seg[-1] if seg else "").removesuffix(".xml")
        section = WSJ_FEED_SECTIONS.get(slug, "")
        feed_url = url if host != WSJ_LEGACY_HOST else WSJ_FEED_BASE + slug
        return _build(url, "feed", "rss", "WSJ feed",
                      name=f"WSJ {section}" if section else "WSJ", feed_url=feed_url,
                      site="wsj", con=con)
    if bare == "wsj.com":
        last = seg[-1] if seg else ""
        # 12 wsj article
        if (len(seg) >= 2 and seg[0] == "articles") or \
                (len(seg) >= 2 and seg[0] in WSJ_ARTICLE_SECTIONS and _wsj_slug(last)):
            return _build(url, "link", "link", "Article", name=bare, site="wsj",
                          one_off=True, link_kind="article", con=con)
        # 13 wsj section root
        feed_url = wsj_section_feed(path)
        if feed_url:
            section = seg[0] if seg else "world"
            return _build(url, "feed", "rss", "WSJ feed", name=f"WSJ {section}",
                          feed_url=feed_url, site="wsj", con=con)
        # 13 (listing half) wsj /news/ column, type or author page: no public
        # feed exists, so the page itself is the feed the poller reads
        # (rss.wsj_section_items through the browser path). Without the WSJ
        # sign-in the page is walled end to end, so the note says so.
        listing = wsj_listing(seg)
        if listing:
            cred = _credential(con, "wsj")
            note = ("The page needs the WSJ sign-in."
                    if cred and not cred["set"] else None)
            return _build(url, "feed", "rss", "WSJ section",
                          name=wsj_listing_name(listing), feed_url=url,
                          site="wsj", config={"wsj_section": listing},
                          con=con, credential=cred, message=note)
    # 14 apple podcasts
    if host == "podcasts.apple.com":
        m = re.search(r"/id(\d+)", path)
        if m:
            hit = itunes_lookup(m.group(1), con=con, state=state)
            feed_url = hit.get("feedUrl")
            if feed_url:
                title = hit.get("collectionName") or hit.get("trackName") or ""
                return _build(url, "podcast", "podcast", "Podcast",
                              name=f"podcast:{title}" if title else f"podcast:{bare}",
                              feed_url=feed_url, con=con)
    # 15 bluesky profile
    if bare == "bsky.app" and len(seg) == 2 and seg[0] == "profile":
        return _build(url, "feed", "rss", "News feed", name=seg[1],
                      feed_url=f"https://bsky.app/profile/{seg[1]}/rss", con=con)
    # 16 telegram channel (invite links carry no channel name)
    if host == "t.me" and seg and seg[0] != "joinchat" and not seg[0].startswith("+"):
        channel = seg[1] if seg[0] == "s" and len(seg) > 1 else seg[0]
        params = {"username": channel}
        return _build(url, "bridge", "bridge", "Telegram",
                      name=channel,
                      feed_url=bridge.display_url("TelegramBridge", params),
                      config={"bridge": {"name": "TelegramBridge", "params": params}},
                      con=con)
    # 17 reddit subreddit
    if bare == "reddit.com" and len(seg) >= 2 and seg[0] == "r":
        sub = seg[1]
        feed_url = f"https://www.reddit.com/r/{sub}/.rss"
        r = _head(feed_url, con=con, state=state)
        if r is not None and r.ok and fetch.looks_like_feed(r):
            info = parse_feed_head(r.text)
            return _build(url, "feed", "rss", "News feed",
                          name=info["title"] or f"r/{sub}", feed_url=feed_url,
                          con=con)
        params = {"context": "single", "r": sub}
        return _build(url, "bridge", "bridge", "Reddit",
                      name=f"r/{sub}",
                      feed_url=bridge.display_url("RedditBridge", params),
                      config={"bridge": {"name": "RedditBridge", "params": params}},
                      con=con)
    # 18 github repo releases
    if bare == "github.com" and len(seg) == 2:
        return _build(url, "feed", "rss", "News feed", name=f"{seg[0]}/{seg[1]}",
                      feed_url=f"https://github.com/{seg[0]}/{seg[1]}/releases.atom",
                      con=con)
    # 19 medium
    if bare == "medium.com" and seg and seg[0].startswith("@"):
        return _build(url, "feed", "rss", "News feed", name=seg[0],
                      feed_url=f"https://medium.com/feed/{seg[0]}", con=con)
    if host.endswith(".medium.com"):
        return _build(url, "feed", "rss", "News feed", name=_host_name(host),
                      feed_url=f"{origin}/feed", con=con)
    # 20 substack
    is_post = len(seg) >= 2 and seg[0] == "p"
    if host.endswith(".substack.com") or is_post:
        pub = origin if host.endswith(".substack.com") else \
            substack_origin(url, con=con, state=state)
        if pub and is_post:
            cred = (_credential(con, "substack")
                    if substack_has_paid(pub, con=con, state=state) else None)
            return _build(url, "link", "link", "Substack post",
                          name=_host_name(host), site="substack", one_off=True,
                          link_kind="substack_post", con=con, credential=cred,
                          message=_note("substack", cred))
        if pub:
            # the publication branch commits to a feed, so it verifies one,
            # the way rules 10 and 17 do; otherwise the table keeps walking
            r = _head(pub + "/feed", con=con, state=state)
            if r is not None and r.ok and fetch.looks_like_feed(r):
                title = parse_feed_head(r.text)["title"]
                cred = (_credential(con, "substack")
                        if substack_has_paid(pub, con=con, state=state) else None)
                return _build(url, "feed", "rss", "Substack",
                              name=title or _host_name(host),
                              feed_url=pub + "/feed", site="substack",
                              config={"substack": {"origin": pub}},
                              con=con, credential=cred,
                              message=_note("substack", cred))
    # 21 the URL itself is a feed
    r = _head(url, con=con, state=state)
    if r is not None and r.ok:
        if fetch.looks_like_feed(r):
            return _feed_from_text(url, url, r.text, con, state, host=host,
                                   headers=r.headers)
        # 22 autodiscovery
        for href in autodiscover(r.text, url)[:2]:
            f = _head(href, con=con, state=state)
            if f is not None and f.ok and fetch.looks_like_feed(f):
                return _feed_from_text(url, href, f.text, con, state,
                                       host=host, headers=f.headers)
        # 23 article page
        if is_article_html(r.text):
            site = "ft" if bare == "ft.com" else ("wsj" if bare == "wsj.com" else None)
            return _build(url, "link", "link", "Article", name=bare, site=site,
                          one_off=True, link_kind="article", con=con)
    # 23b well-known feed paths: sites whose front page is walled or JS-only
    # for a server (403, empty shell) often still serve WordPress /feed,
    # Ghost /rss, Jekyll /feed.xml or Hugo /index.xml. Only when the host
    # answered at all, so an unreachable host does not cost four timeouts.
    if r is not None and bare not in ("ft.com", "wsj.com"):
        base = url.split("?")[0].rstrip("/")
        for candidate in _dedupe([f"{base}/feed", f"{origin}/feed", f"{origin}/rss",
                                  f"{origin}/feed.xml", f"{origin}/index.xml"]):
            # a missing well-known path is a normal answer, not "unreachable":
            # the page itself answered, so these probes get their own failure
            # flag (the deadline stays shared)
            f = _head(candidate, con=con, state=dict(state, failed=False))
            if f is not None and f.ok and fetch.looks_like_feed(f):
                return _feed_from_text(url, candidate, f.text, con, state,
                                       host=host, headers=f.headers)
    # 23c a host whose front page never answered (TLS error, timeout, JS wall)
    # can still serve its feed. One probe before giving up, which is also what
    # catches a Substack publication on a custom domain whose root is down.
    if r is None and bare not in ("ft.com", "wsj.com"):
        feed_url = f"{origin}/feed"
        f = _head(feed_url, con=con, state=state)
        if f is not None and f.ok and fetch.looks_like_feed(f):
            return _feed_from_text(url, feed_url, f.text, con, state,
                                   host=host, headers=f.headers)
    # 24 rss-bridge
    hits = []
    left = _left(state)
    try:
        if left > 0:
            hits = bridge.findfeed(url, timeout=max(1, int(min(bridge.TIMEOUT,
                                                               left))))
    except Exception as e:
        print(f"router: bridge findfeed failed for {fetch.host_of(url)} "
              f"({type(e).__name__})")
        hits = []
    if hits:
        hit = hits[0]
        # rss-bridge's human names end in " Bridge" ("Telegram Bridge");
        # the type column wants "Telegram"
        display = _bridge_display((hit.get("name") or hit["bridge"]).replace(" Bridge", ""))
        first = _first_param(hit.get("params") or {})
        return _build(url, "bridge", "bridge", display,
                      name=first or display,
                      feed_url=hit.get("url") or bridge.display_url(
                          hit["bridge"], hit.get("params") or {}),
                      config={"bridge": {"name": hit["bridge"],
                                         "params": hit.get("params") or {}}},
                      con=con)
    # 25 nothing matched
    return _unsupported(url, UNREACHABLE if state["failed"] else NO_FEED)


# ---------------------------------------------------------------- duplicates


def duplicate_of(con, resolution: Resolution) -> str | None:
    """The name of a live source that already covers this resolution: the same
    feed URL, or the same channel, playlist or username (§4 last paragraph).
    Dropped sources do not count."""
    if resolution is None or resolution.one_off or resolution.kind == "unsupported":
        return None
    if resolution.feed_url:
        row = con.execute(
            "select name from source where status <> 'dropped' "
            "and (url = %s or config->>'feed_url' = %s) limit 1",
            (resolution.feed_url, resolution.feed_url)).fetchone()
        if row:
            return row["name"]
    for key in ("channel_id", "playlist_id", "username"):
        value = (resolution.config or {}).get(key)
        if not value:
            continue
        row = con.execute(
            "select name from source where status <> 'dropped' "
            "and config->>%s::text = %s limit 1", (key, str(value))).fetchone()
        if row:
            return row["name"]
    return None
