"""HTTP fetch with per-site credentials and paywall detection (build spec v4
§3.2).

`get()` impersonates a Chrome TLS/HTTP fingerprint via curl_cffi when it is
importable (it arrives with yfinance) and falls back to `requests` with the
browser UA otherwise. When a `site` is given and a credential exists, the
site's cookies ride along, and known sign-in walls raise `SignInNeeded` so
callers can surface "Sign-in needed" instead of ingesting a barrier page.
Cookie values are never logged.

Every fetch runs through `guard_url` first. The sources page hands this module
whatever address a user pasted, so without a guard `POST /api/sources/resolve`
would fetch the compose network on request: the cloud metadata address, the
Postgres port, the rss-bridge container. Loopback, private, link-local and
reserved addresses are refused, single-label hosts (docker service names) with
them, and the check runs again on the final URL when a redirect moved hosts.
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit

from . import browser, credentials

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_BYTES = 2_000_000
HEAD_BYTES = 512_000

try:  # optional, preferred: Chrome fingerprint
    from curl_cffi import requests as _cffi
except Exception:  # pragma: no cover - environment dependent
    _cffi = None
import requests as _requests


class SignInNeeded(Exception):
    """A premium site answered with its sign-in / bot wall."""

    def __init__(self, site: str, detail: str = ""):
        self.site = site
        self.detail = detail
        super().__init__(f"Sign-in needed for {site}" + (f": {detail}" if detail else ""))


class BlockedAddress(Exception):
    """The URL points at this machine or the private network around it."""


class Response:
    __slots__ = ("status", "url", "bytes", "headers", "_text")

    def __init__(self, status: int, url: str, data: bytes, headers: dict):
        self.status = status
        self.url = url
        self.bytes = data
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self._text = None

    @property
    def content_type(self) -> str:
        return (self.headers.get("content-type") or "").lower()

    @property
    def text(self) -> str:
        if self._text is None:
            enc = None
            m = re.search(r"charset=([\w-]+)", self.content_type)
            if m:
                enc = m.group(1)
            if not enc:
                head = self.bytes[:4096]
                m = re.search(rb'encoding=["\']([\w-]+)["\']', head) or \
                    re.search(rb'charset=["\']?([\w-]+)', head)
                if m:
                    enc = m.group(1).decode("ascii", "ignore")
            try:
                self._text = self.bytes.decode(enc or "utf-8", errors="replace")
            except LookupError:
                self._text = self.bytes.decode("utf-8", errors="replace")
        return self._text

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# ---------------------------------------------------------------- walls

# (status codes that mean a wall, body markers that mean an article body is
#  present, body markers that mean the wall page)
_WALLS = {
    "ft": {
        "codes": {403},
        "body": ("articleBody", "article__content-body", "n-content-body",
                 "article-body"),
        "wall": ("barrier", "Subscribe to read", "Sign In", "sign-in",
                 "subscribe"),
    },
    "wsj": {
        "codes": {401, 403},
        "body": ("articleBody", "article-body", "ArticleBody",
                 "data-type=\"article-body\""),
        "wall": ("captcha", "datadome", "px-captcha", "Access Denied",
                 "Please sign in", "Subscribe Now"),
    },
}

# Markers a full article never carries, checked before the body markers below.
# The body list matches bare substrings ("article-body"), so an FT barrier that
# renders its teaser inside `.article__content-body` would otherwise escape as
# a genuine article and ingest as one.
_HARD_WALL = {
    "ft": ("<title>Subscribe to read",),
    "wsj": ("geo.captcha-delivery.com", "Access Denied"),
}


def detect_wall(site: str | None, status: int, text: str) -> str | None:
    """Return a short reason when the response is a sign-in / bot wall for
    the site, else None. Only ft and wsj have wall heuristics here; substack,
    x and youtube are judged by their own clients."""
    spec = _WALLS.get(site or "")
    if not spec:
        return None
    if status in spec["codes"]:
        return f"HTTP {status}"
    head = text[:200_000]
    if any(m in head for m in _HARD_WALL.get(site or "", ())):
        return "sign-in page returned"
    if any(m in head for m in spec["body"]):
        return None
    if any(m.lower() in head.lower() for m in spec["wall"]):
        return "sign-in page returned"
    return None


# ---------------------------------------------------------------- addresses


# The ranges a pasted URL must never reach from this box: loopback, link-local
# (the cloud metadata address lives there), RFC 1918 and CGNAT, IPv6 ULA and
# site-local, unspecified and multicast. Deliberately NOT ipaddress.is_private:
# that also flags 198.18.0.0/15 (benchmarking) and the documentation ranges,
# which are not reachable targets, and fake-IP DNS setups (VPN resolvers) hand
# back 198.18.x.x for every public host, which would refuse the whole internet.
_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8",
)]


def _private_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.strip("[]"))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _BLOCKED_NETS)


def host_of(url: str) -> str:
    """The hostname without a trailing dot ('postgres.' and 'postgres' are the
    same name to DNS, so they are the same name here)."""
    try:
        return (urlsplit(url).hostname or "").strip().rstrip(".").lower()
    except ValueError:
        return ""


def blocked_host(host: str) -> bool:
    """True for a host nothing on the public internet can be behind, judged
    without a lookup: an address literal in a private range, or a single-label
    name (a container or LAN name, never a public site)."""
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return True
    if _private_ip(host):
        return True
    if ":" in host:            # a public IPv6 literal has no dot to test
        return False
    return "." not in host


def guard_url(url: str) -> str:
    """Raise BlockedAddress unless this is an http(s) URL on a public host.
    An address that does not resolve is left alone: the fetch itself will fail,
    and refusing it here would turn a DNS hiccup into a wrong answer."""
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        port = parts.port
    except ValueError:
        raise BlockedAddress("that address cannot be read") from None
    if scheme not in ("http", "https"):
        raise BlockedAddress(f"{scheme or 'that'} addresses are not fetched")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if blocked_host(host):
        raise BlockedAddress("that address is not on the public internet")
    try:
        infos = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return host
    for info in infos:
        if _private_ip(str(info[4][0])):
            raise BlockedAddress("that address is not on the public internet")
    return host


# ---------------------------------------------------------------- fetch


BROWSER_SITES = ("ft", "wsj")


def _browser_cookies(con, site: str) -> list[dict]:
    """The site's stored cookies as Playwright cookie dicts (all of them: the
    browser scopes by domain itself)."""
    if con is None:
        return []
    value = credentials.value(con, site)
    if not value:
        return []
    try:
        rows = credentials.parse_cookies(value)
    except credentials.InvalidCredential:
        return []
    return browser.playwright_cookies(rows, credentials.SITE_HOSTS.get(site, ()))


def _cookies_for(con, site: str | None, url: str) -> dict[str, str]:
    if not site or con is None:
        return {}
    value = credentials.value(con, site)
    if not value:
        return {}
    host = urlsplit(url).hostname or ""
    try:
        if site == "substack":
            # substack.sid is issued for substack.com but every publication
            # (custom domains included) is one backend, and it honours the
            # cookie on the publication host. Send substack.com's cookies to
            # whatever Substack host we fetch, plus the two flags the
            # rss-bridge SubstackBridge sends alongside (docs/rss-bridge-brief).
            jar = credentials.cookie_jar(value, host="substack.com", site=site)
            if "substack.sid" in jar:
                jar.setdefault("substack.lli", "1")
                jar.setdefault("ab_experiment_sampled", "%22false%22")
            return jar
        return credentials.cookie_jar(value, host=host, site=site)
    except credentials.InvalidCredential:
        return {}


def _read_capped(resp, max_bytes: int) -> bytes:
    data = b""
    for chunk in resp.iter_content(65536):
        if not chunk:
            continue
        data += chunk
        if len(data) >= max_bytes:
            break
    return data[:max_bytes]


def get(url: str, *, site: str | None = None, con=None, timeout: int = 30,
        max_bytes: int = MAX_BYTES, headers: dict | None = None,
        cookies: dict | None = None, allow_wall: bool = False,
        allow_redirects: bool = True) -> Response:
    """GET with a browser fingerprint, the site's cookies, and a byte cap.
    Raises SignInNeeded on a recognised wall unless allow_wall, and
    BlockedAddress when the URL points inside the private network. Transport
    errors propagate as the underlying library's exceptions."""
    host = guard_url(url)
    # FT and WSJ article pages: a real browser first (build spec v4 §10c). The
    # plain path below never gets past their JS walls from a server IP; the
    # sidecar does, and the pasted subscriber cookies then decide the body.
    # Feeds and APIs (allow_wall=True) stay on the plain path.
    if site in BROWSER_SITES and not allow_wall and browser.enabled() \
            and browser.looks_like_article_url(url, site):
        try:
            resp = browser.get(url, site=site, cookies=_browser_cookies(con, site),
                               timeout=max(timeout, int(browser.budget_s())),
                               max_bytes=max_bytes)
        except browser.BrowserUnavailable as e:
            print(f"browser: sidecar unavailable for {site} ({e}); plain fetch")
        else:
            # the browser follows redirects itself, so the same guard the plain
            # path runs on its final URL runs on the rendered page's
            if host_of(resp.url) not in ("", host):
                guard_url(resp.url)
            reason = detect_wall(site, resp.status, resp.text)
            if reason:
                raise SignInNeeded(site, reason)
            return resp
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    jar = dict(cookies or {})
    if not jar:
        jar = _cookies_for(con, site, url)
    if _cffi is not None:
        r = _cffi.get(url, headers=hdrs, cookies=jar or None, timeout=timeout,
                      impersonate="chrome", allow_redirects=allow_redirects,
                      stream=True)
        try:
            data = _read_capped(r, max_bytes)
            resp = Response(r.status_code, str(r.url), data, dict(r.headers))
        finally:
            r.close()
    else:
        r = _requests.get(url, headers=hdrs, cookies=jar or None,
                          timeout=timeout, allow_redirects=allow_redirects,
                          stream=True)
        try:
            data = _read_capped(r, max_bytes)
            resp = Response(r.status_code, r.url, data, dict(r.headers))
        finally:
            r.close()
    # redirects are followed by the library, so the guard runs again whenever
    # the chain ended on another host: a public URL can bounce to a private one
    if host_of(resp.url) not in ("", host):
        guard_url(resp.url)
    if site and not allow_wall:
        reason = detect_wall(site, resp.status, resp.text)
        if reason:
            raise SignInNeeded(site, reason)
    return resp


def head(url: str, *, con=None, site: str | None = None, timeout: int = 8,
         **kw) -> Response:
    """Small capped GET for sniffing (feeds, autodiscovery, article-ness)."""
    return get(url, site=site, con=con, timeout=timeout, max_bytes=HEAD_BYTES,
               allow_wall=True, **kw)


def looks_like_feed(resp: Response) -> bool:
    ct = resp.content_type
    if any(t in ct for t in ("rss+xml", "atom+xml", "application/xml", "text/xml")):
        head_text = resp.text[:2000].lstrip()
        return head_text.startswith("<") and ("<rss" in head_text or "<feed" in head_text
                                              or "<?xml" in head_text)
    head_text = resp.text[:2000].lstrip()
    if head_text.startswith("<?xml"):
        head_text = head_text[head_text.find("?>") + 2:].lstrip()
    return head_text.startswith("<rss") or head_text.startswith("<feed") \
        or head_text.startswith("<rdf:RDF")
