"""URL router tests (build spec v4 §4): one fixture URL per rule, no network.

`graph.fetch.head`, `graph.bridge.findfeed` and the yt-dlp channel fallback are
monkeypatched; anything the fixtures do not wire raises a connection error, so
a rule that reaches for the network by mistake shows up as an unreachable
result instead of a real request.
"""
import json

import pytest
import requests
from psycopg.types.json import Jsonb

from graph import bridge, credentials, fetch, router

REAL_FINDFEED = bridge.findfeed          # captured before any monkeypatching

# ---------------------------------------------------------------- fixtures

YT_CHANNEL_HTML = """<!doctype html><html><head>
<link rel="canonical" href="https://www.youtube.com/channel/UCIALMKvObZNtJ6AmdCLP7Lg">
<meta property="og:title" content="Bloomberg Television">
<title>Bloomberg Television - YouTube</title>
<script>var ytInitialData = {"externalId":"UCIALMKvObZNtJ6AmdCLP7Lg"};</script>
</head><body></body></html>"""

YT_CONSENT_WALL = """<!doctype html><html><head><title>Before you continue
</title></head><body>We use cookies</body></html>"""

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Stratechery by Ben Thompson</title>
  <link>https://stratechery.com</link>
  <item><title>The AI capex cycle</title>
    <link>https://stratechery.com/2026/the-ai-capex-cycle/</link>
    <pubDate>Mon, 17 Aug 2026 09:00:00 +0000</pubDate></item>
</channel></rss>"""

PODCAST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel>
  <title>Unhedged</title>
  <item><title>Episode 214</title>
    <pubDate>Mon, 17 Aug 2026 05:00:00 +0000</pubDate>
    <enclosure url="https://cdn.acast.com/214.mp3" length="2411" type="audio/mpeg"/>
  </item>
</channel></rss>"""

ATOM_XML = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Recent releases from openai/openai-python</title>
  <entry><title>v2.4.0</title>
    <link href="https://github.com/openai/openai-python/releases/tag/v2.4.0"/>
    <updated>2026-08-14T10:00:00Z</updated></entry>
</feed>"""

SUBSTACK_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Platformer</title>
  <generator>Substack</generator>
  <item><title>The week in platforms</title>
    <link>https://www.platformer.news/p/the-week-in-platforms</link></item>
</channel></rss>"""

YT_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>UCIALMKvObZNtJ6AmdCLP7Lg</yt:channelId>
  <title>Bloomberg Television</title>
  <entry><yt:videoId>Kx9nGZ2XmQ0</yt:videoId><title>The markets wrap</title>
    <published>2026-08-17T21:00:00+00:00</published></entry>
</feed>"""

FT_SECTION_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Technology</title>
  <link>https://www.ft.com/companies/technology</link>
  <item><title>Chipmakers raise guidance</title>
    <link>https://www.ft.com/content/8f1c5b4e-2f6a-4a1e-9a2e-6b0c1d2e3f40</link>
  </item>
</channel></rss>"""

AUTODISCOVER_HTML = """<!doctype html><html><head>
<title>Ben's blog</title>
<link rel="alternate" type="application/rss+xml" title="Feed" href="/feed.xml">
</head><body><p>Hello.</p></body></html>"""

ARTICLE_HTML = """<!doctype html><html><head><title>Chip demand cools</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"NewsArticle",
 "headline":"Chip demand cools","datePublished":"2026-08-17T06:00:00Z"}
</script></head><body><article><p>Demand cooled in the quarter.</p></article>
</body></html>"""

PLAIN_HTML = """<!doctype html><html><head><title>Contact us</title></head>
<body><p>Call the desk.</p></body></html>"""

ITUNES_JSON = json.dumps({"resultCount": 1, "results": [{
    "collectionName": "The Journal.",
    "feedUrl": "https://video-api.wsj.com/podcast/rss/wsj/the-journal"}]})

ARCHIVE_PAID = json.dumps([{"publication_id": 8231, "slug": "the-week-in-platforms",
                           "audience": "only_paid"}])
ARCHIVE_FREE = json.dumps([{"publication_id": 8231, "slug": "hello",
                           "audience": "everyone"}])

# rss-bridge answers findfeed with the display URL plus the bridge metadata
FINDFEED_RAW = [{"url": "/?action=display&bridge=TelegramBridge&username=durov"
                        "&format=Json",
                 "bridgeMeta": {"name": "Telegram"}}]
FINDFEED_HIT = [{"bridge": "TelegramBridge", "params": {"username": "durov"},
                 "url": "/?action=display&bridge=TelegramBridge&format=Json"
                        "&username=durov",
                 "name": "Telegram"}]

XML = "application/rss+xml; charset=utf-8"
JSON_CT = "application/json"


# ---------------------------------------------------------------- fake network


class Net:
    """Stand-in for graph.fetch.head. Only wired URLs answer; everything else
    raises, the way an unreachable host does."""

    def __init__(self):
        self.pages = {}
        self.calls = []

    def add(self, url, body, ct="text/html; charset=utf-8", status=200):
        self.pages[url] = (body, ct, status)
        return self

    def head(self, url, **kw):
        self.calls.append(url)
        if url not in self.pages:
            raise requests.ConnectionError(f"no fixture for {url}")
        body, ct, status = self.pages[url]
        return fetch.Response(status, url, body.encode("utf-8"),
                              {"content-type": ct})


@pytest.fixture
def net(monkeypatch):
    n = Net()
    monkeypatch.setattr(fetch, "head", n.head)
    monkeypatch.setattr(fetch, "get", lambda *a, **k: pytest.fail("no network"))
    monkeypatch.setattr(bridge, "findfeed", lambda url, **k: [])
    monkeypatch.setattr(router, "_ytdlp_channel", lambda url: (None, None))
    return n


# ---------------------------------------------------------------- normalize


def test_normalize():
    assert router.normalize("ft.com/rss/home") == "https://ft.com/rss/home"
    assert router.normalize("https://example.com/a/?utm_source=news&utm_id=2&x=1"
                            "#top") == "https://example.com/a?x=1"
    assert router.normalize("https://www.wsj.com/tech/deal-1a2b3c4d?mod=hp_lead") \
        == "https://www.wsj.com/tech/deal-1a2b3c4d"
    assert router.normalize("https://EXAMPLE.com/Path/") == "https://example.com/Path"
    assert router.normalize("https://mobile.twitter.com/elonmusk") == "https://x.com/elonmusk"
    assert router.normalize("twitter.com/durov/status/12") == "https://x.com/durov/status/12"
    assert router.normalize("https://youtu.be/dQw4w9WgXcQ?t=30") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert router.normalize("m.youtube.com/@Bloomberg") == "https://www.youtube.com/@Bloomberg"
    assert router.normalize("https://example.com/a?fbclid=1&gclid=2&ref=hn") == \
        "https://example.com/a"
    assert router.normalize("  ") == ""
    # anything that is not an http(s) address comes back empty, so the caller
    # says "That does not look like a link." instead of storing it verbatim
    assert router.normalize("https://user:SECRET@example.com:99999/feed") == ""
    assert router.normalize("ftp://files.example.com/pub") == ""
    # userinfo is dropped on the normal path too
    assert router.normalize("https://user:SECRET@example.com/feed") == \
        "https://example.com/feed"
    # 'postgres.' and 'postgres' are one name to DNS
    assert router.normalize("http://postgres.:5432/") == "http://postgres:5432"


def test_private_addresses_are_never_fetched(con, net):
    """POST /api/sources/resolve takes any address a user pastes, and the rules
    below rule 19 fetch it. The compose network stays out of reach."""
    for pasted in ("169.254.169.254/latest/meta-data/",
                   "http://127.0.0.1:8642/api/status",
                   "10.0.0.5:5432",
                   "http://caf-rss-bridge./?action=list"):
        r = router.resolve(con, pasted)
        assert r.kind == "unsupported"
        assert r.message == router.NO_FEED
    assert net.calls == []


# ---------------------------------------------------------------- rules 1-19


def test_rule_1_youtube_video(con, net):
    r = router.resolve(con, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert (r.kind, r.one_off, r.link_kind, r.site) == ("link", True, "youtube_video", "youtube")
    assert r.label == "YouTube video"
    assert r.config["video_id"] == "dQw4w9WgXcQ"
    assert r.credential == {"site": "youtube", "set": False}
    assert r.message == "Videos are transcribed from captions."
    assert net.calls == []


def test_rule_2_youtube_short(con, net):
    r = router.resolve(con, "https://www.youtube.com/shorts/abc123XYZ_")
    assert (r.kind, r.link_kind) == ("link", "youtube_video")
    assert r.config["video_id"] == "abc123XYZ_"
    assert net.calls == []


def test_rule_3_youtube_playlist(con, net):
    net.add("https://www.youtube.com/feeds/videos.xml?playlist_id=PL123456", YT_FEED_XML, XML)
    r = router.resolve(con, "https://www.youtube.com/playlist?list=PL123456")
    assert (r.kind, r.connector, r.label) == ("youtube", "youtube", "YouTube playlist")
    assert r.config["playlist_id"] == "PL123456"
    assert r.feed_url == "https://www.youtube.com/feeds/videos.xml?playlist_id=PL123456"
    assert r.name == "Bloomberg Television"
    # one fetch: the native feed's <title> names the source
    assert net.calls == ["https://www.youtube.com/feeds/videos.xml?playlist_id=PL123456"]


def test_rule_4_youtube_channel_id(con, net):
    net.add("https://www.youtube.com/feeds/videos.xml?channel_id=UCIALMKvObZNtJ6AmdCLP7Lg",
            YT_FEED_XML, XML)
    r = router.resolve(con, "https://www.youtube.com/channel/UCIALMKvObZNtJ6AmdCLP7Lg")
    assert (r.kind, r.label) == ("youtube", "YouTube channel")
    assert r.config["channel_id"] == "UCIALMKvObZNtJ6AmdCLP7Lg"
    assert r.feed_url.endswith("channel_id=UCIALMKvObZNtJ6AmdCLP7Lg")
    assert r.name == "Bloomberg Television"
    assert net.calls == ["https://www.youtube.com/feeds/videos.xml?channel_id=UCIALMKvObZNtJ6AmdCLP7Lg"]


def test_rule_5_youtube_handle(con, net):
    net.add("https://www.youtube.com/@Bloomberg", YT_CHANNEL_HTML)
    r = router.resolve(con, "https://www.youtube.com/@Bloomberg/videos")
    assert r.kind == "youtube"
    assert r.config == {"channel_id": "UCIALMKvObZNtJ6AmdCLP7Lg",
                        "handle": "@Bloomberg", "title": "Bloomberg Television",
                        "site": "youtube"}
    assert r.name == "Bloomberg Television"
    assert net.calls == ["https://www.youtube.com/@Bloomberg"]


def test_rule_5_falls_back_to_ytdlp(con, net, monkeypatch):
    net.add("https://www.youtube.com/c/CNBC", YT_CONSENT_WALL)
    monkeypatch.setattr(router, "_ytdlp_channel",
                        lambda url: ("UCrp_UI8XtuYfpiqluWLD7Lw", "CNBC"))
    r = router.resolve(con, "https://www.youtube.com/c/CNBC")
    assert r.config["channel_id"] == "UCrp_UI8XtuYfpiqluWLD7Lw"
    assert r.name == "CNBC"


def test_youtube_channel_id_parser():
    assert router.youtube_channel_id(YT_CHANNEL_HTML) == \
        ("UCIALMKvObZNtJ6AmdCLP7Lg", "Bloomberg Television")
    assert router.youtube_channel_id(PLAIN_HTML)[0] is None


def test_rule_6_x_post(con, net):
    r = router.resolve(con, "https://twitter.com/durov/status/1795000000000000000")
    assert (r.kind, r.one_off, r.link_kind, r.site) == ("link", True, "x_post", "x")
    assert r.config == {"username": "durov", "post_id": "1795000000000000000"}
    assert r.message == "Posts are fetched with the X token."
    assert net.calls == []


def test_rule_7_x_account(con, net):
    r = router.resolve(con, "https://x.com/lisaabramowicz1")
    assert (r.kind, r.connector, r.label) == ("x", "x", "X account")
    assert r.config["username"] == "lisaabramowicz1"
    assert r.name == "@lisaabramowicz1"
    assert net.calls == []


def test_x_reserved_path_is_not_an_account(con, net):
    r = router.resolve(con, "https://x.com/explore")
    assert r.kind == "unsupported"


def test_rule_8_ft_article(con, net):
    url = "https://www.ft.com/content/8f1c5b4e-2f6a-4a1e-9a2e-6b0c1d2e3f40"
    r = router.resolve(con, url)
    assert (r.kind, r.link_kind, r.site, r.label) == ("link", "article", "ft", "Article")
    assert r.credential == {"site": "ft", "set": False}
    assert r.message == "Full text needs the FT sign-in."
    assert net.calls == []


def test_rule_9_ft_rss(con, net):
    r = router.resolve(con, "https://www.ft.com/rss/companies")
    assert (r.kind, r.connector, r.label, r.site) == ("feed", "rss", "FT feed", "ft")
    assert r.feed_url == "https://www.ft.com/rss/companies"
    assert r.name == "FT companies"
    assert r.config["site"] == "ft"
    assert net.calls == []


def test_rule_10_ft_section(con, net):
    net.add("https://www.ft.com/companies/technology?format=rss", FT_SECTION_RSS, XML)
    r = router.resolve(con, "https://www.ft.com/companies/technology")
    assert (r.kind, r.site, r.label) == ("feed", "ft", "FT feed")
    assert r.feed_url == "https://www.ft.com/companies/technology?format=rss"
    assert r.name == "Technology"          # the feed's own title wins


def test_rule_10_unverified_ft_section_falls_through(con, net):
    """A section page whose ?format=rss is not a feed keeps walking the table."""
    net.add("https://www.ft.com/careers?format=rss", PLAIN_HTML)
    net.add("https://www.ft.com/careers", PLAIN_HTML)
    assert router.resolve(con, "https://www.ft.com/careers").kind == "unsupported"


def test_rule_11_wsj_feed_host(con, net):
    r = router.resolve(con, "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain")
    assert (r.kind, r.label, r.site) == ("feed", "WSJ feed", "wsj")
    assert r.name == "WSJ markets"
    assert r.feed_url == "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"
    # the legacy host froze in Jan 2025 (still 200): pasted legacy URLs map
    # onto the live host with the same slug
    r = router.resolve(con, "https://feeds.a.dj.com/rss/RSSMarketsMain.xml")
    assert r.feed_url == "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"
    assert r.name == "WSJ markets"
    assert net.calls == []


def test_rule_12_wsj_article(con, net):
    r = router.resolve(con, "https://www.wsj.com/tech/ai/openai-chip-deal-1a2b3c4d")
    assert (r.kind, r.link_kind, r.site) == ("link", "article", "wsj")
    assert r.message == "Full text needs the WSJ sign-in."
    r2 = router.resolve(con, "https://www.wsj.com/articles/fed-holds-rates-11720000000")
    assert (r2.kind, r2.link_kind) == ("link", "article")
    assert net.calls == []


def test_rule_13_wsj_section(con, net):
    r = router.resolve(con, "https://www.wsj.com/markets")
    assert (r.kind, r.label, r.site) == ("feed", "WSJ feed", "wsj")
    assert r.feed_url == "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"
    assert r.name == "WSJ markets"
    assert router.resolve(con, "https://www.wsj.com").feed_url == \
        "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"
    assert net.calls == []


def test_rule_14_apple_podcasts(con, net):
    net.add("https://itunes.apple.com/lookup?id=1469394914&entity=podcast",
            ITUNES_JSON, JSON_CT)
    r = router.resolve(con, "https://podcasts.apple.com/us/podcast/the-journal/id1469394914")
    assert (r.kind, r.connector, r.label) == ("podcast", "podcast", "Podcast")
    assert r.feed_url == "https://video-api.wsj.com/podcast/rss/wsj/the-journal"
    assert r.name == "podcast:The Journal."


def test_rule_15_bluesky_profile(con, net):
    r = router.resolve(con, "https://bsky.app/profile/dampierre.bsky.social")
    assert r.kind == "feed"
    assert r.feed_url == "https://bsky.app/profile/dampierre.bsky.social/rss"
    assert net.calls == []


def test_rule_16_telegram(con, net):
    r = router.resolve(con, "https://t.me/s/durov")
    assert (r.kind, r.connector, r.label) == ("bridge", "bridge", "Telegram")
    assert r.config["bridge"] == {"name": "TelegramBridge",
                                  "params": {"username": "durov"}}
    assert r.name == "durov"
    assert "TelegramBridge" in r.feed_url
    assert net.calls == []


def test_rule_17_reddit_native_feed(con, net):
    net.add("https://www.reddit.com/r/stocks/.rss", ATOM_XML, XML)
    r = router.resolve(con, "https://www.reddit.com/r/stocks")
    assert r.kind == "feed"
    assert r.feed_url == "https://www.reddit.com/r/stocks/.rss"


def test_rule_17_reddit_falls_back_to_bridge(con, net):
    net.add("https://www.reddit.com/r/stocks/.rss", PLAIN_HTML)
    r = router.resolve(con, "https://reddit.com/r/stocks")
    assert (r.kind, r.label) == ("bridge", "Reddit")
    assert r.config["bridge"]["name"] == "RedditBridge"
    assert r.name == "r/stocks"


def test_rule_18_github_releases(con, net):
    r = router.resolve(con, "https://github.com/openai/openai-python")
    assert r.kind == "feed"
    assert r.feed_url == "https://github.com/openai/openai-python/releases.atom"
    assert r.name == "openai/openai-python"
    assert net.calls == []


def test_rule_19_medium(con, net):
    r = router.resolve(con, "https://medium.com/@benedictevans")
    assert r.feed_url == "https://medium.com/feed/@benedictevans"
    r2 = router.resolve(con, "https://towardsdatascience.medium.com")
    assert r2.feed_url == "https://towardsdatascience.medium.com/feed"
    assert net.calls == []


# ---------------------------------------------------------------- rules 20-25


def test_rule_20_substack_publication(con, net):
    net.add("https://platformer.substack.com/feed", SUBSTACK_FEED, XML)
    net.add("https://platformer.substack.com/api/v1/archive?sort=new&limit=12",
            ARCHIVE_PAID, JSON_CT)
    r = router.resolve(con, "https://platformer.substack.com")
    assert (r.kind, r.connector, r.label, r.site) == ("feed", "rss", "Substack", "substack")
    assert r.feed_url == "https://platformer.substack.com/feed"
    assert r.name == "Platformer"
    assert r.config["substack"] == {"origin": "https://platformer.substack.com"}
    assert r.credential == {"site": "substack", "set": False}
    assert r.message == "Paid posts need the Substack sign-in."


def test_rule_20_free_substack_needs_no_credential(con, net):
    net.add("https://platformer.substack.com/feed", SUBSTACK_FEED, XML)
    net.add("https://platformer.substack.com/api/v1/archive?sort=new&limit=12",
            ARCHIVE_FREE, JSON_CT)
    r = router.resolve(con, "https://platformer.substack.com")
    assert r.credential is None
    assert r.message is None


def test_rule_20_substack_post_on_a_custom_domain(con, net):
    net.add("https://www.platformer.news/feed", SUBSTACK_FEED, XML)
    net.add("https://www.platformer.news/api/v1/archive?sort=new&limit=12",
            ARCHIVE_PAID, JSON_CT)
    r = router.resolve(con, "https://www.platformer.news/p/the-week-in-platforms")
    assert (r.kind, r.one_off, r.link_kind, r.site) == \
        ("link", True, "substack_post", "substack")
    assert r.credential == {"site": "substack", "set": False}


def test_rule_20_share_url_routes_to_the_publication(con, net):
    """open.substack.com/pub/<pub>/p/<slug> is Substack's own share form: it is
    one post from that publication, not Substack's site feed."""
    net.add("https://platformer.substack.com/api/v1/archive?sort=new&limit=12",
            ARCHIVE_PAID, JSON_CT)
    r = router.resolve(
        con, "https://open.substack.com/pub/platformer/p/the-week-in-platforms?r=abc")
    assert (r.kind, r.one_off, r.link_kind, r.site) == \
        ("link", True, "substack_post", "substack")
    assert r.url.startswith("https://platformer.substack.com/p/the-week-in-platforms")
    assert "open.substack.com/feed" not in (r.feed_url or "")

    # and the publication root of the same share form is the publication feed
    net.add("https://platformer.substack.com/feed", SUBSTACK_FEED, XML)
    pub = router.resolve(con, "https://open.substack.com/pub/platformer")
    assert pub.feed_url == "https://platformer.substack.com/feed"


def test_rule_20_publication_branch_verifies_the_feed(con, net):
    """A .substack.com host whose /feed is not a feed keeps walking the table
    instead of committing to a source that polls an HTML page."""
    net.add("https://notreally.substack.com/feed", PLAIN_HTML)
    net.add("https://notreally.substack.com", PLAIN_HTML)
    assert router.resolve(con, "https://notreally.substack.com").kind == "unsupported"


def test_a_dead_front_page_still_finds_the_feed(con, net):
    """The root can time out while /feed answers: one probe before giving up,
    which is also what catches a Substack custom domain with a dead root."""
    net.add("https://www.platformer.news/feed", SUBSTACK_FEED, XML)
    net.add("https://www.platformer.news/api/v1/archive?sort=new&limit=12",
            ARCHIVE_FREE, JSON_CT)
    r = router.resolve(con, "https://www.platformer.news")
    assert (r.kind, r.site, r.label) == ("feed", "substack", "Substack")
    assert r.feed_url == "https://www.platformer.news/feed"


def test_resolve_stops_at_its_deadline(con, net, monkeypatch):
    """One paste must not hold an API worker for the sum of every rule's
    timeout: once the budget is spent, the remaining steps do not fetch."""
    monkeypatch.setattr(router, "BUDGET", 0)
    r = router.resolve(con, "https://slow.example.com/blog")
    assert r.kind == "unsupported"
    assert r.message == router.UNREACHABLE
    assert net.calls == []


def test_rule_21_raw_rss(con, net):
    net.add("https://stratechery.com/feed", RSS_XML, XML)
    r = router.resolve(con, "https://stratechery.com/feed")
    assert (r.kind, r.connector, r.label) == ("feed", "rss", "News feed")
    assert r.name == "Stratechery by Ben Thompson"
    assert r.feed_url == "https://stratechery.com/feed"


def test_rule_21_podcast_enclosure(con, net):
    net.add("https://feeds.acast.com/public/shows/unhedged", PODCAST_XML, XML)
    r = router.resolve(con, "https://feeds.acast.com/public/shows/unhedged")
    assert (r.kind, r.connector, r.label) == ("podcast", "podcast", "Podcast")
    assert r.name == "podcast:Unhedged"


def test_rule_21_atom(con, net):
    net.add("https://github.com/openai/openai-python/releases.atom", ATOM_XML,
            "application/atom+xml")
    r = router.resolve(con, "https://github.com/openai/openai-python/releases.atom")
    assert r.kind == "feed"
    assert r.name == "Recent releases from openai/openai-python"
    assert router.parse_feed_head(ATOM_XML)["is_atom"] is True


def test_rule_21_substack_generator_on_a_custom_domain(con, net):
    net.add("https://www.platformer.news/feed", SUBSTACK_FEED, XML)
    net.add("https://www.platformer.news/api/v1/archive?sort=new&limit=12",
            ARCHIVE_PAID, JSON_CT)
    r = router.resolve(con, "https://www.platformer.news/feed")
    assert (r.kind, r.site, r.label) == ("feed", "substack", "Substack")
    assert r.config["substack"] == {"origin": "https://www.platformer.news"}


def test_rule_21_youtube_feed_keeps_the_youtube_connector(con, net):
    """A pasted videos.xml is a channel, not a plain feed: transcripts need the
    youtube connector."""
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCIALMKvObZNtJ6AmdCLP7Lg"
    net.add(url, YT_FEED_XML, "text/xml")
    r = router.resolve(con, url)
    assert (r.kind, r.connector, r.site) == ("youtube", "youtube", "youtube")
    assert r.config["channel_id"] == "UCIALMKvObZNtJ6AmdCLP7Lg"
    assert r.name == "Bloomberg Television"


def test_rule_22_autodiscovery(con, net):
    net.add("https://blog.example.com", AUTODISCOVER_HTML)
    net.add("https://blog.example.com/feed.xml", RSS_XML, XML)
    r = router.resolve(con, "blog.example.com")
    assert r.kind == "feed"
    assert r.feed_url == "https://blog.example.com/feed.xml"
    assert r.name == "Stratechery by Ben Thompson"


def test_rule_23b_well_known_feed_path(con, net):
    # front page is a bot wall (403) but WordPress still serves /feed
    net.add("https://walled.example.com", "<html>Access denied</html>", status=403)
    net.add("https://walled.example.com/feed", RSS_XML, XML)
    r = router.resolve(con, "https://walled.example.com")
    assert r.kind == "feed"
    assert r.feed_url == "https://walled.example.com/feed"
    # a plain page with no feed anywhere still says "no feed", not "unreachable"
    net.add("https://plain.example.com", PLAIN_HTML)
    r = router.resolve(con, "https://plain.example.com")
    assert r.kind == "unsupported"
    assert r.message == router.NO_FEED


def test_rule_20_substack_by_x_cluster_header(con, monkeypatch):
    # custom domain, generator tag missing, but the x-cluster header is there
    calls = []

    def head(url, **kw):
        calls.append(url)
        if url == "https://letters.example.com/feed":
            return fetch.Response(200, url, b"<rss><channel><title>Letters</title></channel></rss>",
                                  {"content-type": "application/xml", "x-cluster": "substack",
                                   "x-sub": "letters"})
        raise requests.ConnectionError("no fixture")

    monkeypatch.setattr(fetch, "head", head)
    assert router.substack_origin("https://letters.example.com/p/hello") == "https://letters.example.com"


def test_rule_23_article_page(con, net):
    net.add("https://www.reuters.com/business/chip-demand-cools", ARTICLE_HTML)
    r = router.resolve(con, "https://www.reuters.com/business/chip-demand-cools")
    assert (r.kind, r.one_off, r.link_kind, r.label) == ("link", True, "article", "Article")
    assert r.site is None
    assert r.credential is None


def test_is_article_html():
    assert router.is_article_html(ARTICLE_HTML) is True
    assert router.is_article_html(PLAIN_HTML) is False
    long_article = "<article>" + "<p>" + "x" * 1600 + "</p></article>"
    assert router.is_article_html(long_article) is True
    assert router.is_article_html(
        '<meta property="og:type" content="article">') is True


def test_rule_24_bridge_findfeed(con, net, monkeypatch):
    net.add("https://vk.com/durov", PLAIN_HTML)
    monkeypatch.setattr(bridge, "findfeed", lambda url, **k: FINDFEED_HIT)
    r = router.resolve(con, "https://vk.com/durov")
    assert (r.kind, r.connector, r.label) == ("bridge", "bridge", "Telegram")
    assert r.config["bridge"] == {"name": "TelegramBridge",
                                  "params": {"username": "durov"}}
    assert r.name == "durov"
    assert r.feed_url == FINDFEED_HIT[0]["url"]


def test_findfeed_hit_shape(monkeypatch):
    """The router consumes what graph.bridge.findfeed makes of a raw hit."""
    class R:
        status_code = 200
        def json(self):
            return FINDFEED_RAW
    monkeypatch.setenv("CAF_BRIDGE_URL", "http://caf-rss-bridge")
    monkeypatch.setattr(bridge.requests, "get", lambda *a, **k: R())
    hit = bridge.findfeed("https://t.me/durov")[0]
    assert hit["bridge"] == "TelegramBridge"
    assert hit["params"] == {"username": "durov"}
    assert hit["name"] == "Telegram"


def test_rule_25_unsupported(con, net):
    net.add("https://example.com/contact", PLAIN_HTML)
    r = router.resolve(con, "https://example.com/contact")
    assert (r.kind, r.connector, r.feed_url) == ("unsupported", None, None)
    assert r.message == ("No feed found at this address. Paste an article link "
                         "to add it once, or a feed URL.")


def test_unreachable_address(con, net):
    r = router.resolve(con, "https://nothing.example/blog")
    assert r.kind == "unsupported"
    assert r.message == "Could not reach that address."


def test_bridge_is_skipped_when_it_is_not_configured(con, net, monkeypatch):
    """graph.bridge.findfeed returns [] with no CAF_BRIDGE_URL, so rule 24 is
    a no-op and the paste reports rule 25."""
    monkeypatch.delenv("CAF_BRIDGE_URL", raising=False)
    monkeypatch.setattr(bridge, "findfeed", REAL_FINDFEED)
    net.add("https://example.com/contact", PLAIN_HTML)
    assert bridge.findfeed("https://example.com/contact") == []
    assert router.resolve(con, "https://example.com/contact").kind == "unsupported"


# ---------------------------------------------------------------- credentials


def test_credential_flag_follows_the_stored_value(con, net):
    assert router.resolve(con, "https://x.com/lisaabramowicz1").credential == \
        {"site": "x", "set": False}
    credentials.set(con, "x", "AAAAtokenvalue")
    r = router.resolve(con, "https://x.com/lisaabramowicz1")
    assert r.credential == {"site": "x", "set": True}
    r2 = router.resolve(con, "https://www.ft.com/rss/home")
    assert r2.credential == {"site": "ft", "set": False}
    credentials.set(con, "ft", "ft_session=abc; ft_user=1")
    assert router.resolve(con, "https://www.ft.com/rss/home").message is None


# ---------------------------------------------------------------- plumbing


def test_resolution_as_dict_is_json_safe(con, net):
    r = router.resolve(con, "https://feeds.a.dj.com/rss/RSSMarketsMain.xml")
    payload = json.loads(json.dumps(r.as_dict()))
    assert set(payload) == {"kind", "connector", "label", "url", "name",
                            "feed_url", "site", "config", "one_off",
                            "link_kind", "credential", "message"}
    assert payload["kind"] == "feed"


def test_duplicate_of(con, net):
    r = router.resolve(con, "https://feeds.a.dj.com/rss/RSSMarketsMain.xml")
    assert router.duplicate_of(con, r) is None
    con.execute("insert into source (name, connector, url, status) "
                "values ('WSJ markets', 'rss', %s, 'active')", (r.feed_url,))
    assert router.duplicate_of(con, r) == "WSJ markets"


def test_duplicate_of_ignores_dropped_sources(con, net):
    r = router.resolve(con, "https://feeds.a.dj.com/rss/RSSWorldNews.xml")
    con.execute("insert into source (name, connector, url, status) "
                "values ('WSJ world', 'rss', %s, 'dropped')", (r.feed_url,))
    assert router.duplicate_of(con, r) is None


def test_duplicate_of_matches_config_keys(con, net):
    r = router.resolve(con, "https://www.youtube.com/channel/UCIALMKvObZNtJ6AmdCLP7Lg")
    con.execute("insert into source (name, connector, url, status, config) "
                "values ('YouTube: Bloomberg', 'youtube', %s, 'active', %s)",
                ("https://www.youtube.com/channel/UCIALMKvObZNtJ6AmdCLP7Lg",
                 Jsonb({"channel_id": "UCIALMKvObZNtJ6AmdCLP7Lg"})))
    assert router.duplicate_of(con, r) == "YouTube: Bloomberg"

    x = router.resolve(con, "https://x.com/durov")
    assert router.duplicate_of(con, x) is None
    con.execute("insert into source (name, connector, status, config) "
                "values ('@durov', 'x', 'active', %s)",
                (Jsonb({"username": "durov"}),))
    assert router.duplicate_of(con, x) == "@durov"


def test_duplicate_of_skips_one_off_links(con, net):
    r = router.resolve(con, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert router.duplicate_of(con, r) is None
