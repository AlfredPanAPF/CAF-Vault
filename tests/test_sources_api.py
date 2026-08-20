"""Sources API (build spec v4 §7): the URL box, one-off links, sign-ins.

Nothing here touches the network. The router is monkeypatched with the
Resolution its own tests already pin (tests/test_router.py), `graph.fetch.get`
is faked for the link queue, and the credential probes are faked or short-
circuited by having no credential stored. Every response body is checked
against the pasted secret: a credential value must never leave the API.
"""
import argparse
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from graph import cli, credentials, fetch, probes, router, substack_session, webapp
from graph.connectors import links

# a cookies.txt export and a bearer token; the values must never come back
FT_SECRET = "s%3AJ2ftsessionsupersecret.value"
FT_COOKIES = ("# Netscape HTTP Cookie File\n"
              f".ft.com\tTRUE\t/\tTRUE\t2147483647\tFTSession\t{FT_SECRET}\n")
X_SECRET = "AAAAAAAAAAAAAAAAtokenvalue0000"


def article_html(marker="chips", title="Chips and the grid"):
    """A believable article page. Each test passes its own marker: the event
    envelope treats near-identical bodies as duplicates (design §4.4), so two
    tests sharing one body would ingest the second as a mirror."""
    body = "".join(
        f"<p>Utilities negotiating {marker} contracts told regulators that "
        f"substation queues, not silicon, now set the delivery date "
        f"({i} of nine).</p>" for i in range(9))
    return ("<html><head>"
            f"<title>{title}</title>"
            f'<meta property="og:title" content="{title}">'
            '<meta property="article:published_time" content="2026-08-14T09:00:00Z">'
            f"</head><body><article>{body}</article></body></html>")


class FakeResponse:
    """The little of graph.fetch.Response the link queue reads."""

    def __init__(self, text, status=200, headers=None, cookies=None):
        self.text = text
        self.status = status
        self.bytes = text.encode("utf-8")
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})
        self.cookie_expires = {}

    @property
    def ok(self):
        return 200 <= self.status < 300


def feed_resolution(url, name):
    return router.Resolution(
        kind="feed", connector="rss", label="News feed", url=url, name=name,
        feed_url=url, site=None, config={}, one_off=False)


def link_resolution(url, site=None, kind="article"):
    return router.Resolution(
        kind="link", connector="link", label="Article", url=url,
        name=(url.split("/")[2]), site=site, config={}, one_off=True,
        link_kind=kind)


def fixed_router(monkeypatch, resolution):
    monkeypatch.setattr(router, "resolve", lambda con, url: resolution)


# ---------------------------------------------------------------- resolve


def test_api_sources_resolve(con, monkeypatch):
    client = TestClient(webapp.app)
    res = feed_resolution("https://chips.example.com/feed.xml", "Chips Daily")
    fixed_router(monkeypatch, res)

    body = client.post("/api/sources/resolve",
                       json={"url": " chips.example.com/feed.xml "}).json()
    assert set(body) == {"kind", "connector", "label", "url", "name",
                         "feed_url", "site", "config", "one_off", "link_kind",
                         "credential", "message"}
    assert body["kind"] == "feed"
    assert body["label"] == "News feed"
    assert body["name"] == "Chips Daily"
    assert body["one_off"] is False

    # empty and non-URL strings never reach the router
    assert client.post("/api/sources/resolve", json={"url": "  "}).status_code == 400
    assert client.post("/api/sources/resolve",
                       json={"url": "not a link"}).status_code == 400
    # a single-label host is a container name, not a site
    assert client.post("/api/sources/resolve",
                       json={"url": "http://postgres.:5432/"}).status_code == 400
    # link_queue.url is uniquely indexed, and a btree entry past ~2.7 KB is
    # refused: the length check answers with the copy instead of a 500
    long_url = "https://x.com/someuser/status/1234567890?z=" + "a1b2c3d4" * 400
    r = client.post("/api/sources", json={"url": long_url})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("That does not look like a link.")


# ---------------------------------------------------------------- add source


def test_api_sources_post_feed(con, monkeypatch):
    client = TestClient(webapp.app)
    url = "https://chipsfeed.example.com/rss.xml"
    fixed_router(monkeypatch, feed_resolution(url, "Chips feed"))

    body = client.post("/api/sources", json={"url": url}).json()
    assert body["ok"] is True
    assert body["kind"] == "source"
    source = body["source"]
    assert set(source) == {"source_id", "name", "connector", "label", "site",
                           "url", "feed_url", "status", "last_polled",
                           "last_error", "events", "credential"}
    assert source["name"] == "Chips feed"
    assert source["label"] == "News feed"
    assert source["feed_url"] == url
    assert source["status"] == "active"
    assert source["credential"] is None

    row = con.execute("select * from source where source_id=%s",
                      (source["source_id"],)).fetchone()
    assert row["connector"] == "rss"
    assert row["url"] == url
    assert row["added_by"] == "web"
    assert row["config"] == {}

    # the same feed again is a duplicate, not a second row
    r = client.post("/api/sources", json={"url": url})
    assert r.status_code == 409
    assert r.json()["detail"] == "Already added as Chips feed."

    # an unsupported paste carries the router's own message
    fixed_router(monkeypatch,
                 router.Resolution(kind="unsupported", label="Unsupported",
                                   url="https://nope.example.com",
                                   message=router.NO_FEED))
    r = client.post("/api/sources", json={"url": "https://nope.example.com"})
    assert r.status_code == 400
    assert r.json()["detail"] == router.NO_FEED


def test_api_sources_post_youtube_channel(con, monkeypatch):
    client = TestClient(webapp.app)
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtestchannel1"
    fixed_router(monkeypatch, router.Resolution(
        kind="youtube", connector="youtube", label="YouTube channel",
        url="https://www.youtube.com/@chipstalk", name="YouTube: Chips Talk",
        feed_url=feed, site="youtube",
        config={"site": "youtube", "channel_id": "UCtestchannel1",
                "title": "Chips Talk"},
        credential={"site": "youtube", "set": False}))

    source = client.post("/api/sources",
                         json={"url": "youtube.com/@chipstalk"}).json()["source"]
    assert source["connector"] == "youtube"
    assert source["label"] == "YouTube channel"
    assert source["site"] == "youtube"
    assert source["feed_url"] == feed
    # the sign-in badge comes from the credential table, never from the config
    assert source["credential"] == {"site": "youtube", "set": False}
    row = con.execute("select config from source where source_id=%s",
                      (source["source_id"],)).fetchone()
    assert row["config"]["channel_id"] == "UCtestchannel1"


# ---------------------------------------------------------------- one-off links


def test_api_sources_post_article_link(con, monkeypatch):
    client = TestClient(webapp.app)
    url = "https://news.example.com/2026/08/chips-and-the-grid"
    fixed_router(monkeypatch, link_resolution(url))
    monkeypatch.setattr(fetch, "get",
                        lambda u, **kw: FakeResponse(article_html("grid power")))

    body = client.post("/api/sources", json={"url": url}).json()
    assert body["kind"] == "link"
    link = body["link"]
    assert set(link) == {"link_id", "url", "title", "kind", "site", "status",
                         "error", "event_id", "created_at"}
    assert link["status"] == "done"          # non-media links run immediately
    assert link["kind"] == "article"
    assert link["event_id"] is not None
    assert link["title"] == "Chips and the grid"

    event = con.execute("select * from event where event_id=%s",
                        (link["event_id"],)).fetchone()
    assert event["connector"] == "link"
    assert event["meta"]["item_url"] == url

    # the link:<host> bucket the event landed in stays off the sources card
    names = {s["name"] for s in client.get("/api/sources").json()["sources"]}
    assert "link:news.example.com" not in names

    # the same URL again returns the same queue row, reported as what it is:
    # nothing was added the second time
    again = client.post("/api/sources", json={"url": url}).json()["link"]
    assert again["link_id"] == link["link_id"]
    assert again["status"] == "duplicate"
    assert con.execute("select status from link_queue where link_id=%s",
                       (link["link_id"],)).fetchone()["status"] == "done"


def test_api_link_blocked_then_credential_requeues_and_retry(con, monkeypatch):
    client = TestClient(webapp.app)
    url = "https://www.ft.com/content/00000000-0000-0000-0000-00000000abcd"
    fixed_router(monkeypatch, link_resolution(url, site="ft"))

    def walled(u, **kw):
        raise fetch.SignInNeeded("ft", "HTTP 403")

    monkeypatch.setattr(fetch, "get", walled)
    link = client.post("/api/sources", json={"url": url}).json()["link"]
    assert link["status"] == "blocked"
    assert link["error"] == "Sign-in needed for FT."
    assert link["event_id"] is None

    # saving the sign-in puts blocked links of that site back in the queue
    r = client.put("/api/credentials/ft", json={"value": FT_COOKIES})
    assert r.json() == {"ok": True, "site": "ft", "set": True}
    assert FT_SECRET not in r.text
    status = con.execute("select status from link_queue where link_id=%s",
                         (link["link_id"],)).fetchone()["status"]
    assert status == "queued"

    # retry with the wall gone finishes the row
    monkeypatch.setattr(fetch, "get",
                        lambda u, **kw: FakeResponse(article_html("ft subscribers")))
    retried = client.post(f"/api/links/{link['link_id']}/retry").json()
    assert retried["status"] == "done"
    assert retried["event_id"] is not None
    assert client.post(f"/api/links/{uuid.uuid4()}/retry").status_code == 404

    credentials.delete(con, "ft")
    con.commit()


# ---------------------------------------------------------------- remove


def test_api_sources_delete(con, monkeypatch):
    client = TestClient(webapp.app)
    url = "https://dropme.example.com/drop.atom"
    fixed_router(monkeypatch, feed_resolution(url, "Drop me"))
    source_id = client.post("/api/sources", json={"url": url}).json()["source"]["source_id"]

    assert client.delete(f"/api/sources/{source_id}").json() == {"ok": True}
    assert con.execute("select status from source where source_id=%s",
                       (source_id,)).fetchone()["status"] == "dropped"

    listed = {s["source_id"] for s in client.get("/api/sources").json()["sources"]}
    assert source_id not in listed
    status_ids = {s["source_id"] for s in client.get("/api/status").json()["sources"]}
    assert source_id not in status_ids
    assert client.delete(f"/api/sources/{uuid.uuid4()}").status_code == 404

    # a dropped source is gone for good: the same feed can be added again
    r = client.post("/api/sources", json={"url": url})
    assert r.status_code == 200


# ---------------------------------------------------------------- sign-ins


def test_api_credentials_never_return_the_value(con, monkeypatch):
    client = TestClient(webapp.app)

    r = client.put("/api/credentials/ft", json={"value": FT_COOKIES})
    assert r.json()["set"] is True
    assert FT_SECRET not in r.text
    r = client.put("/api/credentials/x", json={"value": X_SECRET})
    assert r.json() == {"ok": True, "site": "x", "set": True}
    assert X_SECRET not in r.text

    r = client.get("/api/sources")
    assert FT_SECRET not in r.text and X_SECRET not in r.text
    rows = {c["site"]: c for c in r.json()["credentials"]}
    assert set(rows) == set(credentials.SITES)
    assert set(rows["ft"]) == {"site", "label", "kind", "set", "updated_at",
                               "checked_at", "check_ok", "check_message", "help",
                               "test_link"}
    assert rows["ft"]["set"] is True and rows["ft"]["kind"] == "cookies"
    # only Substack's Test runs against a pasted link (a paid post the account
    # subscribes to); the row says so with the link box's placeholder
    assert rows["substack"]["test_link"] == "Link to a paid post you subscribe to"
    assert rows["ft"]["test_link"] is None and rows["x"]["test_link"] is None
    assert rows["x"]["set"] is True and rows["x"]["kind"] == "bearer"
    assert rows["wsj"]["set"] is False
    assert rows["ft"]["label"] == "Financial Times"

    # unknown sites and unreadable values are refused
    assert client.put("/api/credentials/bloomberg",
                      json={"value": "x"}).status_code == 400
    assert client.put("/api/credentials/ft", json={"value": "   "}).status_code == 400
    assert client.put("/api/credentials/x",
                      json={"value": "two words"}).status_code == 400
    assert client.delete("/api/credentials/bloomberg").status_code == 400

    r = client.delete("/api/credentials/ft")
    assert r.json() == {"ok": True}
    assert client.delete("/api/credentials/x").json() == {"ok": True}
    body = client.get("/api/sources").json()
    assert {c["site"] for c in body["credentials"] if c["set"]} == set()


def test_api_credential_test_records_the_check(con, monkeypatch):
    client = TestClient(webapp.app)
    client.put("/api/credentials/wsj", json={"value": "wsjsession=abc123"})

    monkeypatch.setattr(probes, "run",
                        lambda con_, site, url=None: (True, "Signed in."))
    r = client.post("/api/credentials/wsj/test")
    assert r.json() == {"ok": True, "message": "Signed in."}
    row = con.execute("select * from credential where site='wsj'").fetchone()
    assert row["check_ok"] is True
    assert row["check_message"] == "Signed in."
    assert row["checked_at"] is not None

    monkeypatch.setattr(probes, "run",
                        lambda con_, site, url=None: (False, "Sign-in failed: HTTP 403."))
    assert client.post("/api/credentials/wsj/test").json() == {
        "ok": False, "message": "Sign-in failed: HTTP 403."}
    listed = {c["site"]: c for c in client.get("/api/sources").json()["credentials"]}
    assert listed["wsj"]["check_ok"] is False
    assert listed["wsj"]["check_message"] == "Sign-in failed: HTTP 403."
    assert client.post("/api/credentials/bloomberg/test").status_code == 400

    client.delete("/api/credentials/wsj")


def test_api_credential_test_passes_the_link_and_records_nothing_for_a_bad_one(
        con, monkeypatch):
    client = TestClient(webapp.app)
    client.put("/api/credentials/substack", json={"value": "sid-for-the-test"})
    seen = []

    def fake_run(con_, site, url=None):
        seen.append((site, url))
        if not url:
            raise probes.BadLink(probes.NEEDS_LINK)
        return True, "Signed in."

    monkeypatch.setattr(probes, "run", fake_run)
    # no link: 400 with the reason, and the row keeps its unchecked state
    r = client.post("/api/credentials/substack/test")
    assert r.status_code == 400
    assert r.json()["detail"] == "Paste a link to a paid post you subscribe to."
    r = client.post("/api/credentials/substack/test", json={"url": ""})
    assert r.status_code == 400
    row = con.execute("select * from credential where site='substack'").fetchone()
    assert row["check_ok"] is None and row["checked_at"] is None
    # the link goes to the probe as pasted
    r = client.post("/api/credentials/substack/test",
                    json={"url": "https://letter.example.com/p/the-paid-post"})
    assert r.json() == {"ok": True, "message": "Signed in."}
    assert seen[-1] == ("substack", "https://letter.example.com/p/the-paid-post")
    row = con.execute("select * from credential where site='substack'").fetchone()
    assert row["check_ok"] is True
    # the other sites' probes still run without a body
    client.put("/api/credentials/x", json={"value": X_SECRET})
    monkeypatch.setattr(probes, "run", lambda con_, site, url=None: (True, "Signed in."))
    assert client.post("/api/credentials/x/test").json()["ok"] is True
    client.delete("/api/credentials/substack")
    client.delete("/api/credentials/x")


def test_probe_without_a_credential_says_so(con):
    # no credential stored -> the probe short-circuits, so this never fetches
    assert probes.run(con, "ft") == (False, "Nothing is saved for this sign-in yet.")
    assert probes.run(con, "bloomberg")[0] is False


def test_ft_probe_reads_the_article_body(con, monkeypatch):
    credentials.set(con, "ft", FT_COOKIES)
    feed = ("<rss><channel><title>FT home</title><item>"
            "<title>Chips and the grid</title>"
            "<link>https://www.ft.com/content/aaaa-bbbb</link>"
            "<pubDate>Thu, 14 Aug 2026 09:00:00 GMT</pubDate>"
            "</item></channel></rss>")

    def fake_get(url, **kw):
        return FakeResponse(feed if "rss/home" in url
                            else article_html("probe reading"))

    monkeypatch.setattr(fetch, "get", fake_get)
    assert probes.run(con, "ft") == (True, "Signed in.")

    # the wall raises, and the message stays short and plain
    def walled(url, **kw):
        if "rss/home" in url:
            return FakeResponse(feed)
        raise fetch.SignInNeeded("ft", "HTTP 403")

    monkeypatch.setattr(fetch, "get", walled)
    ok, message = probes.run(con, "ft")
    assert ok is False
    assert message == "Sign-in failed: HTTP 403."
    con.rollback()


def test_ft_probe_tells_a_live_session_from_a_bot_wall(con, monkeypatch):
    # FT's session service says the cookie is fine, but ft.com's edge walls
    # the article fetch: the message must not blame the cookie
    credentials.set(con, "ft", "FTSession_s=live-token; FTSession=abc")
    feed = ("<rss><channel><title>FT home</title><item>"
            "<title>Chips and the grid</title>"
            "<link>https://www.ft.com/content/aaaa-bbbb</link>"
            "</item></channel></rss>")

    def get(url, **kw):
        if url.startswith(probes.FT_SESSION):
            assert url.endswith("/live-token")
            return FakeResponse('{"UUID": "u1"}')
        if "rss/home" in url:
            return FakeResponse(feed)
        raise fetch.SignInNeeded("ft", "HTTP 403")

    monkeypatch.setattr(fetch, "get", get)
    ok, message = probes.run(con, "ft")
    assert ok is False
    assert message.startswith("Signed in, but the article page came back behind Financial Times")

    # and an invalid session is reported as such, without touching ft.com
    def get2(url, **kw):
        if url.startswith(probes.FT_SESSION):
            return FakeResponse("Session live-token is not valid.", status=404)
        raise AssertionError("must not fetch anything else")

    monkeypatch.setattr(fetch, "get", get2)
    ok, message = probes.run(con, "ft")
    assert ok is False and "no longer valid" in message
    con.rollback()


def test_wsj_probe_skips_a_live_blog_for_a_real_article(con, monkeypatch):
    # the WSJ markets feed leads with a `/livecoverage/` live blog, which has
    # no article body and renders a subscribe overlay however good the sign-in
    # is. The probe must step past it to a real article and read that.
    credentials.set(con, "wsj", "DJSESSION=live-session")
    feed = ("<rss><channel><title>WSJ markets</title>"
            "<item><title>Stocks today</title>"
            "<link>https://www.wsj.com/livecoverage/stock-market-08-20</link>"
            "</item>"
            "<item><title>Real article</title>"
            "<link>https://www.wsj.com/articles/a-real-one</link>"
            "</item></channel></rss>")
    teaser = ("<html><head><title>Live</title></head><body><article>"
              "<p>Stocks rose. Subscribe Now to continue.</p>"
              "</article></body></html>")
    seen = []

    def fake_get(url, **kw):
        seen.append(url)
        if "dowjones.io" in url:
            return FakeResponse(feed)
        if "/livecoverage/" in url:
            return FakeResponse(teaser)
        return FakeResponse(article_html("wsj markets"))

    monkeypatch.setattr(fetch, "get", fake_get)
    assert probes.run(con, "wsj") == (True, "Signed in.")
    # it reached the real article, not the live blog
    assert any("/articles/a-real-one" in u for u in seen)
    con.rollback()


def test_wsj_probe_reports_a_teaser_when_nothing_unlocks(con, monkeypatch):
    # only a live blog on offer, and it comes back as the teaser: an honest
    # "no text" rather than a wall (nothing raised SignInNeeded)
    credentials.set(con, "wsj", "DJSESSION=stale")
    feed = ("<rss><channel><title>WSJ markets</title>"
            "<item><title>Stocks today</title>"
            "<link>https://www.wsj.com/livecoverage/stock-market-08-20</link>"
            "</item></channel></rss>")
    teaser = ("<html><head><title>Live</title></head><body><article>"
              "<p>Stocks rose.</p></article></body></html>")

    def fake_get(url, **kw):
        return FakeResponse(feed if "dowjones.io" in url else teaser)

    monkeypatch.setattr(fetch, "get", fake_get)
    ok, message = probes.run(con, "wsj")
    assert ok is False
    assert message == "Sign-in failed: the article came back without its text."
    con.rollback()


# ---------------------------------------------------------------- substack

FULL_POST = "<p>" + ("Every word of the paid post, for subscribers. " * 60) + "</p>"
PAID_POST = "https://letter.example.com/p/the-paid-post"


def substack_json(body_html, audience="only_paid", wordcount=None):
    data = {"title": "The paid post", "post_date": "2026-08-12T09:00:00Z",
            "audience": audience, "body_html": body_html}
    if wordcount is not None:
        data["wordcount"] = wordcount
    return json.dumps(data)


def substack_net(post_answer, anonymous_answer=None, reader_answer=None,
                 calls=None, handshake="ok"):
    """fetch.get for the Substack probe: the post API with the cookie
    (`site="substack"`) and without it (`site=None`; the same answer unless
    `anonymous_answer` says otherwise), the reader API, the router's
    Substack detection for a custom domain (`x-cluster` on /feed), and the
    cross-domain sign-in a custom domain takes (substack_session.py):
    `handshake="ok"` mints a connect.sid for the host, "dead" answers
    substack.com's sign-in page (the sid no longer signs in). `calls`
    collects (url, site) for every fetch; the probe swallows exceptions from
    the reader fetch, so a test that wants to know the reader API was not
    asked must look there rather than raise from here."""
    def get(url, **kw):
        site = kw.get("site")
        if calls is not None:
            calls.append((url, site))
        if "/api/v1/posts/" in url:
            answer = post_answer
            if site is None and anonymous_answer is not None:
                answer = anonymous_answer
            if isinstance(answer, Exception):
                raise answer
            return answer
        if url.startswith(probes.READER_API):
            return reader_answer or FakeResponse("busy", status=503)
        if url.endswith("/feed"):
            r = FakeResponse("<rss/>")
            r.headers = {"content-type": "application/xml", "x-cluster": "substack"}
            return r
        host = urlsplit(url).hostname
        if url.endswith("/account/login?redirect=%2F"):
            return FakeResponse("", status=301, headers={
                "location": "https://substack.com/sign-in?redirect=%2F"
                            f"&for_pub={host.split('.')[0]}&change_user=false"})
        if url.startswith(substack_session.SIGN_IN + "?"):
            if handshake != "ok":
                return FakeResponse("<html>Sign in</html>")
            pub = parse_qs(urlsplit(url).query)["redirect"][0]
            assert kw.get("cookies", {}).get("substack.sid"), "the sid goes with the sign-in"
            return FakeResponse("", status=303, headers={
                "location": f"{pub}api/v1/sign-in/local/complete?token=t0k&redirect={pub}"})
        if "/api/v1/sign-in/local/complete" in url:
            return FakeResponse("", status=303, headers={"location": f"https://{host}/"},
                                cookies={"connect.sid": f"s%3Asession-for-{host}"})
        raise AssertionError(f"unexpected fetch {url}")
    return get


def handshake_calls(calls):
    return [u for u, _site in calls if "/sign-in" in u or "/account/login" in u]


def reader_calls(calls):
    return [u for u, _site in calls if u.startswith(probes.READER_API)]


def post_calls(calls):
    """(site) of each post API fetch, in order: "substack" with the cookie,
    None without it."""
    return [site for u, site in calls if "/api/v1/posts/" in u]


def test_substack_probe_needs_a_link(con):
    credentials.set(con, "substack", "sid-value")
    with pytest.raises(probes.BadLink) as e:
        probes.run(con, "substack")
    assert str(e.value) == "Paste a link to a paid post you subscribe to."
    with pytest.raises(probes.BadLink):
        probes.run(con, "substack", url="   ")
    con.rollback()


def test_substack_post_ref_reads_pasted_links(con, monkeypatch):
    # a publication host needs no network; a share link is rewritten onto it;
    # a bare host gets https; a link with no post in it is refused
    assert probes.substack_post_ref("https://letter.substack.com/p/the-post?r=abc") == \
        ("https://letter.substack.com", "the-post")
    assert probes.substack_post_ref("letter.substack.com/p/the-post/") == \
        ("https://letter.substack.com", "the-post")
    assert probes.substack_post_ref(
        "https://open.substack.com/pub/letter/p/the-post?r=abc&utm_medium=ios") == \
        ("https://letter.substack.com", "the-post")
    # the tails Substack hangs off a post (comments, one comment) are not
    # part of the slug
    assert probes.substack_post_ref("https://letter.substack.com/p/the-post/comments") == \
        ("https://letter.substack.com", "the-post")
    assert probes.substack_post_ref("https://letter.substack.com/p/the-post/comment/123") == \
        ("https://letter.substack.com", "the-post")
    for bad in ("https://letter.substack.com", "https://letter.substack.com/archive",
                "https://substack.com/home/post/p-123", "not a link", "mailto:x@y.z",
                # pastes urlsplit itself refuses must read as "not a post" too,
                # not surface as a Python error in a recorded check
                "https://[::1/p/abc", "https://letter／substack.com/p/x",
                "http://a[b]c/p/x",
                # ... and so must a port the fetch guard refuses, and a host
                # nothing public can be behind (no fetch happens for these)
                "https://letter.substack.com:99999/p/the-post",
                "https://letter.substack.com:abc/p/the-post",
                "http://127.0.0.1/p/x", "https://localhost/p/x"):
        with pytest.raises(probes.BadLink) as e:
            probes.substack_post_ref(bad)
        assert str(e.value).startswith("That is not a link to a Substack post.")
    # a custom domain is recognised the router's way (x-cluster on /feed)
    monkeypatch.setattr(fetch, "get", substack_net(FakeResponse("{}")))
    assert probes.substack_post_ref("https://www.letter.news/p/the-post", con=con) == \
        ("https://www.letter.news", "the-post")
    # ... a site that is not a Substack at all is refused ...
    def not_substack(url, **kw):
        return FakeResponse("<html>plain</html>", status=200)
    monkeypatch.setattr(fetch, "get", not_substack)
    with pytest.raises(probes.BadLink) as e:
        probes.substack_post_ref("https://www.example.com/p/the-post", con=con)
    assert str(e.value).startswith("That is not a link to a Substack post.")
    # ... and a host that did not answer is reported as that, not as "not a
    # Substack": the analyst's link may well be right
    def down(url, **kw):
        raise ConnectionError("no route to host")
    monkeypatch.setattr(fetch, "get", down)
    with pytest.raises(probes.BadLink) as e:
        probes.substack_post_ref("https://www.letter.news/p/the-post", con=con)
    assert str(e.value) == ("Could not reach www.letter.news to check the link. "
                            "Try again in a moment.")


def test_substack_probe_signs_in_to_the_custom_domain(con, monkeypatch):
    """letter.example.com is a publication on its own domain: the pasted sid
    does not sign in there, so the probe's signed fetch first mints the host's
    own session through Substack's cross-domain sign-in (substack_session.py)
    and keeps it; the verdicts then read the post as before."""
    credentials.set(con, "substack", "sid-value")
    preview = FakeResponse(substack_json("<p>Just the preview.</p>", wordcount=400))
    calls = []
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(FULL_POST, wordcount=400)),
        anonymous_answer=preview, calls=calls))
    assert probes.run(con, "substack", url=PAID_POST) == (True, "Signed in.")
    assert len(handshake_calls(calls)) == 3          # login redirect, sign-in, complete
    assert post_calls(calls) == ["substack", None]
    assert credentials.session_jar(con, "substack", "letter.example.com") == \
        {"connect.sid": "s%3Asession-for-letter.example.com"}
    # the session is kept: the next probe (and every poll) reads the table
    calls.clear()
    assert probes.run(con, "substack", url=PAID_POST) == (True, "Signed in.")
    assert handshake_calls(calls) == []
    # substack.com still signs in but the publication host did not take the
    # hand-over: the verdict is about the handshake, not the subscription
    credentials.set(con, "substack", "sid-value")     # drops the stored session
    monkeypatch.setattr(fetch, "get", substack_net(
        preview, reader_answer=FakeResponse('{"posts": []}'), handshake="dead"))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False
    assert message == ("Signed in on substack.com, but letter.example.com did not "
                       "accept the sign-in this time. Try again in a moment.")
    # the failure is on record (polling backs off for a while), but Test is a
    # human asking: pressing it again runs the hand-over at once
    assert credentials.session_jar(con, "substack", "letter.example.com") == {}
    calls.clear()
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(FULL_POST, wordcount=400)),
        anonymous_answer=preview, calls=calls))
    assert probes.run(con, "substack", url=PAID_POST) == (True, "Signed in.")
    assert len(handshake_calls(calls)) == 3
    # ... and a dead sid is reported as such whatever the host said
    credentials.set(con, "substack", "sid-value")
    monkeypatch.setattr(fetch, "get", substack_net(
        preview, handshake="dead",
        reader_answer=FakeResponse('{"errors":[{"msg":"Please sign in"}]}', status=401)))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False and "no longer valid" in message
    # a publication on substack.com takes the sid as is: no handshake at all
    credentials.set(con, "substack", "sid-value")
    calls.clear()
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(FULL_POST, wordcount=400)),
        anonymous_answer=preview, calls=calls))
    assert probes.run(con, "substack",
                      url="https://letter.substack.com/p/the-paid-post") == (True, "Signed in.")
    assert handshake_calls(calls) == []


def test_substack_probe_reads_the_pasted_paid_post(con, monkeypatch):
    credentials.set(con, "substack", "sid-value")
    preview = FakeResponse(substack_json("<p>Just the preview.</p>", wordcount=400))
    # the cookie unlocks text an anonymous reader does not get: signed in, and
    # the reader API is never asked
    calls = []
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(FULL_POST, wordcount=400)),
        anonymous_answer=preview, calls=calls))
    assert probes.run(con, "substack", url=PAID_POST) == (True, "Signed in.")
    assert post_calls(calls) == ["substack", None]
    assert reader_calls(calls) == []
    # the proof is the difference, not the post's own word count: a whole post
    # whose body reads short of Substack's count (headings, captions and code
    # are counted; a quirk in the count) still passes when the cookie unlocked
    # it, and so does a post whose paywalled tail is a few words
    heavy = ("<h2>" + ("Heading words here. " * 60) + "</h2>"
             "<p>" + ("Paragraph words here. " * 60) + "</p>"
             "<figure><figcaption>" + ("Caption words. " * 60) + "</figcaption></figure>"
             "<pre>" + ("code words " * 60) + "</pre>")
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(heavy, wordcount=900)),
        anonymous_answer=preview))
    assert probes.run(con, "substack", url=PAID_POST) == (True, "Signed in.")
    almost_free = "<p>" + ("Free words for everyone. " * 500) + "</p>"
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(almost_free + "<p>The paid link.</p>", wordcount=2003)),
        anonymous_answer=FakeResponse(substack_json(almost_free, wordcount=2003))))
    assert probes.run(con, "substack", url=PAID_POST) == (True, "Signed in.")

    # the cookie unlocked nothing and the body is cut short of the post's
    # count: a preview. With a live session the account does not subscribe here
    calls = []
    monkeypatch.setattr(fetch, "get", substack_net(
        preview, reader_answer=FakeResponse('{"posts": []}'), calls=calls))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False
    assert message == ("Signed in, but letter.example.com still sent the preview: "
                       "this account has no paid subscription there. Test with a "
                       "post from a publication it pays for.")
    assert post_calls(calls) == ["substack", None] and len(reader_calls(calls)) == 1

    # a preview and the reader API says 401: the session is dead
    monkeypatch.setattr(fetch, "get", substack_net(
        preview,
        reader_answer=FakeResponse('{"errors":[{"msg":"Please sign in"}]}', status=401)))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False and "no longer valid" in message
    assert message.startswith("Sign-in failed:") and message.endswith("substack.sid.")
    # ... and so is a dead session on a post Substack hands out whole (no
    # difference, the reader API says dead): the session verdict comes first
    whole = FakeResponse(substack_json("<p>Ask your questions below.</p>", wordcount=4))
    monkeypatch.setattr(fetch, "get", substack_net(
        whole,
        reader_answer=FakeResponse('{"errors":[{"msg":"Please sign in"}]}', status=401)))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False and "no longer valid" in message

    # a preview and the reader API did not say (503, or a 403 edge block that
    # is no verdict on the cookie): a plain failure
    for status in (503, 403):
        monkeypatch.setattr(fetch, "get", substack_net(
            preview, reader_answer=FakeResponse("<html>busy</html>", status=status)))
        assert probes.run(con, "substack", url=PAID_POST) == \
            (False, "Sign-in failed: the post came back as a preview.")

    # a long preview is still a preview: the post JSON's wordcount is the
    # whole post's, and the body falls well short of it
    long_preview = "<p>" + ("A generous free preview of the paid post. " * 400) + "</p>"
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(long_preview, wordcount=10_000)),
        reader_answer=FakeResponse('{"posts": []}')))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False and message.startswith("Signed in, but")

    # the anonymous fetch is the comparison: when it fails nothing was learned
    # and nothing is recorded
    def flaky(url, **kw):
        if "/api/v1/posts/" in url and kw.get("site") is None:
            raise ConnectionError("reset")
        return substack_net(FakeResponse(substack_json(FULL_POST, wordcount=400)))(url, **kw)
    monkeypatch.setattr(fetch, "get", flaky)
    with pytest.raises(probes.BadLink) as e:
        probes.run(con, "substack", url=PAID_POST)
    assert str(e.value) == ("Could not reach letter.example.com to check the link. "
                            "Try again in a moment.")
    con.rollback()


def test_substack_probe_refuses_links_that_prove_nothing(con, monkeypatch):
    credentials.set(con, "substack", "sid-value")
    # a free post reads the same with or without the sign-in, and is refused
    # before a second fetch
    calls = []
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(FULL_POST, audience="everyone", wordcount=400)),
        calls=calls))
    with pytest.raises(probes.BadLink) as e:
        probes.run(con, "substack", url=PAID_POST)
    assert str(e.value).startswith("That post is free to read")
    assert post_calls(calls) == ["substack"] and reader_calls(calls) == []
    # a paid post Substack hands out whole (a discussion thread whose paid part
    # is the comments; a post with its paywalled tail missing from the count)
    # proves nothing either, whether the session is live or unknown, and the
    # message says what to paste instead
    for reader in (FakeResponse('{"posts": []}'), FakeResponse("busy", status=503)):
        monkeypatch.setattr(fetch, "get", substack_net(
            FakeResponse(substack_json("<p>Ask your questions below.</p>", wordcount=4)),
            reader_answer=reader))
        with pytest.raises(probes.BadLink) as e:
            probes.run(con, "substack", url=PAID_POST)
        assert str(e.value) == ("That post reads the same without the sign-in, so it "
                                "cannot show whether the sign-in works. Paste a link "
                                "to a post with text behind the paywall.")
    # the same with a whole body and no word count at all
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse(substack_json(FULL_POST)), reader_answer=FakeResponse('{"posts": []}')))
    with pytest.raises(probes.BadLink) as e:
        probes.run(con, "substack", url=PAID_POST)
    assert str(e.value).startswith("That post reads the same without the sign-in")
    # nothing at that address
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse('{"error":"Post not found"}', status=404)))
    with pytest.raises(probes.BadLink) as e:
        probes.run(con, "substack", url=PAID_POST)
    assert str(e.value) == "Substack has no post at that link. Check the link and try again."
    # any other trouble is a plain failure, still without the reader API
    monkeypatch.setattr(fetch, "get", substack_net(FakeResponse("busy", status=503)))
    assert probes.run(con, "substack", url=PAID_POST) == \
        (False, "Sign-in failed: HTTP 503.")
    monkeypatch.setattr(fetch, "get", substack_net(
        FakeResponse("<html>not json</html>", status=200)))
    ok, message = probes.run(con, "substack", url=PAID_POST)
    assert ok is False and message.startswith("Sign-in failed:")
    con.rollback()


# ---------------------------------------------------------------- listing


def test_api_sources_shape(con, monkeypatch):
    client = TestClient(webapp.app)
    con.execute("insert into watchlist (ticker, sector, active, added_by, name, "
                "industry, exchange, country, cik, resolver) values "
                "('NVDA', 'Technology', true, 'web', 'NVIDIA Corporation', "
                "'Semiconductors', 'NasdaqGS', 'United States', 1045810, "
                "'yfinance') on conflict (ticker) do update set "
                "name=excluded.name, industry=excluded.industry, "
                "exchange=excluded.exchange, country=excluded.country, "
                "cik=excluded.cik, resolver=excluded.resolver, active=true")
    con.execute("insert into source (name, connector, url, config, status, "
                "added_by) values ('podcast:shapecast', 'podcast', "
                "'https://shape.example.com/pod.xml', '{}', 'active', 'web'), "
                "('@shapeuser', 'x', null, "
                "'{\"site\": \"x\", \"username\": \"shapeuser\"}', 'active', "
                "'web'), ('Telegram: shapechan', 'bridge', "
                "'/?action=display&bridge=TelegramBridge', "
                "'{\"bridge\": {\"name\": \"TelegramBridge\", "
                "\"params\": {\"username\": \"shapechan\"}}}', 'active', 'web')")
    con.commit()

    body = client.get("/api/sources").json()
    assert set(body) == {"watchlist", "sources", "links", "credentials"}

    watch = {w["ticker"]: w for w in body["watchlist"]}["NVDA"]
    assert set(watch) == {"ticker", "name", "sector", "industry", "exchange",
                          "country", "active", "events", "cik"}
    assert watch["name"] == "NVIDIA Corporation"
    assert watch["exchange"] == "NasdaqGS"
    assert watch["cik"] == 1045810

    sources = {s["name"]: s for s in body["sources"]}
    assert sources["podcast:shapecast"]["label"] == "Podcast"
    assert sources["@shapeuser"]["label"] == "X account"
    assert sources["@shapeuser"]["site"] == "x"
    assert sources["@shapeuser"]["feed_url"] is None
    assert sources["@shapeuser"]["credential"] == {"site": "x", "set": False}
    assert sources["Telegram: shapechan"]["label"] == "Telegram"
    # internal buckets never show on the card
    assert not [n for n in sources if n.startswith(("link:", "edgar:"))]
    assert "manual:uploads" not in sources

    assert isinstance(body["links"], list)
    assert len(body["links"]) <= 30


def test_api_status_still_serves_sources(con):
    client = TestClient(webapp.app)
    con.execute("insert into source (name, connector, url, config, status, "
                "last_error) values ('statuscheck-feed', 'rss', "
                "'https://statuscheck.example.com/rss', "
                "'{\"site\": \"ft\"}', 'active', 'Sign-in needed')")
    con.commit()

    body = client.get("/api/status").json()
    row = {s["name"]: s for s in body["sources"]}["statuscheck-feed"]
    assert row["label"] == "FT feed"
    assert row["last_error"] == "Sign-in needed"
    assert set(row) == {"source_id", "name", "connector", "label", "status",
                        "last_polled", "last_error", "events"}
    assert isinstance(body["counts"]["claims"], int)


def test_source_label_covers_every_connector():
    assert webapp.source_label("rss", {"site": "ft"}) == "FT feed"
    assert webapp.source_label("rss", {"site": "wsj"}) == "WSJ feed"
    assert webapp.source_label(
        "rss", {"substack": {"origin": "https://x.substack.com"}}) == "Substack"
    assert webapp.source_label("rss", None) == "News feed"
    assert webapp.source_label("podcast", {}) == "Podcast"
    assert webapp.source_label("youtube", {"channel_id": "UC1"}) == "YouTube channel"
    assert webapp.source_label("youtube", {"playlist_id": "PL1"}) == "YouTube playlist"
    assert webapp.source_label("x", {"username": "a"}) == "X account"
    assert webapp.source_label(
        "bridge", {"bridge": {"name": "RedditBridge"}}) == "Reddit"
    assert webapp.source_label("link", {}) == "Links"
    assert webapp.source_label("manual", {}) == "Uploads"
    assert webapp.source_label("edgar", {}) == "Filings"


def test_watchlist_search_endpoint(con, monkeypatch):
    import graph.watchlist as watchlist_mod
    client = TestClient(webapp.app)
    quotes = [{"symbol": "NVDA", "name": "NVIDIA Corporation",
               "exchange": "NasdaqGS", "type": "EQUITY"}]
    monkeypatch.setattr(watchlist_mod, "search", lambda q, limit=8: quotes)

    assert client.get("/api/watchlist/search", params={"q": "nvid"}).json() == quotes
    # under two characters the box answers empty without asking Yahoo
    assert client.get("/api/watchlist/search", params={"q": "n"}).json() == []
    assert client.get("/api/watchlist/search").json() == []


def test_removed_feeds_endpoint_is_gone():
    client = TestClient(webapp.app)
    r = client.post("/api/feeds", json={"name": "x", "url": "https://x.example",
                                        "kind": "rss"})
    assert r.status_code in (404, 405)


# ---------------------------------------------------------------- CLI


def test_cli_add_source_and_credential(con, monkeypatch):
    """`graph add-source` and `graph set-credential` take the same path as the
    API (build spec v4 §7)."""
    url = "https://cli.example.com/atom.xml"
    fixed_router(monkeypatch, feed_resolution(url, "CLI feed"))
    cli.cmd_add_source(argparse.Namespace(url=url, name=None))
    row = con.execute("select * from source where url=%s", (url,)).fetchone()
    assert row["connector"] == "rss"
    assert row["name"] == "CLI feed"
    assert row["added_by"] == "cli"
    assert row["status"] == "active"

    # a second add reports the duplicate instead of writing a second row
    cli.cmd_add_source(argparse.Namespace(url=url, name=None))
    assert con.execute("select count(*) n from source where url=%s",
                       (url,)).fetchone()["n"] == 1

    # --name wins over the router's suggestion
    other = "https://cli2.example.com/atom.xml"
    fixed_router(monkeypatch, feed_resolution(other, "Suggested"))
    cli.cmd_add_source(argparse.Namespace(url=other, name="My name"))
    assert con.execute("select name from source where url=%s",
                       (other,)).fetchone()["name"] == "My name"

    cli.cmd_set_credential(argparse.Namespace(
        site="substack", file=None, value="sid-value-from-the-cli", note=None))
    assert credentials.is_set(con, "substack") is True
    credentials.delete(con, "substack")
    con.commit()


def test_cli_add_ticker(con, monkeypatch):
    import graph.watchlist as watchlist_mod
    monkeypatch.setattr(watchlist_mod, "resolve", lambda c, t: {
        "ticker": "AMD", "name": "Advanced Micro Devices, Inc.",
        "sector": "Technology", "industry": "Semiconductors",
        "exchange": "NasdaqGS", "country": "United States", "currency": "USD",
        "quote_type": "EQUITY", "website": None, "resolver": "yfinance",
        "cik": 2488} if t == "AMD" else None)

    cli.cmd_add_ticker(argparse.Namespace(ticker="amd", sector=None))
    row = con.execute("select * from watchlist where ticker='AMD'").fetchone()
    assert row["name"] == "Advanced Micro Devices, Inc."
    assert row["exchange"] == "NasdaqGS"
    assert row["added_by"] == "cli"
    assert row["resolver"] == "yfinance"

    # an unknown ticker prints an error and writes nothing
    cli.cmd_add_ticker(argparse.Namespace(ticker="ZZZZ8", sector=None))
    assert con.execute("select 1 from watchlist where ticker='ZZZZ8'"
                       ).fetchone() is None


def test_links_queue_survives_a_failing_handler(con, monkeypatch):
    """A handler that blows up leaves the row failed with the reason, and the
    queue keeps draining (build spec v4 §6.3)."""
    client = TestClient(webapp.app)
    url = "https://broken.example.com/story"
    fixed_router(monkeypatch, link_resolution(url))

    def boom(u, **kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fetch, "get", boom)
    link = client.post("/api/sources", json={"url": url}).json()["link"]
    assert link["status"] == "failed"
    assert "connection reset" in link["error"]

    row = con.execute("select attempts from link_queue where link_id=%s",
                      (link["link_id"],)).fetchone()
    assert row["attempts"] == 1
    monkeypatch.setattr(fetch, "get",
                        lambda u, **kw: FakeResponse(article_html("broken wire")))
    assert links.process(con, limit=50)["done"] >= 1
    con.commit()
