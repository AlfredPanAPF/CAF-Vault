"""Substack custom-domain sessions (build spec v4 §3.4): the pasted sid signs
in on substack.com hosts; a publication on its own domain takes a session of
its own, minted through Substack's cross-domain sign-in and kept per host in
credential_session. Network is mocked at fetch.get; nothing here prints or
stores a cookie value anywhere but the table."""
import json
from urllib.parse import parse_qs, urlsplit

from graph import credentials, fetch, substack_session
from graph.connectors import rss

ORIGIN = "https://letter.example.com"
HOST = "letter.example.com"
SID = "s%3Athe-substack-com-sid.sig"
FULL = "<p>" + ("Every word of the paid post. " * 200) + "</p>"
CUT = "<p>" + ("Just the preview. " * 20) + "</p>"


class Resp:
    def __init__(self, text="", status=200, headers=None, cookies=None,
                 cookie_expires=None):
        self.text = text
        self.status = status
        self.bytes = text.encode()
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.cookie_expires = dict(cookie_expires or {})

    @property
    def ok(self):
        return 200 <= self.status < 300


def post_json(body_html, wordcount=1200):
    return json.dumps({"title": "Paid", "audience": "only_paid",
                       "body_html": body_html, "wordcount": wordcount})


def network(calls, *, sid_dead=False, for_pub="letter", complete_host=HOST,
            posts=None, session_value="s%3Aminted-for-letter.sig", expires=None):
    """fetch.get for the handshake and the post API. `posts` maps the
    connect.sid value the request carried (None for anonymous) to the body
    the post API answers with."""
    def get(url, **kw):
        calls.append((url, kw.get("site"), dict(kw.get("cookies") or {}),
                      kw.get("allow_redirects", True)))
        host = urlsplit(url).hostname
        if url.endswith("/account/login?redirect=%2F"):
            return Resp(status=301, headers={
                "location": f"https://substack.com/sign-in?redirect=%2F&for_pub={for_pub}"
                            "&change_user=false"})
        if url.startswith(substack_session.SIGN_IN + "?"):
            q = parse_qs(urlsplit(url).query)
            assert q["for_pub"] == [for_pub]
            assert kw["cookies"]["substack.sid"]  # the stored sid goes along
            assert kw["cookies"]["substack.lli"] == "1"
            if sid_dead:
                return Resp("<html>Sign in to Substack</html>")
            return Resp(status=303, headers={
                "location": f"https://{complete_host}/api/v1/sign-in/local/complete"
                            f"?token=t0k&redirect={q['redirect'][0]}"})
        if "/api/v1/sign-in/local/complete" in url:
            assert not kw.get("cookies"), "the completion carries no cookies"
            return Resp(status=303, headers={"location": f"https://{host}/"},
                        cookies={"ab": "1", "connect.sid": session_value},
                        cookie_expires={"connect.sid": expires})
        if "/api/v1/posts/" in url:
            # the real fetch path resolves the jar from the table; do the same
            jar = fetch._cookies_for(kw.get("con"), kw.get("site"), url)
            body = (posts or {}).get(jar.get("connect.sid"), CUT)
            return Resp(post_json(body))
        raise AssertionError(f"unexpected fetch {url}")
    return get


def handshakes(calls):
    return [u for u, *_ in calls if "/account/login" in u]


def post_fetches(calls):
    return [u for u, *_ in calls if "/api/v1/posts/" in u]


def test_is_custom():
    assert substack_session.is_custom("https://letter.example.com/p/x")
    assert substack_session.is_custom("newsletter.semianalysis.com")
    assert not substack_session.is_custom("https://letter.substack.com/p/x")
    assert not substack_session.is_custom("substack.com")
    assert not substack_session.is_custom("https://open.substack.com/pub/l/p/x")
    assert not substack_session.is_custom("")


def test_cookies_for_substack_hosts_and_custom_domains(con, monkeypatch):
    credentials.set(con, "substack", SID)
    # substack.com and its subdomains: the pasted sid plus the bridge's flags
    for url in ("https://substack.com/api/v1/reader/posts",
                "https://letter.substack.com/api/v1/posts/x"):
        jar = fetch._cookies_for(con, "substack", url)
        assert jar["substack.sid"] == SID and jar["substack.lli"] == "1"
    # a custom domain: nothing until a session is minted for the host
    assert fetch._cookies_for(con, "substack", f"{ORIGIN}/api/v1/posts/x") == {}
    calls = []
    monkeypatch.setattr(fetch, "get", network(calls, expires=1_900_000_000))
    assert substack_session.ensure(con, ORIGIN) is True
    assert fetch._cookies_for(con, "substack", f"{ORIGIN}/api/v1/posts/x") == \
        {"connect.sid": "s%3Aminted-for-letter.sig"}
    # the substack.com sid never travels to the custom domain
    assert "substack.sid" not in fetch._cookies_for(con, "substack", f"{ORIGIN}/feed")
    # the three steps, each without following redirects; the completion's
    # expiry is kept
    assert [u.split("?")[0] for u, *_ in calls] == [
        f"{ORIGIN}/account/login", substack_session.SIGN_IN,
        f"{ORIGIN}/api/v1/sign-in/local/complete"]
    assert all(follow is False for *_, follow in calls)
    row = credentials.session_get(con, "substack", HOST)
    assert row["live"] and row["fresh"] and row["expires_at"] is not None
    assert row["expires_at"].year == 2030
    # stored once: ensure() again is a table read, not another handshake
    assert substack_session.ensure(con, ORIGIN) is True
    assert len(handshakes(calls)) == 1
    # and refresh() within the hour is refused (the session is not the problem)
    assert substack_session.refresh(con, ORIGIN) is False
    assert len(handshakes(calls)) == 1


def test_dead_sid_is_recorded_and_not_retried_at_once(con, monkeypatch):
    credentials.set(con, "substack", SID)
    calls = []
    monkeypatch.setattr(fetch, "get", network(calls, sid_dead=True))
    assert substack_session.ensure(con, ORIGIN) is False
    row = credentials.session_get(con, "substack", HOST)
    assert row["value"] == "" and not row["live"] and row["current"]
    assert credentials.session_jar(con, "substack", HOST) == {}
    # within the retry window nothing is minted again
    assert substack_session.ensure(con, ORIGIN) is False
    assert substack_session.refresh(con, ORIGIN) is False
    assert len(handshakes(calls)) == 1
    # once the window has passed, ensure() tries again
    con.execute("update credential_session set updated_at=now() - interval '2 hours', "
                "expires_at=now() - interval '1 hour' where host=%s", (HOST,))
    monkeypatch.setattr(fetch, "get", network(calls))
    assert substack_session.ensure(con, ORIGIN) is True
    assert len(handshakes(calls)) == 2


def test_handshake_guards(con, monkeypatch):
    credentials.set(con, "substack", SID)
    calls = []
    # substack.com hands the session to a host other than the publication's:
    # not followed, nothing stored as live
    monkeypatch.setattr(fetch, "get", network(calls, complete_host="evil.example.net"))
    assert substack_session.mint(con, ORIGIN) is False
    assert not any("/complete" in u for u, *_ in calls)
    # no sid pasted at all: no network
    credentials.delete(con, "substack")
    calls.clear()
    assert substack_session.ensure(con, ORIGIN) is False
    assert calls == []
    # substack.com hosts never mint
    credentials.set(con, "substack", SID)
    assert substack_session.ensure(con, "https://letter.substack.com") is False
    assert calls == []


def test_post_json_mints_then_refreshes_a_dead_session(con, monkeypatch):
    credentials.set(con, "substack", SID)
    calls = []
    minted = "s%3Aminted-for-letter.sig"
    monkeypatch.setattr(fetch, "get", network(calls, posts={minted: FULL}))
    # first signed fetch: a session is minted first, and the post comes whole
    data = rss.substack_post_json(con, ORIGIN, "the-post")
    assert rss.html_words(data["body_html"]) > 1000
    assert len(handshakes(calls)) == 1 and len(post_fetches(calls)) == 1
    # the anonymous fetch never touches the session
    calls.clear()
    data = rss.substack_post_json(None, ORIGIN, "the-post", anonymous=True)
    assert rss.substack_preview(data) and calls[0][2] == {} and not handshakes(calls)
    # the stored session stops working (Substack ended it): the post comes cut,
    # the session is an hour old, so a new one is minted and the post fetched
    # again, once
    con.execute("update credential_session set updated_at=now() - interval '2 hours' "
                "where host=%s", (HOST,))
    calls.clear()
    monkeypatch.setattr(fetch, "get", network(
        calls, session_value="s%3Asecond.sig", posts={"s%3Asecond.sig": FULL}))
    data = rss.substack_post_json(con, ORIGIN, "the-post")
    assert rss.html_words(data["body_html"]) > 1000
    assert len(handshakes(calls)) == 1 and len(post_fetches(calls)) == 2
    assert credentials.session_jar(con, "substack", HOST) == {"connect.sid": "s%3Asecond.sig"}
    # a post still cut with a fresh session is the account not subscribing:
    # no second handshake, no second fetch
    calls.clear()
    monkeypatch.setattr(fetch, "get", network(calls, posts={}))
    data = rss.substack_post_json(con, ORIGIN, "the-post")
    assert rss.substack_preview(data)
    assert handshakes(calls) == [] and len(post_fetches(calls)) == 1


def test_sessions_go_with_the_credential_and_stay_private(con, monkeypatch):
    credentials.set(con, "substack", SID)
    calls = []
    monkeypatch.setattr(fetch, "get", network(calls))
    assert substack_session.ensure(con, ORIGIN) is True
    # the session value is scrubbed from any message, like the sid itself
    assert credentials.scrub(con, "cookie s%3Aminted-for-letter.sig sent") == "cookie *** sent"
    # a new paste drops the sessions minted from the old one
    credentials.set(con, "substack", "s%3Aanother-sid.sig")
    assert credentials.session_get(con, "substack", HOST) is None
    assert substack_session.ensure(con, ORIGIN) is True   # minted again from the new one
    assert len(handshakes(calls)) == 2
    # deleting the credential takes the sessions with it
    credentials.delete(con, "substack")
    assert con.execute("select count(*) as n from credential_session "
                       "where site='substack'").fetchone()["n"] == 0


def test_failed_remint_keeps_the_live_session(con, monkeypatch):
    credentials.set(con, "substack", SID)
    calls = []
    monkeypatch.setattr(fetch, "get", network(calls))
    assert substack_session.ensure(con, ORIGIN) is True
    # an hour on, a cut post asks for a re-mint and substack.com answers its
    # sign-in page (a hiccup, or the sid just died): the working session is
    # kept, not replaced by a failure record ...
    con.execute("update credential_session set updated_at=now() - interval '2 hours' "
                "where host=%s", (HOST,))
    monkeypatch.setattr(fetch, "get", network(calls, sid_dead=True))
    assert substack_session.refresh(con, ORIGIN) is False
    assert credentials.session_jar(con, "substack", HOST) == \
        {"connect.sid": "s%3Aminted-for-letter.sig"}
    row = credentials.session_get(con, "substack", HOST)
    assert row["live"] and row["fresh"]
    # ... and its clock restarted, so the next cut post does not repeat the
    # handshake within the hour
    assert substack_session.refresh(con, ORIGIN) is False
    assert len(handshakes(calls)) == 2
    # a hop that raises is the same story
    con.execute("update credential_session set updated_at=now() - interval '2 hours' "
                "where host=%s", (HOST,))

    def broken(url, **kw):
        raise ConnectionError("reset")
    monkeypatch.setattr(fetch, "get", broken)
    assert substack_session.refresh(con, ORIGIN) is False
    assert credentials.session_jar(con, "substack", HOST) == \
        {"connect.sid": "s%3Aminted-for-letter.sig"}


def test_store_failure_does_not_abort_the_callers_transaction(con, monkeypatch):
    credentials.set(con, "substack", SID)
    calls = []
    monkeypatch.setattr(fetch, "get", network(calls))
    # the credential row vanishes between the handshake and the write (the
    # analyst deleted it mid-cycle): the FK fails inside mint's own savepoint,
    # the poll's transaction stays usable and nothing is stored
    real_set = credentials.session_set

    def vanish_then_set(con_, site, host, value, *a, **kw):
        con_.execute("delete from credential where site='substack'")
        return real_set(con_, site, host, value, *a, **kw)
    monkeypatch.setattr(credentials, "session_set", vanish_then_set)
    assert substack_session.mint(con, ORIGIN) is False
    assert con.execute("select 1 as ok").fetchone()["ok"] == 1
    assert con.execute("select count(*) as n from credential_session").fetchone()["n"] == 0
