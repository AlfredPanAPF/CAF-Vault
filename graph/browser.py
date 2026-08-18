"""Browser-side fetch through the CloakBrowser sidecar (build spec v4 §10c).

FT and WSJ answer a Cloudflare / DataDome JavaScript wall to any plain HTTP
client from a server IP, cookies or not; a real browser clears the wall and
the subscriber cookies then decide what comes back. `caf-cloakbrowser` in the
compose stack runs CloakBrowser's `cloakserve` (a CDP multiplexer, headed
under Xvfb) on the internal network; Vault connects over CDP with Playwright,
reuses the sidecar's own context and falls back to throwaway ones, injects the
site's cookies, loads the page, waits for the wall to clear, and hands the
rendered HTML back as a `fetch.Response`.

Owner direction (2026-08-18): premium sources are scraped with the team's paid
accounts through CloakBrowser; nothing here second-guesses that.

One page at a time per process (a lock), hard timeouts everywhere, and any
failure raises `BrowserUnavailable` so `fetch.get` can fall back to the plain
path (which reports the wall honestly).
"""
import os
import re
import threading
import time
from urllib.parse import urlsplit

TIMEOUT_CONNECT_MS = 20_000
TIMEOUT_NAV_MS = 60_000
WALL_WAIT_S = 30           # how long one navigation may sit on a JS wall
NAV_COST_S = 10            # navigation and settle either side of that wait
MAX_ATTEMPTS = 3           # fresh contexts before giving up on a wall
SETTLE_S = 2.0             # after the body appears, let late scripts finish
LOCK_WAIT_S = 5            # how long a second caller waits for the one seat
_LOCK = threading.Lock()

# fingerprint seed + timezone/locale for the sidecar; one stable seed per site
# so the profile looks like the same machine each cycle
SEEDS = {"ft": "vault-ft", "wsj": "vault-wsj"}
TIMEZONE = os.environ.get("CAF_BROWSER_TIMEZONE", "Asia/Singapore")
LOCALE = os.environ.get("CAF_BROWSER_LOCALE", "en-US")

# Only the interstitials, never a script tag a cleared page also carries:
# `js.captcha-delivery.com/tags.js` is DataDome's ordinary tag and rides along
# on a fully entitled WSJ article, while its block page is served from
# `geo.captcha-delivery.com/captcha/` and also carries the ad-blocker line.
WALL_MARKERS = ("cdn-cgi/challenge-platform", "_cf_chl",
                "Please enable JS and disable any ad blocker",
                "geo.captcha-delivery.com")
BODY_MARKERS = {
    "ft": ("articleBody", "article__content-body", "n-content-body",
           'id="article-body"'),
    "wsj": ('data-type="paragraph"', "articleBody", "article-body"),
}
# response headers worth keeping off a page the sidecar rendered: the rest
# (set-cookie above all, plus content-length and content-encoding that describe
# the interstitial rather than the document) do not belong on the Response
KEEP_HEADERS = ("cf-ray", "x-datadome", "cf-mitigated")


class BrowserUnavailable(Exception):
    """The sidecar is not configured, not reachable, or the page did not load."""


def base_url() -> str:
    return (os.environ.get("CAF_BROWSER_URL") or "").rstrip("/")


def enabled() -> bool:
    return bool(base_url())


def attempt_cost_s() -> float:
    """What one attempt can cost: the wall wait plus the navigation and settle
    around it. The retry budget is sized off this rather than a fixed number,
    so a stuck wall still leaves room for the retries §10c promises — the
    fresh context is what clears Cloudflare's hit-or-miss challenge."""
    return WALL_WAIT_S + NAV_COST_S


def budget_s() -> float:
    """The time MAX_ATTEMPTS attempts need; `fetch.get` asks for at least this."""
    return MAX_ATTEMPTS * attempt_cost_s()


def _cdp_url(site: str | None) -> str:
    seed = SEEDS.get(site or "", "vault")
    return f"{base_url()}?fingerprint={seed}&timezone={TIMEZONE}&locale={LOCALE}"


def cdp_url(site: str | None) -> str:
    """The sidecar address for a site, for the other module that dials it
    (graph.signin): same seed, so a sign-in and a fetch look like one machine."""
    return _cdp_url(site)


def playwright_cookies(cookies: list[dict], site_hosts: tuple[str, ...]) -> list[dict]:
    """graph.credentials.parse_cookies() rows -> Playwright add_cookies dicts.
    Rows without a domain get the site's first host, dot-prefixed."""
    out = []
    default = "." + site_hosts[0] if site_hosts else None
    for c in cookies:
        dom = c.get("domain") or default
        if not dom:
            continue
        if not dom.startswith("."):
            dom = "." + dom
        out.append({"name": c["name"], "value": c["value"], "domain": dom,
                    "path": c.get("path") or "/", "secure": True,
                    "httpOnly": False, "sameSite": "Lax"})
    return out


def _wall_gone(html: str, site: str | None = None) -> bool:
    """True when the challenge is no longer what the page is showing. A
    document carrying the site's body markers is past the wall whatever else
    it ships, the same precedence `fetch.detect_wall` gives body over wall."""
    if site and _has_body(html, site):
        return True
    return not any(m in html for m in WALL_MARKERS)


def _has_body(html: str, site: str | None) -> bool:
    return any(m in html for m in BODY_MARKERS.get(site or "", ()))


def get(url: str, *, site: str | None = None, cookies: list[dict] | None = None,
        timeout: int = 60, max_bytes: int | None = None):
    """Load `url` in the sidecar with the given Playwright cookies; return a
    graph.fetch.Response with the rendered HTML. Raises BrowserUnavailable
    when the sidecar cannot be reached or the navigation fails outright. A
    wall that never clears is NOT an error here: the HTML comes back and the
    caller's wall detection reports it."""
    from . import fetch  # local import: fetch imports this module
    if not enabled():
        raise BrowserUnavailable("CAF_BROWSER_URL is not set")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # pragma: no cover - dependency missing
        raise BrowserUnavailable(f"playwright not installed ({e})") from None
    if url.startswith("http://"):
        # every injected cookie is Secure and Chrome withholds those over
        # http, so a hand-pasted http:// link would come back as the barrier
        # for a signed-in account. Both sites are HSTS: this only removes a
        # failure mode.
        url = "https://" + url[7:]

    # the sidecar has one seat: wait briefly for it, then let the caller fall
    # back to the plain path rather than parking a request for the whole budget
    if not _LOCK.acquire(timeout=LOCK_WAIT_S):
        raise BrowserUnavailable("another page is using the sidecar")
    try:
        deadline = time.monotonic() + max(timeout, budget_s())
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(_cdp_url(site),
                                                      timeout=TIMEOUT_CONNECT_MS)
                try:
                    # Cloudflare's managed challenge is hit-or-miss per
                    # navigation (the next one in a fresh context often sails
                    # through), so a stuck wall gets fresh contexts until the
                    # time budget is spent.
                    attempt = 0
                    while True:
                        attempt += 1
                        # cloakserve keeps one Chrome process per fingerprint
                        # seed alive between calls, and its default context
                        # keeps the cf_clearance / datadome cookies a cleared
                        # wall left behind: the first attempt reuses it so the
                        # next article usually skips the challenge; retries
                        # start clean.
                        reuse = attempt == 1 and bool(browser.contexts)
                        status, headers, html, final_url = _load(
                            browser, url, site, cookies, deadline, reuse=reuse)
                        if _wall_gone(html, site) or attempt >= MAX_ATTEMPTS \
                                or time.monotonic() > deadline - attempt_cost_s():
                            break
                        print(f"browser: wall still up for {site or 'the browser'} "
                              f"after attempt {attempt}; retrying with a fresh "
                              "context")
                finally:
                    browser.close()
        except BrowserUnavailable:
            raise
        except Exception as e:
            raise BrowserUnavailable(f"{type(e).__name__}: {str(e)[:160]}") from None
    finally:
        _LOCK.release()
    data = html.encode("utf-8")
    if max_bytes:
        data = data[:max_bytes]
    return fetch.Response(status, final_url, data,
                          {**headers, "content-type": "text/html; charset=utf-8",
                           "x-caf-fetched-by": "browser"})


def _load(browser, url, site, cookies, deadline, reuse=False):
    """One navigation: (status, headers, html, final_url). With reuse, the
    remote browser's default (persistent) context is used and only the page
    is closed afterwards; otherwise a throwaway context."""
    ctx = browser.contexts[0] if reuse else browser.new_context()
    try:
        if cookies:
            try:
                ctx.add_cookies(cookies)
            except Exception as e:
                print(f"browser: cookie import problem for {site}: {type(e).__name__}")
        page = ctx.new_page()
        page.set_default_timeout(TIMEOUT_NAV_MS)
        resp = page.goto(url, wait_until="domcontentloaded",
                         timeout=min(TIMEOUT_NAV_MS,
                                     int(max(deadline - time.monotonic(), 5) * 1000)))
        status = resp.status if resp else 0
        # only the two diagnostics: these are the challenge's headers, not the
        # article's, and set-cookie among them is a credential in a place
        # credentials.scrub() does not reach
        headers = {k.lower(): v for k, v in dict(resp.headers).items()
                   if k.lower() in KEEP_HEADERS} if resp else {}
        html = page.content()
        wall_deadline = min(deadline, time.monotonic() + WALL_WAIT_S)
        while time.monotonic() < wall_deadline:
            if _wall_gone(html, site) and (_has_body(html, site) or not site):
                break
            time.sleep(1.5)
            try:
                html = page.content()
            except Exception:
                # a challenge that clears navigates the page, and a read
                # racing that navigation raises: keep the last snapshot and
                # re-read on the next poll. Never substitute "" — an empty
                # document reads as a cleared wall.
                continue
        if _wall_gone(html, site):
            time.sleep(SETTLE_S)
            try:
                html = page.content()
            except Exception:
                pass
        final_url = page.url
        # the document status after a cleared wall is the page's own;
        # DataDome answers 401 first and 200 on the retry
        if _wall_gone(html, site) and status in (401, 403):
            status = 200
        page.close()
        return status, headers, html, final_url
    finally:
        if not reuse:
            ctx.close()


def health(timeout: int = 5) -> bool:
    """The sidecar's CDP endpoint answers /json/version."""
    if not enabled():
        return False
    try:
        import requests
        r = requests.get(base_url() + "/json/version", timeout=timeout)
        return r.status_code == 200 and "Browser" in r.text
    except Exception:
        return False


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def looks_like_article_url(url: str, site: str | None) -> bool:
    """Only article pages go through the browser; feeds and APIs stay on the
    plain path (they are public and the browser is slow)."""
    path = urlsplit(url).path
    if site == "ft":
        return bool(re.match(r"^/content/[0-9a-f-]{36}", path))
    if site == "wsj":
        return host_of(url).endswith("wsj.com") and not path.endswith((".xml", "/rss"))
    return False
