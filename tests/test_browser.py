"""The CloakBrowser sidecar path for FT and WSJ (build spec v4 §10c, owner
direction 2026-08-18): graph.browser and the hook at the top of
graph.fetch.get.

Nothing here starts a browser or touches the network. `playwright` is a fake
module in sys.modules whose CDP session hands back scripted HTML, the sidecar
address is an environment variable no one dials, DNS is stubbed so `guard_url`
still runs its own checks, and the plain HTTP path is a stub that records what
it was asked for.

    CAF_DB_URL=postgresql:///caf_test_browser uv run pytest tests/test_browser.py -q
"""
import sys
import time
import types

import pytest

from graph import browser, credentials, fetch
from graph.connectors import manual

SIDECAR = "http://caf-cloakbrowser:9222"
FT_ARTICLE = "https://www.ft.com/content/2f9a4b6c-1d3e-4a5b-8c7d-9e0f1a2b3c4d"
WSJ_ARTICLE = "https://www.wsj.com/finance/utilities-chase-the-grid-4f2ab19c"
WSJ_FEED = "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"

# a cookies.txt export and the same two cookies in header form
FT_SECRET = "s%3AJ2ftsessionsupersecret.value"
FT_COOKIES = ("# Netscape HTTP Cookie File\n"
              f".ft.com\tTRUE\t/\tTRUE\t2147483647\tFTSession\t{FT_SECRET}\n"
              "#HttpOnly_.ft.com\tTRUE\t/products\tTRUE\t2147483647\t"
              "FTSession_s\tsessionvalue\n")
FT_HEADER_COOKIES = f"FTSession={FT_SECRET}; FTSession_s=sessionvalue"

# what the sidecar sees while Cloudflare is still deciding, and after
WALL_HTML = ('<html><head><title>Just a moment...</title></head><body>'
             '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1">'
             '</script><p>Please enable JS and disable any ad blocker</p>'
             '</body></html>')
FT_BODY_HTML = ('<html><head><title>Utilities and the grid</title></head><body>'
                '<div class="o-topper__headline"><h1>Utilities and the grid</h1>'
                '</div><div class="article__content-body"><p>Substation queues, '
                'not silicon, now set the delivery date.</p></div></body></html>')
# a cleared challenge that still ends on FT's barrier: the subscription, not
# the bot check, is what is missing
FT_BARRIER_HTML = ('<html><head><title>Subscribe to read</title></head><body>'
                   '<div class="barrier"><p>Subscribe to read the full '
                   'article.</p></div></body></html>')
# the same barrier as FT actually renders it: the topper and a teaser inside
# the body container it uses for real articles
FT_BARRIER_WITH_TEASER = (
    '<html><head><title>Subscribe to read</title></head><body>'
    '<div class="o-topper__headline"><h1>Utilities and the grid</h1></div>'
    '<div class="article__content-body"><p>Substation queues, not silicon, now '
    'set the delivery date.</p></div>'
    '<div class="barrier"><p>Subscribe to read the full article.</p></div>'
    '</body></html>')

# WSJ's live article markup (docs/rss-bridge-brief.md): the body roots at
# `article[style*="article-body"]`, paragraphs are `div[data-type="paragraph"]`
# with no <p> anywhere, and DataDome's ordinary tag script rides along on the
# cleared, fully entitled page
WSJ_PARA = ("Utilities are queueing for substation capacity that will not "
            "arrive before the end of the decade, executives said.")
WSJ_BODY_HTML = (
    '<html><head><title>Utilities chase the grid</title>'
    '<script src="https://js.captcha-delivery.com/tags.js"></script></head>'
    '<body><article style="--article-body: 1; article-body">'
    + "".join(f'<div data-type="paragraph">{WSJ_PARA} ({i})</div>'
              for i in range(4))
    + '</article></body></html>')
# DataDome's interstitial, which is a different host and a different page
WSJ_WALL_HTML = ('<html><head><title>wsj.com</title></head><body>'
                 '<iframe src="https://geo.captcha-delivery.com/captcha/'
                 '?initialCid=abc"></iframe><p>Please enable JS and disable '
                 'any ad blocker</p></body></html>')

_real_sleep = time.sleep


class FakeClock:
    """A virtual clock in browser.time's place: sleeping advances it and
    nothing waits, so a test can exercise the real WALL_WAIT_S / MAX_ATTEMPTS
    budget without spending it."""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------- fakes


class FakePage:
    def __init__(self, ctx):
        self.ctx = ctx
        self.default_timeout = None
        self.goto_calls = []
        self.url = ""
        self.closed = False

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until,
                                "timeout": timeout})
        owner = self.ctx.browser
        self.url = owner.final_url or url
        if owner.goto_error is not None:
            raise owner.goto_error
        return types.SimpleNamespace(status=owner.status,
                                     headers=dict(owner.headers))

    def content(self):
        return self.ctx.browser.next_html()

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, owner, persistent=False):
        self.browser = owner
        self.persistent = persistent
        self.cookies = []
        self.pages = []
        self.closed = False

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True
        # Playwright drops a closed context from browser.contexts
        self.browser.contexts.remove(self)


class FakeBrowser:
    """A CDP browser whose pages return `script` HTML, one entry per content()
    call and the last entry for every call after that. An entry that is an
    exception is raised instead, which is what Playwright does when a read
    races the navigation a cleared challenge performs. `contexts` is the live
    list Playwright exposes; `made` keeps every context, closed or not.
    `persistent=True` models cloakserve's long-lived Chrome, which already has
    its default context open when Vault connects."""

    def __init__(self, script, *, status=403, headers=None, final_url=None,
                 goto_error=None, persistent=False):
        self.script = list(script)
        self.status = status
        self.headers = {"cf-ray": "8ab12c-SIN"} if headers is None else headers
        self.final_url = final_url
        self.goto_error = goto_error
        self.contexts = []
        self.made = []
        self.content_calls = 0
        self.closed = False
        if persistent:
            self._open(FakeContext(self, persistent=True))

    def _open(self, ctx):
        self.contexts.append(ctx)
        self.made.append(ctx)
        return ctx

    def new_context(self):
        return self._open(FakeContext(self))

    def next_html(self):
        html = self.script[min(self.content_calls, len(self.script) - 1)]
        self.content_calls += 1
        if isinstance(html, BaseException):
            raise html
        return html

    def close(self):
        self.closed = True

    @property
    def pages(self):
        return [p for c in self.made for p in c.pages]


def install_playwright(monkeypatch, fake=None, connect_error=None):
    """Put a fake `playwright.sync_api` in sys.modules; return the list of
    connect_over_cdp calls."""
    calls = []

    def connect_over_cdp(url, timeout=None):
        calls.append({"url": url, "timeout": timeout})
        if connect_error is not None:
            raise connect_error
        return fake

    class Session:
        chromium = types.SimpleNamespace(connect_over_cdp=connect_over_cdp)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    module = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = Session
    module.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", module)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return calls


@pytest.fixture
def sidecar(monkeypatch):
    """The sidecar address is set and the waits are short enough to test."""
    monkeypatch.setenv("CAF_BROWSER_URL", SIDECAR)
    monkeypatch.setattr(browser.time, "sleep",
                        lambda s: _real_sleep(min(s, 0.005)))
    monkeypatch.setattr(browser, "SETTLE_S", 0.01)
    return SIDECAR


@pytest.fixture
def public_dns(monkeypatch):
    """Every host resolves to a public address: guard_url keeps running its
    own checks, no test waits on a resolver."""
    monkeypatch.setattr(fetch.socket, "getaddrinfo",
                        lambda host, port, **kw:
                        [(2, 1, 6, "", ("93.184.216.34", port or 443))])


@pytest.fixture
def signed_in(con):
    """FT and WSJ credentials stored: the browser path only runs for a site
    whose sign-in is pasted (without cookies it would fetch metered
    previews). Yields the connection to pass as con=."""
    credentials.set(con, "ft", FT_COOKIES)
    credentials.set(con, "wsj", "DJSESSION=abc; sso=def")
    yield con
    con.rollback()


class FakeHTTP:
    """The little of a streamed requests response that fetch.get reads."""

    def __init__(self, text, status=200, url=""):
        self._data = text.encode("utf-8")
        self.status_code = status
        self.url = url
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.closed = False

    def iter_content(self, size):
        yield self._data

    def close(self):
        self.closed = True


def plain_path(monkeypatch, text=FT_BODY_HTML, status=200):
    """Stub the curl_cffi/requests path; return the calls it received."""
    calls = []

    def fake_get(url, **kw):
        calls.append({"url": url, **kw})
        return FakeHTTP(text, status, url)

    monkeypatch.setattr(fetch, "_cffi", None)
    monkeypatch.setattr(fetch._requests, "get", fake_get)
    return calls


def no_plain_path(monkeypatch):
    """The plain path must not be touched at all."""
    def refuse(url, **kw):
        raise AssertionError(f"plain fetch of {url}")

    monkeypatch.setattr(fetch, "_cffi", None)
    monkeypatch.setattr(fetch._requests, "get", refuse)


def browser_response(html, status=200, url=FT_ARTICLE):
    return fetch.Response(status, url, html.encode("utf-8"),
                          {"content-type": "text/html; charset=utf-8",
                           "x-caf-fetched-by": "browser"})


# ---------------------------------------------------------------- cookies


def test_playwright_cookies_keeps_the_netscape_domain():
    rows = credentials.parse_cookies(FT_COOKIES)
    out = browser.playwright_cookies(rows, credentials.SITE_HOSTS["ft"])

    assert [c["name"] for c in out] == ["FTSession", "FTSession_s"]
    assert [c["domain"] for c in out] == [".ft.com", ".ft.com"]
    assert [c["path"] for c in out] == ["/", "/products"]
    assert out[0]["value"] == FT_SECRET
    assert out[0]["secure"] is True and out[0]["sameSite"] == "Lax"


def test_playwright_cookies_gives_header_form_rows_the_site_host():
    rows = credentials.parse_cookies(FT_HEADER_COOKIES)
    assert [c["domain"] for c in rows] == [None, None]

    out = browser.playwright_cookies(rows, credentials.SITE_HOSTS["ft"])
    assert [(c["name"], c["domain"], c["path"]) for c in out] == [
        ("FTSession", ".ft.com", "/"), ("FTSession_s", ".ft.com", "/")]

    # wsj's first host, not both of them
    wsj = browser.playwright_cookies(credentials.parse_cookies("ccpaUUID=abc"),
                                     credentials.SITE_HOSTS["wsj"])
    assert wsj[0]["domain"] == ".wsj.com"

    # nothing to scope a domainless cookie to: it is dropped, not guessed
    assert browser.playwright_cookies(rows, ()) == []


def test_playwright_cookies_defaults_the_path():
    out = browser.playwright_cookies(
        [{"name": "a", "value": "1", "domain": "ft.com"},
         {"name": "b", "value": "2", "domain": ".ft.com", "path": ""}], ())
    assert [c["path"] for c in out] == ["/", "/"]
    assert [c["domain"] for c in out] == [".ft.com", ".ft.com"]


# ---------------------------------------------------------------- routing


def test_looks_like_article_url():
    assert browser.looks_like_article_url(FT_ARTICLE, "ft") is True
    assert browser.looks_like_article_url(
        "https://www.ft.com/content/2f9a4b6c-1d3e-4a5b-8c7d-9e0f1a2b3c4d"
        "?shareType=nongift", "ft") is True
    # FT's feeds and section pages stay on the plain path
    assert browser.looks_like_article_url(
        "https://www.ft.com/markets?format=rss", "ft") is False
    assert browser.looks_like_article_url("https://www.ft.com/markets",
                                          "ft") is False
    assert browser.looks_like_article_url(
        "https://www.ft.com/rss/home/international", "ft") is False

    assert browser.looks_like_article_url(WSJ_ARTICLE, "wsj") is True
    # the WSJ feed lives on a Dow Jones host, so it is not a wsj.com page
    assert browser.looks_like_article_url(WSJ_FEED, "wsj") is False
    assert browser.looks_like_article_url(
        "https://www.wsj.com/xml/rss/3_7085.xml", "wsj") is False

    # every other site, and no site at all
    assert browser.looks_like_article_url(
        "https://example.substack.com/p/a-post", "substack") is False
    assert browser.looks_like_article_url(FT_ARTICLE, None) is False
    assert browser.looks_like_article_url(FT_ARTICLE, "wsj") is False


# ---------------------------------------------------------------- browser.get


def test_browser_get_without_a_sidecar_address(monkeypatch):
    monkeypatch.delenv("CAF_BROWSER_URL", raising=False)
    assert browser.enabled() is False
    install_playwright(monkeypatch, FakeBrowser([FT_BODY_HTML]))

    with pytest.raises(browser.BrowserUnavailable) as e:
        browser.get(FT_ARTICLE, site="ft")
    assert "CAF_BROWSER_URL" in str(e.value)


def test_browser_get_waits_out_the_wall(monkeypatch, sidecar):
    fake = FakeBrowser([WALL_HTML, WALL_HTML, FT_BODY_HTML],
                       status=403, headers={"cf-ray": "8ab12c-SIN"},
                       final_url=FT_ARTICLE + "?edition=international")
    install_playwright(monkeypatch, fake)
    cookies = browser.playwright_cookies(
        credentials.parse_cookies(FT_COOKIES), credentials.SITE_HOSTS["ft"])

    resp = browser.get(FT_ARTICLE, site="ft", cookies=cookies)

    # the wall is not the page's answer: the cleared document is a 200
    assert resp.status == 200
    assert resp.text == FT_BODY_HTML
    assert resp.url == FT_ARTICLE + "?edition=international"
    assert resp.headers["x-caf-fetched-by"] == "browser"
    assert resp.headers["cf-ray"] == "8ab12c-SIN"
    assert resp.content_type == "text/html; charset=utf-8"

    # one throwaway context, cookies injected before the navigation, and
    # nothing left open behind us
    assert len(fake.made) == 1
    assert [c["name"] for c in fake.made[0].cookies] == ["FTSession",
                                                         "FTSession_s"]
    assert fake.made[0].closed is True
    assert fake.contexts == []
    assert fake.closed is True
    page = fake.pages[0]
    assert page.default_timeout == browser.TIMEOUT_NAV_MS
    assert page.goto_calls[0]["url"] == FT_ARTICLE
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"


def test_browser_get_reuses_the_context_the_sidecar_already_has(monkeypatch,
                                                                sidecar):
    monkeypatch.setattr(browser, "WALL_WAIT_S", 0.1)
    monkeypatch.setattr(browser, "MAX_ATTEMPTS", 2)
    fake = FakeBrowser([FT_BODY_HTML], status=200, persistent=True)
    install_playwright(monkeypatch, fake)
    cookies = browser.playwright_cookies(
        credentials.parse_cookies(FT_HEADER_COOKIES), ("ft.com",))

    resp = browser.get(FT_ARTICLE, site="ft", cookies=cookies, timeout=45)

    # cloakserve's Chrome keeps the cookies a cleared wall left behind, so the
    # first attempt works in the context that is already open and leaves it
    # open; only the page is closed
    assert resp.status == 200
    assert [c.persistent for c in fake.made] == [True]
    assert fake.made[0].closed is False
    assert [c["name"] for c in fake.made[0].cookies] == ["FTSession",
                                                         "FTSession_s"]
    assert fake.pages[0].closed is True

    # a wall the live context cannot clear gets a fresh context on the retry
    walled = FakeBrowser([WALL_HTML], status=403, persistent=True)
    install_playwright(monkeypatch, walled)
    browser.get(FT_ARTICLE, site="ft", timeout=45)
    assert [c.persistent for c in walled.made] == [True, False]
    assert [c.closed for c in walled.made] == [False, True]


def test_browser_get_returns_the_page_when_the_wall_never_clears(monkeypatch,
                                                                 sidecar):
    monkeypatch.setattr(browser, "WALL_WAIT_S", 0.1)
    monkeypatch.setattr(browser, "MAX_ATTEMPTS", 2)
    fake = FakeBrowser([WALL_HTML], status=403)
    install_playwright(monkeypatch, fake)

    # timeout 45 leaves budget for a second attempt (the loop stops one
    # attempt's worth of time short of the deadline)
    resp = browser.get(FT_ARTICLE, site="ft", timeout=45)

    # a stuck wall is not an error here: the caller's wall detection reports it
    assert resp.status == 403
    assert resp.text == WALL_HTML
    assert len(fake.made) == 2              # a fresh context per attempt
    assert all(c.closed for c in fake.made)
    assert fake.closed is True


def test_browser_get_spends_its_whole_budget_on_retries(monkeypatch, sidecar):
    """At production settings — WALL_WAIT_S 30, MAX_ATTEMPTS 3 — a stuck wall
    gets all three attempts. The old budget (deadline minus a flat 20s) let
    the first attempt's own 30s wall wait exhaust it, so only one ever ran."""
    clock = FakeClock()
    monkeypatch.setattr(browser, "time", clock)
    fake = FakeBrowser([WALL_HTML], status=403)
    install_playwright(monkeypatch, fake)

    # 45 is what every caller asks for: rss, links and probes all land there
    resp = browser.get(FT_ARTICLE, site="ft", timeout=45)

    assert resp.status == 403
    assert len(fake.made) == browser.MAX_ATTEMPTS == 3
    # and the budget is the real cost of those attempts, not a magic number
    assert browser.budget_s() == 3 * (browser.WALL_WAIT_S + browser.NAV_COST_S)


def test_browser_get_survives_a_read_across_the_clearing_navigation(monkeypatch,
                                                                    sidecar):
    """page.content() raises while Chrome navigates from the interstitial to
    the real document. That is the success case, not a sidecar failure."""
    monkeypatch.setattr(browser, "WALL_WAIT_S", 0.1)
    boom = RuntimeError("Execution context was destroyed, most likely because "
                        "of a navigation.")
    fake = FakeBrowser([WALL_HTML, boom, FT_BODY_HTML], status=403)
    install_playwright(monkeypatch, fake)

    resp = browser.get(FT_ARTICLE, site="ft", timeout=45)

    assert resp.status == 200                   # not BrowserUnavailable
    assert resp.text == FT_BODY_HTML
    assert len(fake.made) == 1                  # no retry was needed


def test_browser_get_reads_a_cleared_wsj_page_through_datadomes_tag(monkeypatch,
                                                                    sidecar):
    """`js.captcha-delivery.com/tags.js` is on every DataDome-protected page,
    cleared or not, so it must not read as a wall: the body markers decide."""
    assert browser._wall_gone(WSJ_BODY_HTML, "wsj") is True
    assert browser._wall_gone(WSJ_WALL_HTML, "wsj") is False

    monkeypatch.setattr(browser, "WALL_WAIT_S", 0.1)
    # DataDome answers the first hit 401 and serves the article anyway
    fake = FakeBrowser([WSJ_BODY_HTML], status=401)
    install_playwright(monkeypatch, fake)

    resp = browser.get(WSJ_ARTICLE, site="wsj", timeout=45)

    assert resp.status == 200                   # the wall's 401 is not the page's
    assert len(fake.made) == 1                  # nothing retried, nothing waited
    assert fetch.detect_wall("wsj", resp.status, resp.text) is None


def test_browser_get_keeps_only_the_diagnostic_headers(monkeypatch, sidecar):
    monkeypatch.setattr(browser, "WALL_WAIT_S", 0.1)
    fake = FakeBrowser([FT_BODY_HTML], status=200, headers={
        "cf-ray": "8ab12c-SIN", "Set-Cookie": "cf_clearance=" + FT_SECRET,
        "content-length": "17", "content-encoding": "br"})
    install_playwright(monkeypatch, fake)

    resp = browser.get(FT_ARTICLE, site="ft", timeout=45)

    # the challenge's headers describe the challenge, not the document, and a
    # set-cookie has no business riding on a Response
    assert resp.headers["cf-ray"] == "8ab12c-SIN"
    assert "set-cookie" not in resp.headers
    assert "content-length" not in resp.headers
    assert "content-encoding" not in resp.headers
    assert resp.content_type == "text/html; charset=utf-8"


def test_browser_get_upgrades_http_and_caps_the_document(monkeypatch, sidecar):
    monkeypatch.setattr(browser, "WALL_WAIT_S", 0.1)
    fake = FakeBrowser([FT_BODY_HTML], status=200)
    install_playwright(monkeypatch, fake)

    # every cookie is injected Secure, so a hand-pasted http:// link would
    # otherwise navigate with an empty jar and come back as the barrier
    resp = browser.get("http://www.ft.com/content/"
                       "2f9a4b6c-1d3e-4a5b-8c7d-9e0f1a2b3c4d", site="ft",
                       timeout=45, max_bytes=40)
    assert fake.pages[0].goto_calls[0]["url"].startswith("https://")
    assert len(resp.bytes) == 40


def test_browser_get_gives_up_when_the_seat_is_taken(monkeypatch, sidecar):
    monkeypatch.setattr(browser, "LOCK_WAIT_S", 0.01)
    install_playwright(monkeypatch, FakeBrowser([FT_BODY_HTML], status=200))

    browser._LOCK.acquire()
    try:
        with pytest.raises(browser.BrowserUnavailable) as e:
            browser.get(FT_ARTICLE, site="ft", timeout=45)
    finally:
        browser._LOCK.release()
    assert "another page" in str(e.value)


def test_browser_get_when_the_sidecar_refuses_the_connection(monkeypatch,
                                                             sidecar):
    install_playwright(monkeypatch,
                       connect_error=RuntimeError("connect ECONNREFUSED"))

    with pytest.raises(browser.BrowserUnavailable) as e:
        browser.get(FT_ARTICLE, site="ft")
    assert "RuntimeError" in str(e.value)
    assert "ECONNREFUSED" in str(e.value)


def test_browser_get_when_the_navigation_fails(monkeypatch, sidecar):
    fake = FakeBrowser([FT_BODY_HTML], goto_error=TimeoutError("nav timeout"))
    install_playwright(monkeypatch, fake)

    with pytest.raises(browser.BrowserUnavailable):
        browser.get(FT_ARTICLE, site="ft")
    assert fake.closed is True              # the CDP session is not left open


def test_browser_get_cdp_url_carries_the_fingerprint(monkeypatch):
    monkeypatch.setenv("CAF_BROWSER_URL", SIDECAR + "/")
    monkeypatch.setattr(browser.time, "sleep", lambda s: None)
    monkeypatch.setattr(browser, "WALL_WAIT_S", 0)
    monkeypatch.setattr(browser, "SETTLE_S", 0)
    calls = install_playwright(monkeypatch, FakeBrowser([FT_BODY_HTML],
                                                        status=200))

    browser.get(FT_ARTICLE, site="ft")
    url = calls[0]["url"]
    assert url.startswith(SIDECAR + "?")    # the trailing slash is trimmed
    assert "fingerprint=vault-ft" in url
    assert f"timezone={browser.TIMEZONE}" in url
    assert f"locale={browser.LOCALE}" in url
    assert calls[0]["timeout"] == browser.TIMEOUT_CONNECT_MS

    # one stable seed per site, and a neutral one when there is no site
    browser.get(WSJ_ARTICLE, site="wsj")
    assert "fingerprint=vault-wsj" in calls[1]["url"]
    browser.get("https://example.com/story")
    assert "fingerprint=vault&" in calls[2]["url"]


# ---------------------------------------------------------------- fetch hook


def test_fetch_get_sends_ft_articles_through_the_browser(monkeypatch, sidecar,
                                                         public_dns, signed_in):
    calls = []

    def fake_browser_get(url, **kw):
        calls.append({"url": url, **kw})
        return browser_response(FT_BODY_HTML)

    monkeypatch.setattr(browser, "get", fake_browser_get)
    no_plain_path(monkeypatch)              # the curl path is never reached

    resp = fetch.get(FT_ARTICLE, site="ft", con=signed_in)

    assert resp.text == FT_BODY_HTML
    assert resp.headers["x-caf-fetched-by"] == "browser"
    assert len(calls) == 1
    assert calls[0]["url"] == FT_ARTICLE
    assert calls[0]["site"] == "ft"
    assert calls[0]["timeout"] >= 45        # the sidecar needs room to wait


def test_fetch_get_keeps_feeds_and_section_pages_on_the_plain_path(
        monkeypatch, sidecar, public_dns):
    def refuse(url, **kw):
        raise AssertionError(f"browser used for {url}")

    monkeypatch.setattr(browser, "get", refuse)
    calls = plain_path(monkeypatch)

    # a feed fetch (allow_wall) of an article URL stays on the plain path
    fetch.get(FT_ARTICLE, site="ft", allow_wall=True)
    # so does a section page, which is not an article
    fetch.get("https://www.ft.com/markets", site="ft")
    assert [c["url"] for c in calls] == [FT_ARTICLE, "https://www.ft.com/markets"]


def test_fetch_get_falls_back_when_the_sidecar_is_unavailable(monkeypatch,
                                                              sidecar,
                                                              public_dns):
    def down(url, **kw):
        raise browser.BrowserUnavailable("CAF_BROWSER_URL is not set")

    monkeypatch.setattr(browser, "get", down)
    calls = plain_path(monkeypatch)

    resp = fetch.get(FT_ARTICLE, site="ft")
    assert resp.text == FT_BODY_HTML
    assert [c["url"] for c in calls] == [FT_ARTICLE]
    assert "x-caf-fetched-by" not in resp.headers


def test_fetch_get_reports_a_walled_browser_page(monkeypatch, sidecar,
                                                 public_dns, signed_in):
    no_plain_path(monkeypatch)

    # the challenge cleared but FT answered with its barrier
    monkeypatch.setattr(browser, "get",
                        lambda url, **kw: browser_response(FT_BARRIER_HTML))
    with pytest.raises(fetch.SignInNeeded) as e:
        fetch.get(FT_ARTICLE, site="ft", con=signed_in)
    assert e.value.site == "ft"
    assert e.value.detail == "sign-in page returned"

    # the challenge never cleared: the sidecar hands back the 403 it saw
    monkeypatch.setattr(browser, "get",
                        lambda url, **kw: browser_response(WALL_HTML, status=403))
    with pytest.raises(fetch.SignInNeeded) as e:
        fetch.get(FT_ARTICLE, site="ft", con=signed_in)
    assert e.value.detail == "HTTP 403"


def test_fetch_get_hands_the_browser_the_stored_cookies(con, monkeypatch,
                                                        sidecar, public_dns):
    credentials.set(con, "ft", FT_COOKIES)
    calls = []

    def fake_browser_get(url, **kw):
        calls.append(kw)
        return browser_response(FT_BODY_HTML)

    monkeypatch.setattr(browser, "get", fake_browser_get)
    no_plain_path(monkeypatch)

    fetch.get(FT_ARTICLE, site="ft", con=con)
    cookies = calls[0]["cookies"]
    assert [c["name"] for c in cookies] == ["FTSession", "FTSession_s"]
    assert cookies[0]["domain"] == ".ft.com"

    # no credential stored: the browser is skipped (it would only fetch a
    # metered preview) and the plain path runs instead
    credentials.delete(con, "ft")
    plain = []
    monkeypatch.setattr(fetch, "_cffi", None)
    monkeypatch.setattr(fetch._requests, "get",
                        lambda url, **kw: plain.append(url) or FakeHTTP(FT_BODY_HTML))
    fetch.get(FT_ARTICLE, site="ft", con=con)
    assert len(calls) == 1 and plain == [FT_ARTICLE]


def test_fetch_get_asks_for_the_browsers_whole_retry_budget(monkeypatch,
                                                            sidecar, public_dns, signed_in):
    calls = []

    def fake_browser_get(url, **kw):
        calls.append(kw)
        return browser_response(FT_BODY_HTML)

    monkeypatch.setattr(browser, "get", fake_browser_get)
    no_plain_path(monkeypatch)

    fetch.get(FT_ARTICLE, site="ft", timeout=30, con=signed_in)
    # a 30s caller must not clip the retries off the sidecar's budget
    assert calls[0]["timeout"] >= browser.budget_s()
    assert calls[0]["max_bytes"] == fetch.MAX_BYTES   # the plain path's cap


def test_wsj_article_survives_the_browser_path(monkeypatch, sidecar, public_dns, signed_in):
    """The rendered WSJ document all the way through: DataDome's tag on the
    page, a 401 on the first hit, and a body written as paragraph divs."""
    monkeypatch.setattr(browser, "get",
                        lambda url, **kw: browser_response(WSJ_BODY_HTML,
                                                           url=WSJ_ARTICLE))
    no_plain_path(monkeypatch)

    resp = fetch.get(WSJ_ARTICLE, site="wsj", con=signed_in)       # no SignInNeeded
    _title, _published, body = manual.extract_article(resp.text, WSJ_ARTICLE)

    assert WSJ_PARA in body
    assert len(body) >= 400                         # probes.MIN_BODY


# a WSJ /news/ listing as the sidecar renders it: no article body anywhere,
# the pieces in the __NEXT_DATA__ JSON, DataDome's ordinary tag riding along
WSJ_LISTING = "https://www.wsj.com/news/heard-on-the-street"
WSJ_LISTING_HTML = (
    '<html><head><title>Heard on the Street</title>'
    '<script src="https://js.captcha-delivery.com/tags.js"></script></head>'
    '<body><div id="root">Heard on the Street</div>'
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"latestArticles":[{"articleUrl":'
    '"https://www.wsj.com/finance/auto-insurance-93243754","headline":'
    '"Auto insurance premiums have room to keep falling","summary":'
    '"The soft market pleases inflation watchers.","timestamp":'
    '"2026-08-18T09:30:00Z"}]}}}</script></body></html>')


def test_browser_get_listing_markers_stand_in_for_the_body(monkeypatch, sidecar):
    """A WSJ /news/ listing carries none of the article body markers, so
    without its own markers every poll would sit out the full wall wait and
    the wall check would read the page as a barrier (§4 rule 13, listing
    half)."""
    from graph.connectors import rss

    assert browser._has_body(WSJ_LISTING_HTML, "wsj") is False
    assert browser._has_body(WSJ_LISTING_HTML, "wsj",
                             rss.WSJ_LISTING_MARKERS) is True
    # DataDome's ordinary tag says "captcha" in the head, so without the
    # markers the wall check calls the whole listing a sign-in page
    assert fetch.detect_wall("wsj", 200, WSJ_LISTING_HTML) == "sign-in page returned"
    assert fetch.detect_wall("wsj", 200, WSJ_LISTING_HTML,
                             rss.WSJ_LISTING_MARKERS) is None

    clock = FakeClock()
    monkeypatch.setattr(browser, "time", clock)
    fake = FakeBrowser([WSJ_LISTING_HTML], status=401)
    install_playwright(monkeypatch, fake)

    resp = browser.get(WSJ_LISTING, site="wsj", timeout=45,
                       body_markers=rss.WSJ_LISTING_MARKERS)

    assert resp.status == 200               # DataDome's 401 is not the page's
    assert resp.text == WSJ_LISTING_HTML
    assert len(fake.made) == 1              # no retry
    # and no wall wait: the listing markers ended it on the first read
    assert clock.now - 1000.0 < browser.WALL_WAIT_S


def test_fetch_get_reports_an_ft_barrier_that_carries_a_teaser(monkeypatch,
                                                               sidecar,
                                                               public_dns, signed_in):
    """FT's barrier renders the topper and a teaser inside the same container
    a real article uses, so the body markers alone cannot clear it."""
    monkeypatch.setattr(browser, "get",
                        lambda url, **kw: browser_response(FT_BARRIER_WITH_TEASER))
    no_plain_path(monkeypatch)

    with pytest.raises(fetch.SignInNeeded) as e:
        fetch.get(FT_ARTICLE, site="ft", con=signed_in)
    assert e.value.detail == "sign-in page returned"


def test_fetch_get_never_uses_the_browser_for_other_sites(con, monkeypatch,
                                                          sidecar, public_dns):
    def refuse(url, **kw):
        raise AssertionError(f"browser used for {url}")

    monkeypatch.setattr(browser, "get", refuse)
    calls = plain_path(monkeypatch, text="<html><body><p>A post.</p></body></html>")

    fetch.get("https://example.substack.com/p/a-post", site="substack", con=con)
    fetch.get("https://news.example.com/story")
    assert len(calls) == 2
