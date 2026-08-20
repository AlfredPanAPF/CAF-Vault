"""Connectors added by build spec v4 §3.3, §5.3, §6.1-6.4: article extraction,
RSS/Atom + premium sites, YouTube transcripts, X, the one-off link queue, and
the rss-bridge poller. Everything network-touching is monkeypatched; no test
here reaches the internet or a real yt-dlp.

    CAF_DB_URL=postgresql:///caf_test_conn uv run pytest tests/test_connectors_v4.py -q
"""
import json
import sys
import types

import pytest
import requests
from psycopg.types.json import Jsonb

from graph import bridge as bridge_client
from graph import credentials, fetch
from graph.connectors import bridge, links, manual, rss, x, youtube


class Resp:
    """Stand-in for graph.fetch.Response."""

    def __init__(self, text, status=200, url=""):
        self.text = text
        self.status = status
        self.url = url
        self.bytes = text.encode("utf-8")
        self.headers = {}

    @property
    def ok(self):
        return 200 <= self.status < 300


class ApiResp:
    """Stand-in for a requests response."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def source_row(con, name):
    return con.execute("select * from source where name=%s", (name,)).fetchone()


def events_of(con, connector):
    return con.execute("select * from event where connector=%s order by fetched_at",
                       (connector,)).fetchall()


# ---------------------------------------------------------------- §3.3 extract

PARA = ("Paragraph {} of the article body, long enough to clear the "
        "forty-character paragraph floor comfortably.")

JSONLD_HTML = """<html><head><title>Wrapper title</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"WebPage","name":"nav"},
  {"@type":"NewsArticle","headline":"Nvidia lifts its guidance",
   "datePublished":"2026-08-01T09:00:00Z",
   "articleBody":"Nvidia raised its outlook for the year.\\n\\nThe company cited data centre demand."}
]}
</script></head><body><p>Cookie banner text that is not the article.</p></body></html>"""

WSJ_HTML = ("<html><head><title>WSJ story</title></head><body>"
            "<div class=\"sidebar\"><p>Sidebar promo paragraph that is long "
            "enough to survive the paragraph floor easily.</p></div>"
            "<section data-type=\"article-body\">"
            + "".join(f"<p>{PARA.format(i)}</p>" for i in range(4))
            + "</section></body></html>")

SUBSTACK_HTML = ("<html><head><title>The newsletter</title></head><body>"
                 "<div class=\"body markup\">"
                 + "".join(f"<p>{PARA.format(i)}</p>" for i in range(4))
                 + "</div></body></html>")

PLAIN_HTML = ("<html><head><title>Plain story</title></head><body><article>"
              + "".join(f"<p>{PARA.format(i)}</p>" for i in range(5))
              + "</article></body></html>")


def test_extract_article_uses_jsonld_body():
    title, published, body = extracted = manual.extract_article(
        JSONLD_HTML, "https://news.example.com/story")
    assert title == "Nvidia lifts its guidance"
    assert published.startswith("2026-08-01")
    assert "Nvidia raised its outlook" in body
    assert "Cookie banner" not in body
    assert len(extracted) == 3


def test_extract_article_uses_site_selectors():
    _title, _published, body = manual.extract_article(
        WSJ_HTML, "https://www.wsj.com/finance/story-abc123")
    assert "Paragraph 0" in body and "Paragraph 3" in body
    assert "Sidebar promo" not in body

    _t, _p, sub_body = manual.extract_article(
        SUBSTACK_HTML, "https://example.substack.com/p/a-post")
    assert "Paragraph 2" in sub_body


def test_extract_article_falls_back_to_the_heuristic():
    title, _published, body = manual.extract_article(
        PLAIN_HTML, "https://blog.example.com/post")
    assert title == "Plain story"
    assert body == manual.extract(PLAIN_HTML)[2]
    assert "Paragraph 4" in body


# ---------------------------------------------------------------- §6.4 rss

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Example Atom</title>
<entry>
  <title>Atom item one</title>
  <link rel="alternate" href="https://atom.example.com/posts/one"/>
  <link rel="enclosure" href="https://atom.example.com/one.mp3" type="audio/mpeg"/>
  <id>tag:atom.example.com,2026:1</id>
  <published>2026-08-11T08:30:00Z</published>
  <content type="html">&lt;p&gt;Atom body paragraph.&lt;/p&gt;</content>
</entry>
</feed>
"""

RSS_WITH_CONTENT = """<?xml version="1.0"?>
<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Paid Letter</title>
<item>
<title>The paid post</title>
<link>https://letter.example.com/p/the-paid-post</link>
<pubDate>Tue, 12 Aug 2026 09:00:00 GMT</pubDate>
<content:encoded><![CDATA[<p>Preview only.</p>]]></content:encoded>
</item>
</channel></rss>
"""

FT_FEED = """<?xml version="1.0"?>
<rss><channel><title>FT home</title>
<item>
<title>Chip demand keeps climbing</title>
<link>https://www.ft.com/content/abc-123</link>
<pubDate>Wed, 13 Aug 2026 07:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


def test_rss_items_parse_atom_entries():
    items = list(rss.items(ATOM_FEED))
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Atom item one"
    assert item["link"] == "https://atom.example.com/posts/one"
    assert item["date"] == "2026-08-11"
    assert "Atom body paragraph" in item["content_html"]
    assert item["enclosure_type"] == "audio/mpeg"
    assert item["uid"] == "tag:atom.example.com,2026:1"


def test_rss_date_of_reads_both_feed_formats():
    assert rss.date_of("Tue, 12 Aug 2026 09:00:00 GMT") == "2026-08-12"
    assert rss.date_of("2026-08-11T08:30:00Z") == "2026-08-11"
    assert rss.date_of("sometime last spring") == ""
    assert rss.date_of("") == ""


def test_rss_site_source_blocked_by_sign_in_wall(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('ft:home', 'rss', 'https://www.ft.com/rss/home', 'active', %s)",
        (Jsonb({"site": "ft"}),))

    def fake_get(url, **kw):
        if "/rss/home" in url:
            return Resp(FT_FEED)
        raise fetch.SignInNeeded("ft", "HTTP 403")

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["blocked"] == 1
    assert out["new"] == 0
    row = source_row(con, "ft:home")
    assert row["last_error"] == "Sign-in needed"
    assert row["last_error_at"] is not None
    assert events_of(con, "rss") == []


FT_TEASER_FEED = """<?xml version="1.0"?>
<rss><channel><title>FT home</title>
<item>
<title>Chip demand keeps climbing</title>
<link>https://www.ft.com/content/abc-123</link>
<description>Orders at the largest foundries rose again in July.</description>
<pubDate>Wed, 13 Aug 2026 07:00:00 GMT</pubDate>
</item>
<item>
<title>Rates hold steady</title>
<link>https://www.ft.com/content/def-456</link>
<description>The committee left policy unchanged, as expected.</description>
<pubDate>Wed, 13 Aug 2026 06:00:00 GMT</pubDate>
</item>
</channel></rss>
"""


def test_rss_ft_wall_keeps_the_feed_teasers(con, monkeypatch):
    """FT and WSJ article pages are walled at the edge for every server, so the
    feed's teaser is the document; the source still reports the wall."""
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('ft:teasers', 'rss', 'https://www.ft.com/rss/home', 'active', %s)",
        (Jsonb({"site": "ft"}),))
    pages = []

    def fake_get(url, **kw):
        if "/rss/home" in url:
            return Resp(FT_TEASER_FEED)
        pages.append(url)
        raise fetch.SignInNeeded("ft", "HTTP 403")

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["new"] == 2 and out["blocked"] == 1 and out["thin"] == 2
    assert len(pages) == 1          # the wall is noted once, not per item
    row = source_row(con, "ft:teasers")
    assert row["last_error"] == "Sign-in needed"
    events = events_of(con, "rss")
    assert [e["meta"]["item_url"] for e in events] == [
        "https://www.ft.com/content/abc-123", "https://www.ft.com/content/def-456"]
    assert all(e["meta"]["thin"] for e in events)
    assert events[0]["meta"]["title"] == "Chip demand keeps climbing"


def test_rss_feed_error_status_is_reported(con, monkeypatch):
    """A premium feed that starts answering 404 says so instead of looking
    freshly polled and healthy."""
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('wsj:gone', 'rss', 'https://feeds.content.dowjones.io/public/rss/Nope', "
        "'active', %s)", (Jsonb({"site": "wsj"}),))
    monkeypatch.setattr(fetch, "get",
                        lambda url, **kw: Resp("<html>not found</html>", status=404))
    out = rss.poll(con)
    assert out["errors"] == 1 and out["new"] == 0
    row = source_row(con, "wsj:gone")
    assert row["last_error"] == "Feed unreachable"
    assert row["last_polled"] is None


def test_rss_item_url_matches_the_link_queue_key(con, monkeypatch):
    """rss stores the router-normalized URL, so an article pasted once and then
    met in its feed is not fetched a second time."""
    feed = ("<rss><channel><title>Strat</title><item>"
            "<title>The AI capex cycle</title>"
            "<link>https://strat.example.com/2026/capex/?utm_source=rss</link>"
            "</item></channel></rss>")
    con.execute(
        "insert into source (name, connector, url, status) values "
        "('Strat', 'rss', 'https://strat.example.com/feed', 'active')")
    monkeypatch.setattr(rss, "fetch_page",
                        lambda url: feed if url.endswith("/feed") else PLAIN_HTML)
    assert rss.poll(con)["new"] == 1
    ev = events_of(con, "rss")[0]
    assert ev["meta"]["item_url"] == "https://strat.example.com/2026/capex"


# ------------------------------------------- §6.4 wsj /news/ listing sources

HOTS_URL = "https://www.wsj.com/news/heard-on-the-street"
HOTS_PAGE = ('<html><head><title>Heard on the Street</title></head><body>'
             '<script id="__NEXT_DATA__" type="application/json">'
             + json.dumps({"props": {"pageProps": {
                 "latestArticles": [
                     {"articleUrl": "https://www.wsj.com/finance/"
                                    "auto-insurance-93243754?mod=hots",
                      "headline": "Auto insurance premiums keep falling",
                      "summary": "The soft market pleases inflation watchers.",
                      "timestamp": "2026-08-18T09:30:00Z"},
                     {"articleUrl": "https://www.wsj.com/video/markets-clip",
                      "headline": "A markets clip", "summary": "",
                      "timestamp": "2026-08-18T10:00:00Z", "isVideo": True}],
                 "moreInArticlesInitial": [
                     {"articleUrl": "https://www.wsj.com/tech/ai/"
                                    "open-weight-ai-523e6410",
                      "headline": "Open-weight AI will not crimp demand",
                      "summary": "The boom's beneficiaries cash in either way.",
                      "timestamp": "2026-08-16T09:30:00Z"},
                     # the same piece appears in both lists on the real page
                     {"articleUrl": "https://www.wsj.com/finance/"
                                    "auto-insurance-93243754?mod=hots",
                      "headline": "Auto insurance premiums keep falling",
                      "summary": "The soft market pleases inflation watchers.",
                      "timestamp": "2026-08-18T09:30:00Z"}],
                 "latestVideos": [
                     {"articleUrl": "https://www.wsj.com/video/v1",
                      "headline": "A video",
                      "timestamp": "2026-08-18T11:00:00Z"}],
                 "whatsNewsData": [
                     {"articleUrl": "https://www.wsj.com/economy/"
                                    "whats-news-abc12345",
                      "headline": "What's News digest",
                      "timestamp": "2026-08-18T12:00:00Z"}]}}})
             + '</script></body></html>')

WSJ_SECTION_ARTICLE = ("<html><head><title>WSJ piece</title></head><body>"
                       "<section data-type=\"article-body\">"
                       + "".join(f"<p>{PARA.format(i)}</p>" for i in range(6))
                       + "</section></body></html>")


def wsj_section_source(con):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('WSJ heard on the street', 'rss', %s, 'active', %s)",
        (HOTS_URL, Jsonb({"site": "wsj", "wsj_section": "heard-on-the-street"})))


def test_wsj_section_items_reads_the_listing_json():
    items = rss.wsj_section_items(HOTS_PAGE)
    # sidebar lists (latestVideos, whatsNewsData), video rows and the piece
    # that appears in both column lists never become items
    assert [i["title"] for i in items] == [
        "Auto insurance premiums keep falling",
        "Open-weight AI will not crimp demand"]
    assert items[0]["date"] == "2026-08-18"
    assert items[0]["link"] == ("https://www.wsj.com/finance/"
                                "auto-insurance-93243754?mod=hots")
    assert items[0]["content_html"] == \
        "The soft market pleases inflation watchers."
    assert rss.wsj_section_items("<html><body><p>Not a listing.</p></body>"
                                 "</html>") == []
    assert rss.wsj_section_items("") == []


def test_rss_wsj_section_polls_the_page_and_fetches_articles(con, monkeypatch):
    wsj_section_source(con)
    listing_kw = {}

    def fake_get(url, **kw):
        if url == HOTS_URL:
            listing_kw.update(kw)
            return Resp(HOTS_PAGE)
        return Resp(WSJ_SECTION_ARTICLE)

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["new"] == 2 and out["thin"] == 0 and out["blocked"] == 0
    # the listing fetch keeps wall detection on and hands the browser path
    # the listing markers in the article body markers' place
    assert listing_kw.get("body_markers") == rss.WSJ_LISTING_MARKERS
    assert "allow_wall" not in listing_kw
    events = events_of(con, "rss")
    # item_url is router-normalized (the ?mod= tracker is gone), so a piece
    # pasted once and met on the page again is one document. Compared as a
    # sorted pairing, not a sequence: both rows share one transaction
    # timestamp, so the events_of order-by-fetched_at tie breaks on physical
    # row order, which other files' committed rows perturb under full-suite
    # ordering.
    assert sorted((e["meta"]["item_url"], e["meta"]["title"])
                  for e in events) == [
        ("https://www.wsj.com/finance/auto-insurance-93243754",
         "Auto insurance premiums keep falling"),
        ("https://www.wsj.com/tech/ai/open-weight-ai-523e6410",
         "Open-weight AI will not crimp demand")]
    assert all(e["meta"]["site"] == "wsj" for e in events)
    row = source_row(con, "WSJ heard on the street")
    assert row["last_error"] is None and row["last_polled"] is not None
    # nothing new on the next cycle: both pieces dedupe on item_url
    assert rss.poll(con)["duplicate"] == 2


def test_rss_wsj_section_wall_keeps_the_page_teasers(con, monkeypatch):
    """Article pages that wall mid-cycle degrade to the listing's own one-line
    summaries, the same honest state a WSJ feed source lands in."""
    wsj_section_source(con)
    credentials.set(con, "wsj", "DJSESSION=stale")

    def fake_get(url, **kw):
        if url == HOTS_URL:
            return Resp(HOTS_PAGE)
        raise fetch.SignInNeeded("wsj", "HTTP 401")

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["new"] == 2 and out["blocked"] == 1 and out["thin"] == 2
    row = source_row(con, "WSJ heard on the street")
    assert row["last_error"] == rss.HEADLINES_ONLY
    events = events_of(con, "rss")
    assert len(events) == 2
    assert all(e["meta"]["thin"] for e in events)


def test_rss_wsj_section_listing_wall_blocks_the_source(con, monkeypatch):
    """The listing itself is walled (no sign-in, or the sidecar is down): the
    source says so and no challenge page is ever parsed for items."""
    wsj_section_source(con)

    def fake_get(url, **kw):
        raise fetch.SignInNeeded("wsj", "HTTP 403")

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["blocked"] == 1 and out["new"] == 0
    row = source_row(con, "WSJ heard on the street")
    assert row["last_error"] == "Sign-in needed"
    assert events_of(con, "rss") == []


def test_rss_wsj_section_without_the_listing_shape_is_reported(con, monkeypatch):
    """A page that answers without the listing JSON (a redesign) is an error
    on the sources page, not a healthy source that never delivers."""
    wsj_section_source(con)
    monkeypatch.setattr(fetch, "get",
                        lambda url, **kw: Resp("<html><body><p>New layout."
                                               "</p></body></html>"))
    out = rss.poll(con)
    assert out["errors"] == 1 and out["new"] == 0
    row = source_row(con, "WSJ heard on the street")
    assert row["last_error"] == "No articles found on the page"
    assert row["last_polled"] is not None


def test_rss_substack_paid_post_uses_the_post_api(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('Paid Letter', 'rss', 'https://letter.example.com/feed', 'active', %s)",
        (Jsonb({"site": "substack",
                "substack": {"origin": "https://letter.example.com"}}),))
    full_body = "<p>" + ("Full post body for subscribers. " * 80) + "</p>"
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if url.endswith("/feed"):
            return Resp(RSS_WITH_CONTENT)
        if "/api/v1/posts/" in url:
            return Resp(json.dumps({"title": "The paid post",
                                    "post_date": "2026-08-12T09:00:00Z",
                                    "audience": "only_paid",
                                    "body_html": full_body}))
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["new"] == 1
    assert "https://letter.example.com/api/v1/posts/the-paid-post" in calls
    ev = events_of(con, "rss")[0]
    assert ev["meta"]["item_url"] == "https://letter.example.com/p/the-paid-post"
    assert ev["meta"]["site"] == "substack"


def test_rss_substack_preview_counts_as_blocked(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('Paid Letter 2', 'rss', 'https://two.example.com/feed', 'active', %s)",
        (Jsonb({"site": "substack",
                "substack": {"origin": "https://two.example.com"}}),))

    def fake_get(url, **kw):
        if url.endswith("/feed"):
            return Resp(RSS_WITH_CONTENT.replace("letter.example.com",
                                                 "two.example.com"))
        return Resp(json.dumps({"title": "The paid post", "audience": "only_paid",
                                "body_html": "<p>Preview only.</p>"}))

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["blocked"] == 1
    assert source_row(con, "Paid Letter 2")["last_error"] == "Sign-in needed"


def test_rss_substack_long_preview_is_still_a_preview(con, monkeypatch):
    """Substack sends the whole post's `wordcount` with the preview: a body
    well short of it is the preview however long it runs (a Pragmatic
    Engineer preview is 4,500 words of a 10,000-word post), and a body that
    reaches it is the post."""
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('Paid Letter 3', 'rss', 'https://three.example.com/feed', 'active', %s)",
        (Jsonb({"site": "substack",
                "substack": {"origin": "https://three.example.com"}}),))
    long_preview = "<p>" + ("A generous free preview of the paid post. " * 400) + "</p>"

    def fake_get(url, **kw):
        if url.endswith("/feed"):
            return Resp(RSS_WITH_CONTENT.replace("letter.example.com",
                                                 "three.example.com"))
        return Resp(json.dumps({"title": "The paid post", "audience": "only_paid",
                                "body_html": long_preview, "wordcount": 10_000}))

    monkeypatch.setattr(fetch, "get", fake_get)
    out = rss.poll(con)
    assert out["blocked"] == 1
    assert source_row(con, "Paid Letter 3")["last_error"] == "Sign-in needed"

    # the same length with a matching wordcount is the post itself; a free
    # post is never a preview; without a wordcount the length rule decides
    words = rss.html_words(long_preview)
    assert rss.substack_preview({"audience": "only_paid", "body_html": long_preview,
                                 "wordcount": words}) is False
    assert rss.substack_preview({"audience": "only_paid", "body_html": long_preview,
                                 "wordcount": words * 2}) is True
    assert rss.substack_preview({"audience": "everyone", "body_html": "<p>Hi</p>",
                                 "wordcount": 5000}) is False
    assert rss.substack_preview({"audience": "only_paid", "body_html": "<p>Hi</p>"}) is True
    assert rss.substack_preview({"audience": "only_paid", "body_html": long_preview,
                                 "wordcount": 0}) is False
    assert rss.substack_preview({"audience": "only_paid", "body_html": long_preview,
                                 "wordcount": "n/a"}) is False
    assert rss.substack_preview({"audience": "only_paid", "body_html": long_preview,
                                 "wordcount": True}) is False
    # the count is over every text node, as Substack's is: a complete post
    # that keeps a third of its words in headings, captions and code must not
    # read as a preview against the paragraphs-only stored body
    heavy = ("<h2>" + ("Heading words here. " * 60) + "</h2>"
             "<p>" + ("Paragraph words here. " * 60) + "</p>"
             "<figure><figcaption>" + ("Caption words. " * 60) + "</figcaption></figure>"
             "<pre>" + ("code words " * 60) + "</pre>"
             "<script>var x = 'not words';</script>")
    assert rss.html_words(heavy) == 180 + 180 + 120 + 120
    assert len(rss.html_to_text(heavy).split()) == 180
    assert rss.substack_preview({"audience": "only_paid", "body_html": heavy,
                                 "wordcount": 600}) is False
    assert rss.substack_preview({"audience": "only_paid", "body_html": heavy,
                                 "wordcount": 1200}) is True
    # and the post slug never carries the comments tail
    assert rss.slug_of("https://l.substack.com/p/the-post/comments") == "the-post"
    assert rss.slug_of("https://l.substack.com/p/the-post/comment/12?x=1") == "the-post"
    assert rss.slug_of("https://l.substack.com/archive") == ""


# ---------------------------------------------------------------- §6.1 youtube

VIDEOS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
<title>Example Channel</title>
<entry>
  <id>yt:video:VID12345678</id>
  <yt:videoId>VID12345678</yt:videoId>
  <title>Nvidia earnings call recap</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=VID12345678"/>
  <author><name>Example Channel</name></author>
  <published>2026-08-10T12:00:00+00:00</published>
</entry>
</feed>
"""

JSON3 = json.dumps({"events": [
    {"segs": [{"utf8": "Nvidia "}, {"utf8": "lifted guidance"}]},
    {"segs": [{"utf8": " again this quarter."}]},
]})

CAPTIONS_INFO = {
    "id": "VID12345678", "title": "Nvidia earnings call recap",
    "channel": "Example Channel", "duration": 754, "upload_date": "20260810",
    "automatic_captions": {"en": [
        {"ext": "vtt", "url": "https://captions.example.com/vtt"},
        {"ext": "json3", "url": "https://captions.example.com/json3"},
    ]},
}


def fake_ytdlp(info=None, error=None):
    """A stand-in yt_dlp module whose YoutubeDL returns `info` (or raises)."""
    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            if error:
                raise error
            return dict(info or {})

    return types.SimpleNamespace(YoutubeDL=FakeYDL)


def add_youtube_source(con, name="YouTube: Example Channel", config=None):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "(%s, 'youtube', 'https://www.youtube.com/channel/UC123', 'active', %s)",
        (name, Jsonb(config or {"channel_id": "UC123"})))


def test_youtube_poll_writes_a_caption_transcript(con, monkeypatch):
    add_youtube_source(con)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_ytdlp(CAPTIONS_INFO))

    def fake_get(url, **kw):
        if "videos.xml" in url:
            return Resp(VIDEOS_XML)
        if "json3" in url:
            return Resp(JSON3)
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(fetch, "get", fake_get)
    out = youtube.poll(con)
    assert out == {"new": 1, "duplicate": 0, "queued": 0, "skipped": 0,
                   "blocked": 0, "errors": 0}
    ev = events_of(con, "youtube")[0]
    assert ev["meta"]["transcript_source"] == "captions"
    assert ev["meta"]["video_id"] == "VID12345678"
    assert ev["meta"]["duration_s"] == 754
    assert ev["published_at"].date().isoformat() == "2026-08-10"

    # second poll dedups on meta video_id
    out2 = youtube.poll(con)
    assert out2["new"] == 0 and out2["duplicate"] == 1


def test_youtube_no_captions_with_asr_off_is_remembered(con, monkeypatch):
    add_youtube_source(con, name="YouTube: Silent Channel")
    monkeypatch.setenv("CAF_ASR", "off")
    monkeypatch.setitem(sys.modules, "yt_dlp",
                        fake_ytdlp({"id": "VID12345678", "duration": 60}))
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp(VIDEOS_XML))

    out = youtube.poll(con)
    assert out["skipped"] == 1 and out["new"] == 0
    assert events_of(con, "youtube") == []
    row = source_row(con, "YouTube: Silent Channel")
    assert row["config"]["skipped"] == ["VID12345678"]

    # the skipped video is not retried next cycle
    out2 = youtube.poll(con)
    assert out2["skipped"] == 0 and out2["new"] == 0


def test_youtube_bot_wall_sets_sign_in_needed(con, monkeypatch):
    add_youtube_source(con, name="YouTube: Walled Channel")
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_ytdlp(
        error=RuntimeError("ERROR: Sign in to confirm you're not a bot")))
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp(VIDEOS_XML))

    out = youtube.poll(con)
    assert out["blocked"] == 1 and out["new"] == 0
    assert source_row(con, "YouTube: Walled Channel")["last_error"] == "Sign-in needed"
    assert events_of(con, "youtube") == []


def test_youtube_live_video_is_never_downloaded(con, monkeypatch):
    """A live broadcast has no end for the downloader to stop at, so it is
    skipped with a reason instead of recorded until the stream finishes."""
    monkeypatch.setenv("CAF_ASR", "faster-whisper")
    downloads = []

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            downloads.append(download)
            return {"id": "LIVE1234567", "title": "Markets open",
                    "is_live": True, "duration": None}

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    text, source_kind, info = youtube.transcript("LIVE1234567", con)
    assert (text, source_kind) == ("", None)
    assert info["skip_reason"] == "No captions, and the video is live or has no length yet."
    assert downloads == [False]


def test_youtube_skip_list_keeps_insertion_order(con):
    """Sorting before the 200 cap would drop the ids that sort lowest, and
    those get re-attempted every cycle."""
    add_youtube_source(con, name="YouTube: Ordered")
    source_id = source_row(con, "YouTube: Ordered")["source_id"]
    stored = []
    for video_id in ("zzz1", "aaa2", "mmm3"):
        stored = youtube._remember_skip(con, source_id, stored, video_id)
    assert stored == ["zzz1", "aaa2", "mmm3"]
    assert source_row(con, "YouTube: Ordered")["config"]["skipped"] == stored


def test_youtube_feed_error_status_is_reported(con, monkeypatch):
    add_youtube_source(con, name="YouTube: Deleted")
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp("", status=404))
    out = youtube.poll(con)
    assert out["errors"] == 1
    row = source_row(con, "YouTube: Deleted")
    assert row["last_error"] == "Feed unreachable" and row["last_polled"] is None


def test_youtube_caption_text_joins_segments():
    assert youtube.caption_text(JSON3) == "Nvidia lifted guidance again this quarter."
    assert youtube.caption_text("not json") == ""


# ---------------------------------------------------------------- §6.2 x

POSTS = [
    {"id": "1900000000000000002", "created_at": "2026-08-14T09:00:00.000Z",
     "text": "Second post with a link https://t.co/abc",
     "entities": {"urls": [{"url": "https://t.co/abc",
                            "expanded_url": "https://example.com/report"}]}},
    {"id": "1900000000000000001", "created_at": "2026-08-14T08:00:00.000Z",
     "text": "First post of the day."},
]


def add_x_source(con, name="@examplecorp", config=None):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "(%s, 'x', 'https://x.com/examplecorp', 'active', %s)",
        (name, Jsonb(config or {"username": "examplecorp"})))


def test_x_poll_writes_one_event_and_keeps_since_id(con, monkeypatch):
    credentials.set(con, "x", "AAAAtokenvalue")
    add_x_source(con)
    seen = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.append((url, dict(params or {})))
        if "/users/by/username/" in url:
            return ApiResp({"data": {"id": "42", "username": "examplecorp"}})
        if url.endswith("/tweets"):
            if (params or {}).get("since_id"):
                return ApiResp({"data": []})
            return ApiResp({"data": list(POSTS)})
        raise AssertionError(f"unexpected call {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    out = x.poll(con)
    assert out["new"] == 1
    ev = events_of(con, "x")[0]
    assert ev["meta"]["username"] == "examplecorp"
    assert ev["meta"]["newest_id"] == "1900000000000000002"
    assert len(ev["meta"]["post_ids"]) == 2
    row = source_row(con, "@examplecorp")
    assert row["config"]["since_id"] == "1900000000000000002"
    assert row["config"]["user_id"] == "42"
    assert row["last_polled"] is not None

    # nothing new -> no event, and the username lookup is not repeated
    before = len(seen)
    out2 = x.poll(con)
    assert out2["new"] == 0 and out2["empty"] == 1
    assert len(events_of(con, "x")) == 1
    assert all("/users/by/username/" not in u for u, _ in seen[before:])


def test_x_document_expands_short_links(con):
    title, doc = x.document("examplecorp", list(reversed(POSTS)), "2026-08-14")
    assert title == "@examplecorp, 2 posts, 2026-08-14"
    assert "# source_type: social" in doc
    assert "# origin: x.com/@examplecorp" in doc
    assert "https://example.com/report" in doc
    assert "https://t.co/abc" not in doc
    assert "https://x.com/examplecorp/status/1900000000000000001" in doc


def test_x_rate_limit_stops_the_cycle(con, monkeypatch):
    credentials.set(con, "x", "AAAAtokenvalue")
    add_x_source(con, name="@limited", config={"username": "limited",
                                               "user_id": "77"})
    monkeypatch.setattr(requests, "get",
                        lambda url, **kw: ApiResp({"title": "Too Many Requests"},
                                                  status=429))
    out = x.poll(con)
    assert out["new"] == 0 and out["blocked"] == 1
    assert source_row(con, "@limited")["last_error"] == "Rate limited, retry next cycle"
    assert events_of(con, "x") == []


def test_x_missing_token_is_a_sign_in(con, monkeypatch):
    add_x_source(con, name="@notoken", config={"username": "notoken",
                                               "user_id": "88"})
    out = x.poll(con)
    assert out["blocked"] == 1
    assert source_row(con, "@notoken")["last_error"] == "Sign-in needed"


# ---------------------------------------------------------------- §6.3 links


def test_links_enqueue_is_idempotent(con):
    row = links.enqueue(con, "https://blog.example.com/post", "article", None, "web")
    again = links.enqueue(con, "https://blog.example.com/post", "article", None,
                          "cli", title="Other")
    assert row["link_id"] == again["link_id"]
    assert again["added_by"] == "web"
    assert con.execute("select count(*) c from link_queue").fetchone()["c"] == 1


def test_links_process_article_then_duplicate(con, monkeypatch):
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp(PLAIN_HTML))
    links.enqueue(con, "https://blog.example.com/post", "article", None, "web")
    out = links.process(con)
    assert out["done"] == 1
    row = con.execute("select * from link_queue where url like '%%/post'").fetchone()
    assert row["status"] == "done"
    assert row["event_id"] is not None
    assert row["title"] == "Plain story"
    assert row["attempts"] == 1
    bucket = source_row(con, "link:blog.example.com")
    assert bucket["connector"] == "link" and bucket["is_internal"] is False
    ev = con.execute("select * from event where event_id=%s",
                     (row["event_id"],)).fetchone()
    assert ev["meta"]["item_url"] == "https://blog.example.com/post"

    # the same body under another URL is already in the vault
    links.enqueue(con, "https://blog.example.com/mirror", "article", None, "web")
    out2 = links.process(con)
    assert out2["duplicate"] == 1
    mirror = con.execute("select * from link_queue where url like '%%/mirror'").fetchone()
    assert mirror["status"] == "duplicate"


def test_links_blocked_row_waits_for_the_credential(con, monkeypatch):
    def blocked(url, **kw):
        raise fetch.SignInNeeded("ft", "HTTP 403")

    monkeypatch.setattr(fetch, "get", blocked)
    row = links.enqueue(con, "https://www.ft.com/content/abc-123", "article",
                        "ft", "web")
    out = links.process_one(con, row["link_id"])
    assert out["status"] == "blocked"
    assert out["error"] == "Sign-in needed for FT."
    assert out["attempts"] == 0
    assert links.process(con) == {"done": 0, "duplicate": 0, "blocked": 0,
                                  "failed": 0}   # blocked rows are not retried

    assert links.requeue_blocked(con, "ft") == 1
    assert con.execute("select status from link_queue where link_id=%s",
                       (row["link_id"],)).fetchone()["status"] == "queued"


def test_links_failed_rows_count_attempts(con, monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fetch, "get", boom)
    links.enqueue(con, "https://down.example.com/post", "article", None, "web")
    for expected in (1, 2, 3):
        out = links.process(con)
        assert out["failed"] == 1
        row = con.execute("select * from link_queue").fetchone()
        assert row["status"] == "failed" and row["attempts"] == expected
        assert "connection reset" in row["error"]
    assert links.process(con)["failed"] == 0      # attempt cap reached


def test_links_error_page_is_not_a_document(con, monkeypatch):
    """A 404 page answers with HTML; without a status check it ingests as an
    article and the row finishes green."""
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp(
        "<html><head><title>404 Not Found</title></head><body></body></html>",
        status=404))
    links.enqueue(con, "https://dead.example.com/story", "article", None, "web")
    out = links.process(con)
    assert out["failed"] == 1 and out["done"] == 0
    row = con.execute("select * from link_queue").fetchone()
    assert row["status"] == "failed" and row["error"] == "HTTP 404"
    assert con.execute("select count(*) c from event").fetchone()["c"] == 0


def test_links_x_rate_limit_keeps_the_attempt_budget(con, monkeypatch):
    """A rate limit is X's clock, not the link's fault: the row waits instead
    of using up its three attempts."""
    credentials.set(con, "x", "AAAAtokenvalue")
    monkeypatch.setattr(requests, "get", lambda url, **kw: ApiResp(
        {"title": "Too Many Requests"}, status=429))
    row = links.enqueue(con, "https://x.com/someone/status/1900000000000000123",
                        "x_post", "x", "web")
    out = links.process_one(con, row["link_id"])
    assert out["status"] == "failed"
    assert out["error"] == "Rate limited, retry next cycle"
    assert out["attempts"] == 0


def test_links_enqueue_reports_whether_it_inserted(con):
    first = links.enqueue(con, "https://twice.example.com/post", "article",
                          None, "web")
    again = links.enqueue(con, "https://twice.example.com/post", "article",
                          None, "web")
    assert first["created"] is True and again["created"] is False


def test_links_youtube_video_uses_the_transcript(con, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_ytdlp(CAPTIONS_INFO))
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp(JSON3))
    row = links.enqueue(con, "https://www.youtube.com/watch?v=VID12345678",
                        "youtube_video", "youtube", "web")
    out = links.process_one(con, row["link_id"])
    assert out["status"] == "done"
    ev = con.execute("select * from event where event_id=%s",
                     (out["event_id"],)).fetchone()
    assert ev["meta"]["transcript_source"] == "captions"
    assert source_row(con, "link:youtube.com") is not None


def test_links_x_post_becomes_an_event(con, monkeypatch):
    credentials.set(con, "x", "AAAAtokenvalue")
    monkeypatch.setattr(requests, "get", lambda url, **kw: ApiResp({
        "data": {"id": "1900000000000000009", "text": "A single post.",
                 "created_at": "2026-08-15T10:00:00.000Z"},
        "includes": {"users": [{"id": "42", "username": "examplecorp"}]}}))
    row = links.enqueue(
        con, "https://x.com/examplecorp/status/1900000000000000009", "x_post",
        "x", "web")
    out = links.process_one(con, row["link_id"])
    assert out["status"] == "done"
    ev = con.execute("select * from event where event_id=%s",
                     (out["event_id"],)).fetchone()
    assert ev["meta"]["post_ids"] == ["1900000000000000009"]
    assert source_row(con, "link:x.com") is not None


def test_links_video_id_parsing():
    assert links.video_id_of("https://www.youtube.com/watch?v=abc12345") == "abc12345"
    assert links.video_id_of("https://www.youtube.com/shorts/xyz98765") == "xyz98765"
    assert links.video_id_of("https://youtu.be/qrs45678") == "qrs45678"
    assert links.video_id_of("https://example.com/nope") == ""


# ---------------------------------------------------------------- §5.3 bridge

BRIDGE_FEED = {
    "title": "Telegram: examplechannel",
    "url": "https://t.me/s/examplechannel",
    "items": [{
        "title": "Fab update",
        "url": "https://t.me/examplechannel/1234",
        "published": "2026-08-16T11:00:00+00:00",
        "content_html": "<p>" + ("The fab is running ahead of schedule. " * 20)
                        + "</p>",
        "author": "examplechannel",
        "uid": "https://t.me/examplechannel/1234",
    }],
}


def test_bridge_poll_ingests_items(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('Telegram: examplechannel', 'bridge', null, 'active', %s)",
        (Jsonb({"bridge": {"name": "TelegramBridge",
                           "params": {"username": "examplechannel"}}}),))
    calls = []

    def fake_display(name, params, timeout=20):
        calls.append((name, params))
        return dict(BRIDGE_FEED)

    monkeypatch.setattr(bridge_client, "display", fake_display)
    out = bridge.poll(con)
    assert out["new"] == 1 and out["errors"] == 0
    assert calls == [("TelegramBridge", {"username": "examplechannel"})]
    ev = events_of(con, "bridge")[0]
    assert ev["meta"]["bridge"] == "TelegramBridge"
    assert ev["meta"]["item_url"] == "https://t.me/examplechannel/1234"
    assert ev["meta"]["feed"] == "Telegram: examplechannel"
    assert source_row(con, "Telegram: examplechannel")["last_polled"] is not None

    out2 = bridge.poll(con)
    assert out2["new"] == 0 and out2["duplicate"] == 1


def test_bridge_error_sets_last_error(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('Telegram: broken', 'bridge', null, 'active', %s)",
        (Jsonb({"bridge": {"name": "TelegramBridge",
                           "params": {"username": "broken"}}}),))

    def boom(name, params, timeout=20):
        raise bridge_client.BridgeError(502, "bad gateway")

    monkeypatch.setattr(bridge_client, "display", boom)
    out = bridge.poll(con)
    assert out["errors"] == 1 and out["new"] == 0
    assert source_row(con, "Telegram: broken")["last_error"] == "Feed unreachable"


def test_bridge_thin_item_falls_back_to_the_page(con, monkeypatch):
    con.execute(
        "insert into source (name, connector, url, status, config) values "
        "('Bluesky: someone', 'bridge', null, 'active', %s)",
        (Jsonb({"bridge": {"name": "BlueskyBridge", "params": {"user": "someone"}}}),))
    thin = {**BRIDGE_FEED, "items": [{**BRIDGE_FEED["items"][0],
                                      "content_html": "<p>Short note.</p>",
                                      "url": "https://blog.example.com/post"}]}
    monkeypatch.setattr(bridge_client, "display",
                        lambda name, params, timeout=20: dict(thin))
    monkeypatch.setattr(fetch, "get", lambda url, **kw: Resp(PLAIN_HTML))
    out = bridge.poll(con)
    assert out["new"] == 1 and out["thin"] == 0


# ---------------------------------------------------------------- §3.2 fetch


def test_fetch_refuses_the_private_network():
    """The sources page hands fetch whatever address a user pasted, so the
    compose network has to be out of reach (metadata, Postgres, the bridge)."""
    assert fetch.blocked_host("169.254.169.254") is True
    assert fetch.blocked_host("127.0.0.1") is True
    assert fetch.blocked_host("10.0.0.5") is True
    assert fetch.blocked_host("postgres.") is True      # a trailing dot resolves
    assert fetch.blocked_host("caf-rss-bridge") is True
    assert fetch.blocked_host("www.ft.com") is False

    for url in ("http://127.0.0.1:8642/api/status",
                "https://169.254.169.254/latest/meta-data",
                "http://caf-rss-bridge./?action=list",
                "file:///etc/passwd"):
        with pytest.raises(fetch.BlockedAddress):
            fetch.get(url)


def test_bridge_errors_never_carry_the_token(monkeypatch):
    monkeypatch.setenv("CAF_BRIDGE_URL", "http://caf-rss-bridge")
    monkeypatch.setenv("CAF_BRIDGE_TOKEN", "SUPERSECRETBRIDGETOKEN")

    def unreachable(*a, **kw):
        raise requests.ConnectionError(
            "Max retries exceeded with url: /?action=findfeed&token="
            "SUPERSECRETBRIDGETOKEN")

    monkeypatch.setattr(bridge_client.requests, "get", unreachable)
    with pytest.raises(bridge_client.BridgeError) as caught:
        bridge_client.findfeed("https://t.me/s/durov")
    assert "SUPERSECRETBRIDGETOKEN" not in str(caught.value)
    assert caught.value.detail == "bridge unreachable"
    assert bridge_client._scrub("token=SUPERSECRETBRIDGETOKEN") == "token=***"


def test_bearer_token_refuses_a_header_break():
    """A stray carriage return survives strip() and then travels into the
    client's error text, which is stored and shown."""
    with pytest.raises(credentials.InvalidCredential):
        credentials.bearer_token("AAAA\rSECRETTOKEN")
    with pytest.raises(credentials.InvalidCredential):
        credentials.bearer_token("two words")
    # real X tokens are percent-encoded, and those still pass
    assert credentials.bearer_token("Bearer AAAA%2Fbb%3Dcc-dd_ee") == \
        "AAAA%2Fbb%3Dcc-dd_ee"


def test_credential_values_are_scrubbed_from_messages(con):
    credentials.set(con, "x", "AAAAsupersecrettokenvalue")
    credentials.set(con, "ft", "FTSession=ftsecretcookievalue")
    text = credentials.scrub(
        con, "Bearer AAAAsupersecrettokenvalue rejected (ftsecretcookievalue)")
    assert "supersecret" not in text and "ftsecretcookievalue" not in text
    assert text.count("***") == 2
    con.rollback()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Anything this module forgets to mock fails loudly instead of dialling out."""
    def forbidden(*a, **kw):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)

def test_stale_feed_is_flagged_but_still_polled(con, monkeypatch):
    from datetime import date
    old = [{"date": "2025-01-27"}, {"date": "2025-01-24"}]
    assert rss.stale_since(old, today=date(2026, 8, 18)) == "2025-01-27"
    fresh = [{"date": "2026-08-17"}, {"date": "2025-01-24"}]
    assert rss.stale_since(fresh, today=date(2026, 8, 18)) is None
    assert rss.stale_since([{"date": ""}, {}], today=date(2026, 8, 18)) is None

def test_walled_ft_with_a_cookie_says_headlines_only(con, monkeypatch):
    from graph import credentials
    credentials.set(con, "ft", "FTSession_s=abc")
    assert rss.wall_message(con, "ft") == rss.HEADLINES_ONLY
    assert rss.wall_message(con, "wsj") == "Sign-in needed"
    assert rss.wall_message(con, "substack") == "Sign-in needed"
    con.rollback()
