"""Signing into FT and WSJ from the sources page (build spec v4 §7, §10c).

Nothing here starts a browser or touches the network: `graph.signin`'s
`async_playwright` is replaced with a fake whose awaited methods record what
they were asked for and hand back scripted frames and cookies. TestClient
drives the async endpoints. Every response body is checked against the session
cookie value: a credential must never leave the API.

    CAF_DB_URL=postgresql:///caf_test_signin uv run pytest tests/test_signin.py -q
"""
import asyncio
import base64
import types

import pytest
from fastapi.testclient import TestClient

from graph import credentials, signin, webapp

SIDECAR = "http://caf-cloakbrowser:9222"
FT_LOGIN = "https://accounts.ft.com/login"
WSJ_LOGIN = "https://www.wsj.com/client/login"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF frame bytes\xff\xd9"

# what the FT context holds once the form is done: the session cookie, a
# second cookie that expires with the browser, and one from another site
FT_SECRET = "s%3AJ2ftsessionsupersecret.value"
FT_JAR = [
    {"name": "FTSession_s", "value": FT_SECRET, "domain": ".ft.com",
     "path": "/", "expires": 1790000000.0, "secure": True, "httpOnly": True},
    {"name": "FTSession", "value": "legacy-session", "domain": "accounts.ft.com",
     "path": "/login", "expires": -1, "secure": True, "httpOnly": False},
    {"name": "consent", "value": "yes", "domain": ".google.com", "path": "/",
     "expires": 0, "secure": False, "httpOnly": False},
]
# the same context before the sign-in finishes: cookies, but not that one
FT_JAR_ANONYMOUS = [
    {"name": "FTCookieConsentGDPR", "value": "true", "domain": ".ft.com",
     "path": "/", "expires": 1790000000.0, "secure": True, "httpOnly": False},
]


# ---------------------------------------------------------------- fakes


class FakeMouse:
    def __init__(self, page):
        self.page = page

    async def click(self, x, y, click_count=1):
        self.page.fail_now()
        self.page.actions.append(("click", x, y, click_count))

    async def wheel(self, dx, dy):
        self.page.fail_now()
        self.page.actions.append(("wheel", dx, dy))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, text):
        self.page.fail_now()
        self.page.actions.append(("type", text))

    async def press(self, key):
        self.page.fail_now()
        self.page.actions.append(("press", key))


class FakePage:
    """One page in the remote browser. `actions` is everything it was asked to
    do; `input_error` makes the next input raise the way a page that navigated
    out from under the caller does."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.actions = []
        self.url = "about:blank"
        self.viewport = None
        self.closed = False
        self.image = JPEG
        self.title_text = "Sign in"
        self.goto_error = None
        self.screenshot_error = None
        self.input_error = None

    def fail_now(self):
        if self.input_error is not None:
            raise self.input_error

    @property
    def mouse(self):
        return FakeMouse(self)

    @property
    def keyboard(self):
        return FakeKeyboard(self)

    async def set_viewport_size(self, size):
        self.viewport = dict(size)

    async def goto(self, url, wait_until=None, timeout=None):
        self.actions.append(("goto", url, wait_until, timeout))
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url

    async def screenshot(self, type=None, quality=None):
        if self.screenshot_error is not None:
            raise self.screenshot_error
        self.actions.append(("screenshot", type, quality))
        return self.image

    async def title(self):
        return self.title_text

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, owner, persistent=False):
        self.owner = owner
        self.persistent = persistent
        self.pages = []
        self.jar = []
        self.closed = False

    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        self.owner.pages.append(page)
        return page

    async def cookies(self):
        if self.owner.cookies_error is not None:
            raise self.owner.cookies_error
        return [dict(c) for c in self.jar]

    async def close(self):
        self.closed = True


class FakeBrowser:
    """cloakserve's Chrome as Playwright sees it over CDP: its default context
    is already open when Vault connects."""

    def __init__(self, persistent=True, cookies_error=None):
        self.contexts = []
        self.pages = []
        self.closed = False
        self.cookies_error = cookies_error
        if persistent:
            self.contexts.append(FakeContext(self, persistent=True))

    async def new_context(self):
        ctx = FakeContext(self)
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, remote, connect_error=None):
        self.remote = remote
        self.connect_error = connect_error
        self.calls = []
        self.stopped = False
        self.chromium = types.SimpleNamespace(connect_over_cdp=self._connect)

    async def _connect(self, url, timeout=None):
        self.calls.append({"url": url, "timeout": timeout})
        if self.connect_error is not None:
            raise self.connect_error
        return self.remote

    async def stop(self):
        self.stopped = True


class FakeStarter:
    def __init__(self, pw):
        self.pw = pw

    async def start(self):
        return self.pw


def install(monkeypatch, remote=None, connect_error=None):
    """Put a fake async_playwright in graph.signin; return (pw, remote)."""
    remote = FakeBrowser() if remote is None else remote
    pw = FakePlaywright(remote, connect_error=connect_error)
    monkeypatch.setattr(signin, "async_playwright", lambda: FakeStarter(pw))
    return pw, remote


@pytest.fixture(autouse=True)
def no_sessions():
    """Sessions are module state in the web process; no test inherits one."""
    signin._SESSIONS.clear()
    yield
    signin._SESSIONS.clear()


@pytest.fixture
def sidecar(monkeypatch):
    monkeypatch.setenv("CAF_BROWSER_URL", SIDECAR)
    return SIDECAR


@pytest.fixture
def client():
    return TestClient(webapp.app)


def open_ft(client, monkeypatch, jar=None):
    """A started FT session: (body, pw, remote, page)."""
    pw, remote = install(monkeypatch)
    remote.contexts[0].jar = list(jar or [])
    body = client.post("/api/signin/ft").json()
    return body, pw, remote, remote.pages[0]


# ---------------------------------------------------------------- start


def test_signin_start_opens_the_login_page(con, monkeypatch, sidecar, client):
    pw, remote = install(monkeypatch)

    r = client.post("/api/signin/ft")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "site", "url", "title", "width", "height",
                         "resumed"}
    assert body["site"] == "ft"
    assert body["url"] == FT_LOGIN
    assert body["title"] == "Sign in"
    assert (body["width"], body["height"]) == (1280, 800)
    assert body["resumed"] is False

    # the sidecar is dialled with the site's own fingerprint seed, and the
    # context cloakserve already has open is the one signed into
    assert pw.calls[0]["url"] == SIDECAR + "?fingerprint=vault-ft" \
        "&timezone=Asia/Singapore&locale=en-US"
    assert pw.calls[0]["timeout"] == signin.CONNECT_TIMEOUT_MS
    assert len(remote.contexts) == 1 and remote.contexts[0].persistent is True
    page = remote.pages[0]
    assert page.viewport == {"width": 1280, "height": 800}
    assert page.actions[0] == ("goto", FT_LOGIN, "domcontentloaded",
                               signin.NAV_TIMEOUT_MS)

    # a second click resumes the session instead of opening a second page
    again = client.post("/api/signin/ft").json()
    assert again["id"] == body["id"]
    assert again["resumed"] is True
    assert len(remote.pages) == 1 and len(pw.calls) == 1

    # WSJ is its own session, on its own login page
    wsj_pw, wsj_remote = install(monkeypatch)
    wsj = client.post("/api/signin/wsj").json()
    assert wsj["site"] == "wsj" and wsj["url"] == WSJ_LOGIN
    assert wsj["id"] != body["id"]
    assert "fingerprint=vault-wsj" in wsj_pw.calls[0]["url"]
    assert wsj_remote.pages[0].actions[0][1] == WSJ_LOGIN


def test_signin_start_only_for_ft_and_wsj(con, monkeypatch, sidecar, client):
    pw, _remote = install(monkeypatch)

    for site in ("bloomberg", "substack", "x", "youtube"):
        r = client.post(f"/api/signin/{site}")
        assert r.status_code == 400
        assert r.json()["detail"] == "No sign-in for that site."
    assert pw.calls == []                   # nothing was dialled


def test_signin_start_without_the_sidecar(con, monkeypatch, client):
    monkeypatch.delenv("CAF_BROWSER_URL", raising=False)
    pw, _remote = install(monkeypatch)

    r = client.post("/api/signin/ft")
    assert r.status_code == 503
    assert r.json()["detail"] == "The browser is not running."
    assert pw.calls == []
    assert signin._SESSIONS == {}


def test_signin_start_when_the_sidecar_refuses(con, monkeypatch, sidecar,
                                               client):
    pw, _remote = install(monkeypatch,
                          connect_error=RuntimeError("connect ECONNREFUSED"))

    r = client.post("/api/signin/ft")
    assert r.status_code == 503
    assert r.json()["detail"] == "The browser is not running."
    assert signin._SESSIONS == {}
    assert pw.stopped is True               # nothing left running behind us


def test_signin_start_leaves_no_tab_when_the_setup_fails(con, monkeypatch,
                                                         sidecar, client):
    """The page is opened in cloakserve's own context, which outlives the
    disconnect: a start that fails half way must take its tab with it."""
    pw, remote = install(monkeypatch)
    ctx = remote.contexts[0]
    made = []

    async def new_page():
        page = FakePage(ctx)

        async def viewport_fails(size):
            raise RuntimeError("Target crashed")

        page.set_viewport_size = viewport_fails
        made.append(page)
        ctx.pages.append(page)
        remote.pages.append(page)
        return page

    ctx.new_page = new_page
    r = client.post("/api/signin/ft")
    assert r.status_code == 503
    assert r.json()["detail"] == "The browser is not running."
    assert signin._SESSIONS == {}
    assert made[0].closed is True             # no tab left in the shared browser
    assert pw.stopped is True


def test_signin_start_survives_a_slow_login_page(con, monkeypatch, sidecar,
                                                 client):
    """A navigation that times out still leaves a session: the frame shows
    whatever loaded and the analyst can reload from there."""
    _pw, remote = install(monkeypatch)
    ctx = remote.contexts[0]

    async def new_page():
        page = FakePage(ctx)
        page.goto_error = TimeoutError("navigation timeout")
        ctx.pages.append(page)
        remote.pages.append(page)
        return page

    ctx.new_page = new_page
    r = client.post("/api/signin/ft")
    assert r.status_code == 200
    assert r.json()["url"] == "about:blank"
    assert len(signin._SESSIONS) == 1


# ---------------------------------------------------------------- frame


def test_signin_frame_is_a_base64_jpeg(con, monkeypatch, sidecar, client):
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    page.title_text = "Sign in to the FT"
    page.url = FT_LOGIN + "?location=%2F"

    r = client.get(f"/api/signin/{body['id']}")
    assert r.status_code == 200
    frame = r.json()
    assert set(frame) == {"id", "site", "url", "title", "width", "height",
                          "image"}
    assert base64.b64decode(frame["image"]) == JPEG
    assert frame["url"] == FT_LOGIN + "?location=%2F"
    assert frame["title"] == "Sign in to the FT"
    assert ("screenshot", "jpeg", 55) in page.actions

    # an id that never existed, and one that has been closed
    gone = client.get("/api/signin/deadbeef")
    assert gone.status_code == 404
    assert gone.json()["detail"] == "That sign-in has ended."


def test_signin_frame_when_the_page_stops_answering(con, monkeypatch, sidecar,
                                                    client):
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    page.screenshot_error = RuntimeError("Target closed")

    r = client.get(f"/api/signin/{body['id']}")
    assert r.status_code == 409
    assert r.json()["detail"] == "The sign-in page did not respond. Try again."
    # the session stays: the next poll may well work
    assert len(signin._SESSIONS) == 1


def test_signin_a_page_that_refuses_frames_is_still_in_use(con, monkeypatch,
                                                           sidecar, client):
    """A frame that fails is still an analyst at an open sign-in: polling keeps
    the session alive, or the idle sweep closes it under the modal."""
    clock = {"t": 5_000.0}
    monkeypatch.setattr(signin, "now", lambda: clock["t"])
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    page.screenshot_error = RuntimeError("Target closed")

    for _ in range(3):                        # an hour of failing polls
        clock["t"] += signin.IDLE_S - 60
        assert client.get(f"/api/signin/{body['id']}").status_code == 409

    # the page comes back and the session is still the one that was open
    page.screenshot_error = None
    clock["t"] += 60
    r = client.get(f"/api/signin/{body['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == body["id"]


# ---------------------------------------------------------------- events


def test_signin_forwards_clicks_keys_and_scrolls(con, monkeypatch, sidecar,
                                                 client):
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    url = f"/api/signin/{body['id']}/event"

    assert client.post(url, json={"kind": "click", "x": 640, "y": 412}).json() \
        == {"ok": True, "url": FT_LOGIN}
    client.post(url, json={"kind": "dblclick", "x": 12, "y": 0})
    client.post(url, json={"kind": "type", "text": "analyst@example.com"})
    client.post(url, json={"kind": "key", "key": "Enter"})
    client.post(url, json={"kind": "key", "key": "Control+a"})
    client.post(url, json={"kind": "scroll", "dy": 300})

    assert page.actions[1:] == [
        ("click", 640, 412, 1), ("click", 12, 0, 2),
        ("type", "analyst@example.com"), ("press", "Enter"),
        ("press", "Control+a"), ("wheel", 0, 300)]


def test_signin_refuses_input_it_should_not_forward(con, monkeypatch, sidecar,
                                                    client):
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    url = f"/api/signin/{body['id']}/event"
    before = len(page.actions)

    bad = [
        {"kind": "click", "x": 1280, "y": 10},      # outside the viewport
        {"kind": "click", "x": -1, "y": 10},
        {"kind": "click", "y": 10},                 # no x at all
        {"kind": "key", "key": "F12"},              # not on the allowlist
        {"kind": "key", "key": "Control+Shift+I"},
        {"kind": "key", "key": ""},
        {"kind": "type", "text": "x" * 501},
        {"kind": "sudo"},
        {"kind": ""},
    ]
    for payload in bad:
        r = client.post(url, json=payload)
        assert r.status_code == 400, payload
        assert r.json()["detail"]
    assert len(page.actions) == before        # the page was never touched

    # an unknown session, whatever the event
    assert client.post("/api/signin/nosuchid/event",
                       json={"kind": "click", "x": 1, "y": 1}).status_code == 404


def test_signin_goto_stays_on_the_site(con, monkeypatch, sidecar, client):
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    url = f"/api/signin/{body['id']}/event"

    r = client.post(url, json={"kind": "goto",
                               "url": "https://www.ft.com/products"})
    assert r.json() == {"ok": True, "url": "https://www.ft.com/products"}
    assert page.actions[-1] == ("goto", "https://www.ft.com/products",
                                "domcontentloaded", signin.NAV_TIMEOUT_MS)

    for address in ("https://www.google.com/", "https://notft.com/login",
                    "file:///etc/passwd", "http://localhost:9222/json",
                    "https://www.wsj.com/client/login", ""):
        r = client.post(url, json={"kind": "goto", "url": address})
        assert r.status_code == 400, address
        assert r.json()["detail"] == "That address is not part of the sign-in."

    # the WSJ session takes the Dow Jones hosts, and not FT's
    install(monkeypatch)
    wsj = client.post("/api/signin/wsj").json()
    wsj_url = f"/api/signin/{wsj['id']}/event"
    assert client.post(wsj_url, json={"kind": "goto",
                                      "url": "https://sso.accounts.dowjones.com/login"
                                      }).status_code == 200
    assert client.post(wsj_url, json={"kind": "goto",
                                      "url": FT_LOGIN}).status_code == 400


def test_signin_event_when_the_page_stops_answering(con, monkeypatch, sidecar,
                                                    client):
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    page.input_error = RuntimeError("Execution context was destroyed")

    r = client.post(f"/api/signin/{body['id']}/event",
                    json={"kind": "click", "x": 10, "y": 10})
    assert r.status_code == 409
    assert r.json()["detail"] == "The sign-in page did not respond. Try again."


# ---------------------------------------------------------------- fast path
#
# The form fill itself (graph.signin._fill_login against a real login form) is
# tested in tests/test_signin_fill.py. Here it is stubbed, so these cover the
# orchestration: what is stored, when the live view is handed back, and that a
# password never leaves the API.

PASSWORD = "correct-horse-battery-staple"


def stub_fill(monkeypatch, outcome="ok"):
    """Replace the form fill. Returns a dict that records what it was asked to
    type, so the test can prove submit passes it through (and only through)."""
    seen = {}

    async def fake_fill(page, email, password):
        seen["email"] = email
        seen["password"] = password
        if outcome == "raise":
            raise RuntimeError("no email field on the page")

    monkeypatch.setattr(signin, "_fill_login", fake_fill)
    return seen


def test_submit_signs_in_and_stores_the_cookies(con, monkeypatch, sidecar,
                                                client):
    _pw, remote = install(monkeypatch)
    remote.contexts[0].jar = list(FT_JAR)
    seen = stub_fill(monkeypatch)

    r = client.post("/api/signin/ft/submit",
                    json={"email": "analyst@example.com", "password": PASSWORD})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True and out["signed_in"] is True
    assert out.get("needs_view") in (None, False)
    assert out["cookies"] == 2                    # the google.com one is not ours
    assert out["message"] == "Saved the FT sign-in."
    # neither the password nor a cookie value ever leaves the API
    assert PASSWORD not in r.text and FT_SECRET not in r.text

    # the form got exactly what the analyst typed, and nothing else
    assert seen == {"email": "analyst@example.com", "password": PASSWORD}

    stored = credentials.get(con, "ft")["value"]
    assert f"FTSession_s\t{FT_SECRET}" in stored
    # the session is closed once the cookies are in
    assert signin._SESSIONS == {}
    assert remote.pages[0].closed is True

    credentials.delete(con, "ft")
    con.commit()


def test_submit_hands_back_the_live_view_when_a_step_is_needed(
        con, monkeypatch, sidecar, client):
    """The form went in but no session cookie followed (a one-time code, a
    captcha): the same open session becomes the live view to finish by hand."""
    monkeypatch.setattr(signin, "SUBMIT_POLLS", 2)
    monkeypatch.setattr(signin, "POLL_S", 0)
    _pw, remote = install(monkeypatch)
    remote.contexts[0].jar = list(FT_JAR_ANONYMOUS)
    stub_fill(monkeypatch)

    r = client.post("/api/signin/ft/submit",
                    json={"email": "analyst@example.com", "password": PASSWORD})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True and out["signed_in"] is False
    assert out["needs_view"] is True
    assert out["message"] == signin.NEEDS_VIEW
    session = out["session"]
    assert session["site"] == "ft" and session["url"] == FT_LOGIN
    assert PASSWORD not in r.text

    # the same session is live: its frame endpoint answers, and it is the only
    # one open
    assert len(signin._SESSIONS) == 1
    assert client.get(f"/api/signin/{session['id']}").status_code == 200
    assert credentials.get(con, "ft") is None


def test_submit_hands_back_the_live_view_when_the_form_is_not_found(
        con, monkeypatch, sidecar, client):
    _pw, remote = install(monkeypatch)
    remote.contexts[0].jar = list(FT_JAR_ANONYMOUS)
    stub_fill(monkeypatch, outcome="raise")

    r = client.post("/api/signin/ft/submit",
                    json={"email": "analyst@example.com", "password": PASSWORD})
    out = r.json()
    assert out["signed_in"] is False and out["needs_view"] is True
    # the session stays open for the analyst to take over
    assert client.get(f"/api/signin/{out['session']['id']}").status_code == 200
    assert credentials.get(con, "ft") is None


def test_submit_reuses_an_open_session_and_returns_to_the_login_page(
        con, monkeypatch, sidecar, client):
    _pw, remote = install(monkeypatch)
    remote.contexts[0].jar = list(FT_JAR)
    client.post("/api/signin/ft")             # the live view is already open
    page = remote.pages[0]
    goto_login = ("goto", FT_LOGIN, "domcontentloaded", signin.NAV_TIMEOUT_MS)
    before = page.actions.count(goto_login)
    stub_fill(monkeypatch)

    r = client.post("/api/signin/ft/submit",
                    json={"email": "analyst@example.com", "password": PASSWORD})
    assert r.json()["signed_in"] is True
    # a resumed session is put back on the login page before the form is filled
    assert page.actions.count(goto_login) == before + 1
    assert len(remote.pages) == 1             # the same page, no second tab

    credentials.delete(con, "ft")
    con.commit()


def test_submit_needs_an_email_and_a_password(con, monkeypatch, sidecar, client):
    pw, _remote = install(monkeypatch)
    for body in ({"email": "", "password": PASSWORD},
                 {"email": "analyst@example.com", "password": ""},
                 {"email": "   ", "password": "x"}):
        r = client.post("/api/signin/ft/submit", json=body)
        assert r.status_code == 400, body
        assert r.json()["detail"] == "Enter your email and password."
    assert pw.calls == []                      # the sidecar was never dialled


def test_submit_only_for_ft_and_wsj(con, monkeypatch, sidecar, client):
    pw, _remote = install(monkeypatch)
    r = client.post("/api/signin/substack/submit",
                    json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 400
    assert r.json()["detail"] == "No sign-in for that site."
    assert pw.calls == []


def test_submit_without_the_sidecar(con, monkeypatch, client):
    monkeypatch.delenv("CAF_BROWSER_URL", raising=False)
    pw, _remote = install(monkeypatch)
    r = client.post("/api/signin/ft/submit",
                    json={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 503
    assert r.json()["detail"] == "The browser is not running."
    assert signin._SESSIONS == {}
    assert pw.calls == []


# ---------------------------------------------------------------- finish


def test_signin_finish_needs_the_session_cookie(con, monkeypatch, sidecar,
                                                client):
    body, _pw, _remote, _page = open_ft(client, monkeypatch, jar=FT_JAR_ANONYMOUS)

    r = client.post(f"/api/signin/{body['id']}/finish")
    assert r.status_code == 200
    out = r.json()
    assert set(out) == {"ok", "cookies", "signed_in", "message"}
    assert out["ok"] is False and out["signed_in"] is False
    assert out["cookies"] == 1
    assert out["message"] == "No session cookie yet. Finish signing in first."

    # nothing stored, and the session is still there to finish signing in
    assert credentials.get(con, "ft") is None
    assert client.get(f"/api/signin/{body['id']}").status_code == 200


def test_signin_finish_stores_the_cookies_and_unblocks_the_site(
        con, monkeypatch, sidecar, client):
    blocked = "https://www.ft.com/content/00000000-0000-0000-0000-0000000signin"
    con.execute("insert into link_queue (url, kind, site, status, error, "
                "attempts, added_by) values (%s, 'article', 'ft', 'blocked', "
                "'Sign-in needed for FT.', 0, 'web')", (blocked,))
    con.execute("insert into source (name, connector, url, config, status, "
                "added_by, last_error, last_error_at) values "
                "('signin-ft-feed', 'rss', 'https://www.ft.com/rss/home', "
                "'{\"site\": \"ft\"}', 'active', 'web', 'Sign-in needed', now())")
    con.commit()
    body, pw, remote, page = open_ft(client, monkeypatch, jar=FT_JAR)

    r = client.post(f"/api/signin/{body['id']}/finish")
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True and out["signed_in"] is True
    assert out["cookies"] == 2                  # the google.com one is not ours
    assert out["message"] == "Saved the FT sign-in."
    assert FT_SECRET not in r.text              # the value never leaves the API

    stored = credentials.get(con, "ft")["value"]
    assert stored.startswith("# Netscape HTTP Cookie File\n")
    assert f".ft.com\tTRUE\t/\tTRUE\t1790000000\tFTSession_s\t{FT_SECRET}" in stored
    # a cookie that dies with the browser is written far-future: the file has
    # no column for "until the browser closes"
    assert ("accounts.ft.com\tFALSE\t/login\tTRUE\t2147483647\tFTSession\t"
            "legacy-session") in stored
    assert "google.com" not in stored
    # and it is a value the fetchers can read back
    jar = credentials.cookie_jar(stored, "www.ft.com", "ft")
    assert jar["FTSession_s"] == FT_SECRET

    # the sign-in is what those rows were waiting for
    assert con.execute("select status, error from link_queue where url=%s",
                       (blocked,)).fetchone()["status"] == "queued"
    row = con.execute("select last_error, last_error_at from source "
                      "where name='signin-ft-feed'").fetchone()
    assert row["last_error"] is None and row["last_error_at"] is None

    # the session is closed: the page is gone, the CDP session is disconnected
    assert client.get(f"/api/signin/{body['id']}").status_code == 404
    assert page.closed is True
    assert remote.closed is True and pw.stopped is True
    # cloakserve's own context stays open; it is not ours to close
    assert remote.contexts[0].closed is False

    credentials.delete(con, "ft")
    con.commit()


def test_signin_finish_when_the_cookies_cannot_be_read(con, monkeypatch,
                                                       sidecar, client):
    remote = FakeBrowser(cookies_error=RuntimeError("Target closed"))
    pw = FakePlaywright(remote)
    monkeypatch.setattr(signin, "async_playwright", lambda: FakeStarter(pw))
    body = client.post("/api/signin/ft").json()

    r = client.post(f"/api/signin/{body['id']}/finish")
    assert r.status_code == 409
    assert r.json()["detail"] == "The sign-in page did not respond. Try again."
    assert credentials.get(con, "ft") is None

    assert client.post("/api/signin/nosuchid/finish").status_code == 404


# ---------------------------------------------------------------- closing


def test_signin_delete_closes_without_storing(con, monkeypatch, sidecar,
                                              client):
    body, pw, remote, page = open_ft(client, monkeypatch, jar=FT_JAR)

    assert client.delete(f"/api/signin/{body['id']}").json() == {"ok": True}
    assert page.closed is True
    assert remote.closed is True and pw.stopped is True
    assert remote.contexts[0].closed is False
    assert credentials.get(con, "ft") is None
    assert client.get(f"/api/signin/{body['id']}").status_code == 404

    # closing twice is not an error: the button and the page unload both fire
    assert client.delete(f"/api/signin/{body['id']}").json() == {"ok": True}


def test_signin_closes_an_idle_session(con, monkeypatch, sidecar, client):
    clock = {"t": 1_000.0}
    monkeypatch.setattr(signin, "now", lambda: clock["t"])
    body, pw, remote, page = open_ft(client, monkeypatch)

    # nineteen minutes of nothing, and the session is still there
    clock["t"] += signin.IDLE_S - 60
    assert client.get(f"/api/signin/{body['id']}").status_code == 200

    # each frame is a use, so an open live view never times out
    clock["t"] += signin.IDLE_S - 60
    assert client.get(f"/api/signin/{body['id']}").status_code == 200

    clock["t"] += signin.IDLE_S + 1
    r = client.get(f"/api/signin/{body['id']}")
    assert r.status_code == 404
    assert r.json()["detail"] == "That sign-in has ended."
    assert page.closed is True
    assert remote.closed is True and pw.stopped is True
    assert signin._SESSIONS == {}

    # and the site is free to be signed into again
    install(monkeypatch)
    assert client.post("/api/signin/ft").json()["resumed"] is False


def test_signin_never_sweeps_a_session_mid_request(con, monkeypatch, sidecar,
                                                   client):
    """A request holding the page must not have it closed under it: one input
    at a time, and the sweep leaves a session that is being used alone."""
    clock = {"t": 2_000.0}
    monkeypatch.setattr(signin, "now", lambda: clock["t"])
    body, _pw, _remote, page = open_ft(client, monkeypatch)
    session = signin._SESSIONS[body["id"]]
    clock["t"] += signin.IDLE_S + 1

    async def sweep_while_busy():
        async with session.lock:              # as an in-flight event holds it
            return await signin.sweep()

    assert asyncio.run(sweep_while_busy()) == 0
    assert body["id"] in signin._SESSIONS and page.closed is False


# ---------------------------------------------------------------- units


def test_netscape_serialization_round_trips():
    text = signin.netscape([
        {"name": "DJSESSION", "value": "abc", "domain": ".wsj.com",
         "path": "/", "expires": 1790000000.5, "secure": True},
        {"name": "sso", "value": "def", "domain": "sso.accounts.dowjones.com",
         "path": "", "expires": 0, "secure": False},
        {"name": "", "value": "no name", "domain": ".wsj.com"},
        {"name": "nodomain", "value": "x", "domain": ""},
    ])
    lines = text.strip().split("\n")
    assert lines[0] == "# Netscape HTTP Cookie File"
    assert lines[1] == ".wsj.com\tTRUE\t/\tTRUE\t1790000000\tDJSESSION\tabc"
    assert lines[2] == ("sso.accounts.dowjones.com\tFALSE\t/\tFALSE\t"
                        "2147483647\tsso\tdef")
    assert len(lines) == 3                  # the two unusable rows are dropped
    assert credentials.cookie_jar(text, "www.wsj.com", "wsj") == {"DJSESSION": "abc"}


def test_cookies_are_kept_to_the_site():
    assert signin._in_site(".ft.com", ("ft.com",)) is True
    assert signin._in_site("accounts.ft.com", ("ft.com",)) is True
    assert signin._in_site("notft.com", ("ft.com",)) is False
    assert signin._in_site(".google.com", ("ft.com",)) is False
    assert signin._in_site("", ("ft.com",)) is False
    assert signin._in_site(".dowjones.com", ("wsj.com", "dowjones.com")) is True
