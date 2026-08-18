"""Sources API (build spec v4 §7): the URL box, one-off links, sign-ins.

Nothing here touches the network. The router is monkeypatched with the
Resolution its own tests already pin (tests/test_router.py), `graph.fetch.get`
is faked for the link queue, and the credential probes are faked or short-
circuited by having no credential stored. Every response body is checked
against the pasted secret: a credential value must never leave the API.
"""
import argparse
import uuid

from fastapi.testclient import TestClient

from graph import cli, credentials, fetch, probes, router, webapp
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

    def __init__(self, text, status=200):
        self.text = text
        self.status = status
        self.bytes = text.encode("utf-8")
        self.headers = {}

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
                               "checked_at", "check_ok", "check_message", "help"}
    assert rows["ft"]["set"] is True and rows["ft"]["kind"] == "cookies"
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

    monkeypatch.setattr(probes, "run", lambda con_, site: (True, "Signed in."))
    r = client.post("/api/credentials/wsj/test")
    assert r.json() == {"ok": True, "message": "Signed in."}
    row = con.execute("select * from credential where site='wsj'").fetchone()
    assert row["check_ok"] is True
    assert row["check_message"] == "Signed in."
    assert row["checked_at"] is not None

    monkeypatch.setattr(probes, "run",
                        lambda con_, site: (False, "Sign-in failed: HTTP 403."))
    assert client.post("/api/credentials/wsj/test").json() == {
        "ok": False, "message": "Sign-in failed: HTTP 403."}
    listed = {c["site"]: c for c in client.get("/api/sources").json()["credentials"]}
    assert listed["wsj"]["check_ok"] is False
    assert listed["wsj"]["check_message"] == "Sign-in failed: HTTP 403."
    assert client.post("/api/credentials/bloomberg/test").status_code == 400

    client.delete("/api/credentials/wsj")


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
    assert message.startswith("Signed in, but Financial Times blocks server fetches")

    # and an invalid session is reported as such, without touching ft.com
    def get2(url, **kw):
        if url.startswith(probes.FT_SESSION):
            return FakeResponse("Session live-token is not valid.", status=404)
        raise AssertionError("must not fetch anything else")

    monkeypatch.setattr(fetch, "get", get2)
    ok, message = probes.run(con, "ft")
    assert ok is False and "no longer valid" in message
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
