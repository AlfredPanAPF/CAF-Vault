"""Sign-in sessions: the site's login page, live, inside the sidecar (build
spec v4 §7 and §10c).

FT and WSJ cookies used to be harvested in a headed browser on the owner's Mac
and pasted into the Sign-ins card. This does the same job from the app: a
session opens a page on the site's login form inside `caf-cloakbrowser`, the
sources page shows JPEG frames of it and forwards clicks, keys and scrolls, and
Done reads the context's cookies and stores them as the site's credential. It
is the browser the fetchers already use, with the same fingerprint seed, so the
session signed into here is the session that later loads articles.

Playwright's async API, because these are async endpoints and the browser
objects have to stay on the event loop that made them. Sessions live in the web
process (one uvicorn worker), at most one per site, viewport 1280x800, closed
after 20 minutes idle. Nothing here logs a cookie value, or a whole exception:
a Playwright error quotes the page it choked on, and a login page carries what
was typed into it.
"""
import asyncio
import base64
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from . import browser, credentials

try:  # the sidecar's client; a dev box without it still serves the API
    from playwright.async_api import async_playwright
except Exception:                              # pragma: no cover - not installed
    async_playwright = None

# Login pages and what a finished sign-in leaves behind. `hosts` is what the
# stored jar is filtered to (credentials.SITE_HOSTS says the same for the
# fetchers); `session` is the cookie whose presence means the form is done.
SITES = {
    "ft": {
        "name": "FT",
        "login": "https://accounts.ft.com/login",
        "hosts": ("ft.com",),
        "session": ("FTSession_s", "FTSession"),
    },
    "wsj": {
        "name": "WSJ",
        "login": "https://www.wsj.com/client/login",
        "hosts": ("wsj.com", "dowjones.com"),
        "session": ("DJSESSION", "sso", "djcs_session"),
    },
}

WIDTH, HEIGHT = 1280, 800
IDLE_S = 20 * 60
JPEG_QUALITY = 55
MAX_TEXT = 500
MAX_SCROLL = 20_000
FOREVER = 2147483647          # cookies.txt has no "session cookie" column
CONNECT_TIMEOUT_MS = 20_000
NAV_TIMEOUT_MS = 45_000

# keys the page may be sent: editing and navigation, plus one modifier and a
# letter (select-all, copy, paste). Nothing that reaches the browser itself.
KEYS = {"Enter", "Backspace", "Tab", "Escape", "Delete", "ArrowUp", "ArrowDown",
        "ArrowLeft", "ArrowRight", "Home", "End", "Space"}
MODIFIERS = ("Shift", "Control", "Meta")

BROWSER_DOWN = "The browser is not running."
NOT_A_SITE = "No sign-in for that site."
GONE = "That sign-in has ended."
NO_PAGE = "The sign-in page did not respond. Try again."
NO_COOKIE = "No session cookie yet. Finish signing in first."


@dataclass
class Session:
    id: str
    site: str
    pw: object
    browser: object
    context: object
    page: object
    created: float
    last_used: float
    width: int = WIDTH
    height: int = HEIGHT
    # one input at a time on this page: two forwarded events are two requests
    # and the loop is free to interleave them, which reorders typing
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SESSIONS: dict[str, Session] = {}


def now() -> float:
    """The one clock idle expiry reads (a test replaces it)."""
    return time.time()


# ---------------------------------------------------------------- lifetime


async def sweep(at: float | None = None) -> int:
    """Close sessions idle longer than IDLE_S; return how many went. Called at
    the start of every endpoint, which is the only tick this needs: an open
    live view polls for frames and nothing else keeps a browser page around."""
    at = now() if at is None else at
    stale = [s for s in list(_SESSIONS.values())
             if at - s.last_used > IDLE_S and not s.lock.locked()]
    for session in stale:
        _SESSIONS.pop(session.id, None)
        print(f"signin: closing the idle {session.site} sign-in")
        await _close(session)
    return len(stale)


def _get(session_id) -> Session:
    """The session, and it counts as used from here. The clock is bumped on
    the way in, not once the page has answered: a page that keeps refusing
    screenshots is still an analyst sitting in front of an open sign-in, and a
    request already awaiting Playwright must not be swept out from under."""
    session = _SESSIONS.get(str(session_id))
    if session is None:
        raise HTTPException(404, GONE)
    session.last_used = now()
    return session


def _for_site(site: str) -> Session | None:
    return next((s for s in _SESSIONS.values() if s.site == site), None)


async def _quit(remote, pw) -> None:
    """Disconnect from the sidecar. Its Chrome and its persistent context stay
    up: they belong to cloakserve, not to this session."""
    for step in (getattr(remote, "close", None), getattr(pw, "stop", None)):
        if step is None:
            continue
        try:
            await step()
        except Exception as e:
            print(f"signin: disconnect problem ({type(e).__name__})")


async def _close(session: Session) -> None:
    try:
        await session.page.close()
    except Exception as e:
        print(f"signin: could not close the page ({type(e).__name__})")
    await _quit(session.browser, session.pw)


async def _info(session: Session) -> tuple[str, str]:
    """Where the page is and what it calls itself; never raises (a title read
    across a navigation does)."""
    try:
        url = session.page.url
    except Exception:
        url = ""
    try:
        title = await session.page.title()
    except Exception:
        title = ""
    return str(url or ""), str(title or "")


async def _describe(session: Session, resumed: bool) -> dict:
    url, title = await _info(session)
    return {"id": session.id, "site": session.site, "url": url, "title": title,
            "width": session.width, "height": session.height,
            "resumed": resumed}


# ---------------------------------------------------------------- session


async def _open(site: str) -> Session:
    """Connect to the sidecar and land a page on the site's login form."""
    spec = SITES[site]
    pw = remote = page = None
    try:
        pw = await async_playwright().start()
        remote = await pw.chromium.connect_over_cdp(browser.cdp_url(site),
                                                    timeout=CONNECT_TIMEOUT_MS)
        # cloakserve's Chrome already has its default context open, and that is
        # the one holding the cf_clearance / datadome cookies a cleared wall
        # left behind: sign in there and the next article fetch skips the
        # challenge
        ctx = remote.contexts[0] if remote.contexts else await remote.new_context()
        page = await ctx.new_page()
        await page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
    except Exception as e:
        print(f"signin: the browser did not open a {site} page "
              f"({type(e).__name__})")
        if page is not None:
            # the context is cloakserve's and outlives the disconnect, so a
            # half-set-up tab would sit in the browser the fetchers share
            try:
                await page.close()
            except Exception as close_error:
                print(f"signin: could not close the half-open page "
                      f"({type(close_error).__name__})")
        await _quit(remote, pw)
        raise HTTPException(503, BROWSER_DOWN) from None
    try:
        await page.goto(spec["login"], wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS)
    except Exception as e:
        # a slow or half-loaded login page is still something to sign in on:
        # the frame shows whatever arrived
        print(f"signin: the {site} login page did not settle "
              f"({type(e).__name__})")
    at = now()
    return Session(id=uuid.uuid4().hex, site=site, pw=pw, browser=remote,
                   context=ctx, page=page, created=at, last_used=at)


async def start(site: str) -> dict:
    """Open the sign-in for `site`, or hand back the one already open."""
    await sweep()
    if site not in SITES:
        raise HTTPException(400, NOT_A_SITE)
    live = _for_site(site)
    if live is not None:
        live.last_used = now()
        return await _describe(live, resumed=True)
    if async_playwright is None or not browser.enabled():
        raise HTTPException(503, BROWSER_DOWN)
    session = await _open(site)
    live = _for_site(site)
    if live is not None:          # two clicks raced the connect; keep the first
        await _close(session)
        live.last_used = now()
        return await _describe(live, resumed=True)
    _SESSIONS[session.id] = session
    return await _describe(session, resumed=False)


async def frame(session_id) -> dict:
    """One JPEG of the page viewport, base64, plus where the page is."""
    await sweep()
    session = _get(session_id)
    try:
        shot = await session.page.screenshot(type="jpeg", quality=JPEG_QUALITY)
    except Exception as e:
        print(f"signin: no frame from the {session.site} page "
              f"({type(e).__name__})")
        raise HTTPException(409, NO_PAGE) from None
    url, title = await _info(session)
    return {"id": session.id, "site": session.site, "url": url, "title": title,
            "width": session.width, "height": session.height,
            "image": base64.b64encode(shot).decode("ascii")}


async def close(session_id) -> dict:
    """Done with it, nothing stored. Closing twice is not an error: the page
    unload fires this and so does the button."""
    await sweep()
    session = _SESSIONS.pop(str(session_id), None)
    if session is not None:
        await _close(session)
    return {"ok": True}


# ---------------------------------------------------------------- events


def _int(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _point(ev: dict, session: Session) -> tuple[int, int]:
    x, y = _int(ev.get("x")), _int(ev.get("y"))
    if x is None or y is None or not 0 <= x < session.width \
            or not 0 <= y < session.height:
        raise HTTPException(400, "That click is outside the page.")
    return x, y


def _key(value) -> str:
    key = str(value or "").strip()
    if key in KEYS:
        return key
    mod, _, rest = key.partition("+")
    if mod in MODIFIERS and len(rest) == 1 and rest.isascii() and rest.isalpha():
        return key
    raise HTTPException(400, "That key is not allowed.")


def _target(value, site: str) -> str:
    """A goto stays on the site being signed into: this is a browser the
    server drives, and any address would make it an open proxy."""
    url = str(value or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and _in_site(host, SITES[site]["hosts"]):
        return url
    raise HTTPException(400, "That address is not part of the sign-in.")


async def event(session_id, ev: dict) -> dict:
    """One input on the live page: click, dblclick, type, key, scroll, goto."""
    await sweep()
    session = _get(session_id)
    ev = ev or {}
    kind = str(ev.get("kind") or "")
    page = session.page
    # the lock keeps the typed password ahead of the Enter that submits it,
    # whatever order the two requests reach the loop in
    async with session.lock:
        try:
            if kind in ("click", "dblclick"):
                x, y = _point(ev, session)
                await page.mouse.click(x, y,
                                       click_count=2 if kind == "dblclick" else 1)
            elif kind == "type":
                text = str(ev.get("text") or "")
                if len(text) > MAX_TEXT:
                    raise HTTPException(400,
                                        "That is too much text to type at once.")
                await page.keyboard.type(text)
            elif kind == "key":
                await page.keyboard.press(_key(ev.get("key")))
            elif kind == "scroll":
                dy = _int(ev.get("dy")) or 0
                await page.mouse.wheel(0, max(-MAX_SCROLL, min(MAX_SCROLL, dy)))
            elif kind == "goto":
                await page.goto(_target(ev.get("url"), session.site),
                                wait_until="domcontentloaded",
                                timeout=NAV_TIMEOUT_MS)
            else:
                raise HTTPException(400, "That is not a sign-in action.")
        except HTTPException:
            raise
        except Exception as e:
            print(f"signin: the {session.site} page refused a {kind or 'blank'} "
                  f"({type(e).__name__})")
            raise HTTPException(409, NO_PAGE) from None
    url, _title = await _info(session)
    return {"ok": True, "url": url}


# ---------------------------------------------------------------- finish


def _in_site(domain, hosts: tuple[str, ...]) -> bool:
    dom = str(domain or "").lstrip(".").lower()
    return any(dom == host or dom.endswith("." + host) for host in hosts)


def netscape(cookies: list[dict]) -> str:
    """Playwright cookies -> a cookies.txt body, the shape credentials.set
    stores and every fetcher already reads. A session cookie (expires 0 or -1)
    is written as far-future: the file has no column for "until the browser
    closes", and this browser is about to."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        domain = str(c.get("domain") or "")
        name = str(c.get("name") or "")
        if not domain or not name:
            continue
        try:
            expires = int(float(c.get("expires")))
        except (TypeError, ValueError):
            expires = 0
        lines.append("\t".join([
            domain, "TRUE" if domain.startswith(".") else "FALSE",
            str(c.get("path") or "/"), "TRUE" if c.get("secure") else "FALSE",
            str(expires if expires > 0 else FOREVER),
            name, str(c.get("value") or "")]))
    return "\n".join(lines) + "\n"


async def finish(session_id, con) -> dict:
    """Read the site's cookies out of the context and store them as the
    credential. Without the session cookie there is nothing worth storing, so
    the session stays open and says so."""
    from . import webapp  # local import: webapp imports this module
    await sweep()
    session = _get(session_id)
    spec = SITES[session.site]
    try:
        jar = await session.context.cookies()
    except Exception as e:
        print(f"signin: could not read the {session.site} cookies "
              f"({type(e).__name__})")
        raise HTTPException(409, NO_PAGE) from None
    mine = [c for c in (jar or [])
            if c.get("name") and _in_site(c.get("domain"), spec["hosts"])]
    names = {str(c["name"]) for c in mine}
    if not any(n in names for n in spec["session"]):
        print(f"signin: {session.site} has no session cookie yet "
              f"({len(mine)} cookies)")
        return {"ok": False, "cookies": len(mine), "signed_in": False,
                "message": NO_COOKIE}

    def store():
        """The database half, off the loop: requeueing a big backlog of blocked
        links and committing it would otherwise stall every other request,
        including the frame polls of the sign-in still open on the other site."""
        credentials.set(con, session.site, netscape(mine))
        webapp.credential_saved(con, session.site)
        con.commit()

    await run_in_threadpool(store)
    _SESSIONS.pop(session.id, None)
    await _close(session)
    print(f"signin: stored {len(mine)} cookies for {session.site}")
    return {"ok": True, "cookies": len(mine), "signed_in": True,
            "message": f"Saved the {spec['name']} sign-in."}
