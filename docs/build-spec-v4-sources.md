# Build spec v4 — sources overhaul

Owner direction (2026-08-18): the sources page is more complex than it needs
to be. Three changes:

1. **Watchlist by ticker alone.** No sector field on entry. Company name,
   sector, industry, exchange and country come from Yahoo Finance (the
   `yfinance` package) at add time. Non-US tickers (`0700.HK`, `7203.T`,
   `SAP.DE`) are accepted; EDGAR polling still only covers tickers in the SEC
   registry.
2. **One URL box for everything else.** A pasted link is routed automatically:
   native RSS/Atom feed, podcast feed, YouTube channel/playlist/video, X
   account/post, Substack publication/post, FT and WSJ section feeds and
   articles, Apple Podcasts pages, Telegram/Bluesky/Reddit and other sites via
   a private rss-bridge instance, plain article pages. Feed type is sniffed,
   never asked.
3. **Premium sources we pay for** (Financial Times, Wall Street Journal, paid
   Substacks, X, YouTube sign-in) are fetched with credentials the team
   pastes into a Sign-ins card. Cookies and tokens live in the database and
   never leave the API. Terms-of-service questions are settled by the owner;
   this spec is about mechanics.

Videos: audio content only. A YouTube video becomes a transcript document
(captions first, whisper only when ASR is enabled). No frame analysis.

Design references (§) are docs/company-graph-design.md. Copy rules for every
user-facing string: build-spec-v2-frontend.md §7. The rss-bridge research
brief that informed §5 is docs/rss-bridge-brief.md.

Hard facts from the probes on 2026-08-18 that shape this spec:

- `yfinance` (v1.6) resolves `Ticker(t).info` in about a second and returns
  `longName`, `sector`, `industry`, `fullExchangeName`, `country`, `currency`,
  `quoteType`, `website`; unknown symbols return an all-null dict (Yahoo logs a
  404). `yf.Search(q).quotes` gives name search with symbol/exchange/type.
  Its dependency tree adds pandas, numpy, curl_cffi (about 100 MB RSS).
- YouTube: a channel `@handle` page resolves to `channel_id` without sign-in
  (fetch with the SOCS consent cookie, read `<link rel="canonical">` or
  `"externalId"`), and `https://www.youtube.com/feeds/videos.xml?channel_id=UC…`
  is a public feed. Per-video access from a server IP is bot-walled: every
  yt-dlp player client and `youtube-transcript-api` fail with "Sign in to
  confirm you're not a bot". Video captions and audio therefore need a
  cookies.txt export from a signed-in Google account (yt-dlp `cookiefile`).
  yt-dlp 2026.07 wants `yt-dlp-ejs` plus a JS runtime (deno, node, bun,
  quickjs) for full format access; captions do not need it, audio may.
- FT: any section or topic slug with `?format=rss` (`/markets?format=rss`,
  `/companies/technology?format=rss`, `/lex?format=rss`, `/semiconductors?format=rss`;
  `/rss/home` 301s to `/rss/home/international`, `/rss/companies` 301s to
  `/companies?format=rss`) is a public feed: 25 items, headline + a teaser of
  at most 120 chars, `<guid>` = the article UUID, `<ttl>15</ttl>`. Article
  HTML is behind a Cloudflare JS challenge (`cf-mitigated: challenge`, 403)
  for datacenter IPs regardless of headers, TLS impersonation or cookies: the
  edge answers before the origin ever reads `FTSession`. FT's login page is
  hCaptcha-gated. `https://session-next.ft.com/<FTSession_s>` answers 200 or
  404 "Session … is not valid" without the challenge, so it is the right
  liveness check for a pasted cookie. Full text for servers is sold as the
  FT Content API (`GET https://api.ft.com/content/<uuid>`, `X-Api-Key`,
  `bodyXML` + annotations with FIGI/LEI; "Datamining licence", royalty,
  content.licensing@ft.com); reader subscriptions do not include it.
- WSJ: the widely cited `feeds.a.dj.com/rss/<name>.xml` feeds return 200 but
  froze on 27 Jan 2025. The live host is
  `https://feeds.content.dowjones.io/public/rss/<SLUG>` (no `.xml`; verified
  slugs RSSMarketsMain, RSSWorldNews, WSJcomUSBusiness, RSSWSJD, RSSOpinion,
  socialeconomyfeed, RSSPersonalFinance, RSSLifestyle, RSSArtsCulture,
  RSSMarkets), headline + one-line summary. Article HTML sits behind DataDome
  (401 + JS challenge, `x-datadome: protected`) which rejects every non-browser
  client at the CDN edge before entitlement, so subscriber cookies in a
  server fetch cannot help. Two server-reachable extras for later: the Dow
  Jones GraphQL gateway (`shared-data.dowjones.io/gateway/graphql`, needs
  `apollographql-client-name: wsj-mobile-android-release` and a UA) lists
  editions with `originId`, `articleIsFree` and, for each article, a public
  full narration MP3 (`readToMe.audioUrl` on m.wsj.net) that our ASR path
  could transcribe; and the licensed Factiva Retrieval API
  (`POST api.dowjones.com/content/gen-ai/retrieve`, sources `WSJO`/`J`).

  Consequence for this build: FT and WSJ section feeds work today
  (headline + teaser, poll every 15 min); the credential path is built and
  degrades honestly (a walled fetch marks the source or link "Sign-in
  needed" and never ingests the challenge page), but full text from the
  server needs either a licence (FT Content API, Factiva) or a browser-based
  fetcher on a non-datacenter egress, both out of scope here (§11).
- Substack: `<origin>/feed` carries `<generator>Substack</generator>` and full
  `content:encoded` for free posts, on custom domains too. The public JSON
  API `<origin>/api/v1/archive?sort=new&limit=N` lists posts with `audience`
  (`everyone` / `only_paid` / `founding`), `slug`, `canonical_url`, and
  `<origin>/api/v1/posts/<slug>` returns `body_html` (full text for the
  subscriber's session, preview otherwise).
- Bluesky profiles have native RSS at `https://bsky.app/profile/<handle>/rss`.

---

## 1. Schema — schema/010_sources_v4.sql

```sql
-- watchlist: resolved company facts (yfinance first, SEC registry fallback)
alter table watchlist
    add column name        text,
    add column industry    text,
    add column exchange    text,
    add column country     text,
    add column currency    text,
    add column quote_type  text,
    add column website     text,
    add column cik         bigint,
    add column resolver    text,           -- 'yfinance' | 'sec' | 'manual' | 'seed'
    add column resolved_at timestamptz;

-- sources: per-source health surfaced in the UI
alter table source add column last_error text;
alter table source add column last_error_at timestamptz;

-- one-off links pasted into the sources page (articles, videos, posts)
create table link_queue (
    link_id     uuid primary key default gen_random_uuid(),
    url         text not null,
    kind        text not null check (kind in
                ('article','substack_post','youtube_video','x_post')),
    site        text,                              -- ft | wsj | substack | youtube | x | null
    source_id   uuid references source,            -- link:<host> bucket
    status      text not null default 'queued'
                check (status in ('queued','done','duplicate','blocked','failed')),
    attempts    int not null default 0,
    event_id    uuid references event,
    title       text,
    error       text,
    added_by    text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create unique index link_queue_url_uniq on link_queue (url);
create index on link_queue (status, created_at);

-- credentials for premium sites; value never leaves the API
create table credential (
    site          text primary key check (site in ('ft','wsj','substack','x','youtube')),
    kind          text not null check (kind in ('cookies','bearer')),
    value         text not null,
    note          text,
    updated_at    timestamptz not null default now(),
    checked_at    timestamptz,
    check_ok      boolean,
    check_message text
);
```

`source.connector` gains values `youtube`, `x`, `bridge`, `link` (001's check
is a comment, not a constraint; nothing to alter). `source.config` (jsonb,
from 004) carries per-kind settings, listed under each connector below.
`source.status` keeps its 001 domain: `active` / `demoted` (paused) /
`dropped` (removed from the page, never polled, rows kept for lineage).

---

## 2. Watchlist — graph/watchlist.py (new)

```python
def resolve(con, ticker: str) -> dict | None
```
Uppercase and strip the input (Yahoo symbols keep their suffix and dash:
`0700.HK`, `BRK-B`). Order:

1. `yfinance.Ticker(t).info` (10 s budget, in a worker thread so a hung Yahoo
   call cannot pin the API). Accept when `quoteType` in `('EQUITY','ETF')`
   and a name is present. Fields: `name` = `longName` or `shortName`,
   `sector`, `industry`, `exchange` = `fullExchangeName` or `exchange`,
   `country`, `currency`, `quote_type`, `website`. `resolver='yfinance'`.
2. Else `registry_sec` by ticker: `name` = title, `resolver='sec'`.
3. Else `None`.

Always join `registry_sec` for `cik` when the ticker is there (US filings
keep flowing for those rows; other rows are skipped by edgar.poll exactly as
today, "not in SEC registrant list").

```python
def search(q: str, limit=8) -> list[dict]     # yf.Search: symbol, name, exchange, type
def upsert(con, ticker, info, added_by, sector_override=None) -> None
```
`upsert` writes all resolved columns plus `resolved_at=now()`; on conflict
it sets `active=true` and refreshes the resolved columns (`sector` only if
the override or the resolver gave one). Never touches `added_at`.

yfinance failures (network, rate limit, parse) are caught and logged; they
degrade to step 2, never 500. `yfinance` is imported lazily inside the
function so tests and the worker do not pay the pandas import.

`graph add-ticker TICKER [--sector S]` calls the same path.

`cli.seed(sources=True)` (dev only) keeps writing `sector` from the ref file
and sets `resolver='seed'`; no network in seed.

Downstream: `WATCHLIST_SQL` company = `coalesce(w.name, r.title)`; edgar's
`sector_of` unchanged; quality/funnel joins unchanged.

---

## 3. Fetching with credentials — graph/fetch.py, graph/credentials.py (new)

### 3.1 credentials.py

```python
SITES = {
  "ft":       {"label": "Financial Times", "kind": "cookies",
               "help": "Paste a cookies.txt export for ft.com from a signed-in browser."},
  "wsj":      {"label": "Wall Street Journal", "kind": "cookies",
               "help": "Paste a cookies.txt export for wsj.com from a signed-in browser."},
  "substack": {"label": "Substack", "kind": "cookies",
               "help": "Paste a cookies.txt export for substack.com. Paid posts need it."},
  "x":        {"label": "X", "kind": "bearer",
               "help": "Paste the bearer token from the X developer portal."},
  "youtube":  {"label": "YouTube", "kind": "cookies",
               "help": "Paste a cookies.txt export for youtube.com from a signed-in browser. Needed for transcripts."},
}
def get(con, site) -> dict | None            # row or None (value included; internal use)
def set(con, site, value, note=None)         # validates format for the kind
def delete(con, site)
def cookie_jar(value: str) -> dict[str, str] # Netscape cookies.txt OR "a=b; c=d" header text
def status(con) -> list[dict]                # per site: label, kind, set, updated_at, checked_at, check_ok, check_message, help
def record_check(con, site, ok, message)
```
Format validation on set: cookies must parse to at least one cookie; a
bearer token is a single non-empty line. Cookie parsing accepts both the
Netscape 7-column format (comment lines, `#HttpOnly_` prefixes) and a raw
`Cookie:` header line. Domain and path columns are kept when present so the
jar can be filtered per host; the raw header form applies to the site's
hosts (`SITE_HOSTS = {"ft": ("ft.com",), "wsj": ("wsj.com","dowjones.com"),
"substack": ("substack.com",), "youtube": ("youtube.com","google.com")}` and
any Substack custom domain the fetcher is asked for).

### 3.2 fetch.py

```python
class SignInNeeded(Exception): site, detail
class Response: status, url, text, bytes, headers

def get(url, *, site=None, con=None, timeout=30, max_bytes=2_000_000,
        headers=None, allow_wall=False) -> Response
```
- Uses `curl_cffi.requests` with `impersonate="chrome"` when importable
  (it arrives with yfinance), else `requests` with the browser UA from
  podcast.py. Streams and stops at `max_bytes`.
- When `site` is set and a credential exists, attaches its cookies (filtered
  by host when domains are known). Never logs cookie values.
- Wall detection (only when a site is set), raises `SignInNeeded` unless
  `allow_wall`:
  - `ft`: status 403, or status 200 with none of the article-body markers
    (`"articleBody"`, `article__content-body`, `n-content-body`) and one of
    (`barrier`, `subscribe`, `Sign in`) in the first 200 KB.
  - `wsj`: status 401/403, or body containing `captcha` / `datadome` /
    `px-captcha` / `Access Denied` without an article body marker.
  - `substack`: only when the JSON API says a paid post came back as a
    preview (`body_html` under 1,500 chars for `audience != 'everyone'`).
  - `youtube` / `x`: handled by their clients (§6), not here.
- `head(url)`: GET with `max_bytes=512_000`, used by the router.

### 3.3 extract — graph/connectors/manual.py additions

`extract_article(html, url) -> (title, published, body)`:
1. JSON-LD `NewsArticle` / `Article` with `articleBody` (also inside
   `@graph`): use it, `title` from `headline`, `published` from
   `datePublished`.
2. Site selectors: ft `.article__content-body, #article-body`; wsj
   `section[data-type="article-body"], .article-content, [class*="ArticleBody"]`;
   substack `.body.markup, .available-content`.
3. Fallback: existing `extract()` heuristic.

`extract()` stays; rss.py and manual uploads switch to `extract_article`.

---

## 4. URL router — graph/router.py (new)

```python
@dataclass
class Resolution:
    kind: str          # feed | podcast | youtube | x | bridge | link | unsupported
    connector: str | None
    label: str         # user-facing type: "News feed", "Podcast", "Substack",
                       # "YouTube channel", "YouTube playlist", "X account",
                       # "FT feed", "WSJ feed", "Telegram" (bridge), "Article", ...
    url: str           # normalized input
    name: str | None   # suggested source name
    feed_url: str | None
    site: str | None   # ft | wsj | substack | youtube | x | None
    config: dict       # goes to source.config
    one_off: bool      # True -> link_queue, not a source
    link_kind: str | None
    credential: dict | None   # {"site", "set": bool} when the kind needs one
    message: str | None       # unsupported reason or a note

def resolve(con, raw_url: str) -> Resolution
```

`normalize(url)`: add `https://` when no scheme, lowercase host, drop
fragments, drop `utm_*`, `fbclid`, `gclid`, `mod`, `syn-*`, `ref` query
params, strip trailing `/`. `x.com` and `twitter.com` and `mobile.twitter.com`
collapse to `x.com`; `youtu.be/<id>` becomes `youtube.com/watch?v=<id>`;
`m.youtube.com` becomes `www.youtube.com`.

Rules, in order; the first match wins:

| # | Match | Result |
|---|-------|--------|
| 1 | host `www.youtube.com` path `/watch` with `v` | one_off `youtube_video`, site youtube |
| 2 | youtube `/shorts/<id>` | same as 1 |
| 3 | youtube `/playlist?list=<id>` | kind youtube, config `{playlist_id}`, feed_url `feeds/videos.xml?playlist_id=` |
| 4 | youtube `/channel/<UC…>` | kind youtube, config `{channel_id}` |
| 5 | youtube `/@handle`, `/c/<name>`, `/user/<name>` (with optional `/videos` etc.) | resolve channel_id (§6.1); kind youtube |
| 6 | host `x.com` path `/<user>/status/<id>` | one_off `x_post`, site x |
| 7 | host `x.com` path `/<user>` (not `home`, `search`, `i`, `explore`) | kind x, config `{username}` |
| 8 | host `www.ft.com` path `/content/<uuid>` | one_off `article`, site ft |
| 9 | host `www.ft.com` path `/rss/*` or query `format=rss` | kind feed, connector rss, site ft, label "FT feed" |
| 10 | host `www.ft.com` any other path (section) | kind feed, feed_url `<path>?format=rss` verified by a fetch (must parse as RSS), site ft |
| 11 | host `feeds.content.dowjones.io` (live) or `feeds.a.dj.com` (frozen legacy; remapped to the live host, same slug without `.xml`) | kind feed, connector rss, site wsj, label "WSJ feed" |
| 12 | host `www.wsj.com` path `/articles/*` or `/finance/*`,`/business/*`,`/tech/*`,`/economy/*`,`/politics/*`,`/world/*`,`/opinion/*` ending in a slug with a hex/word id | one_off `article`, site wsj |
| 13 | host `www.wsj.com` section root (`/`, `/finance`, `/business`, `/tech`, `/economy`, `/world`, `/opinion`, `/markets`, `/personal-finance`, `/lifestyle`, `/arts-culture`) | kind feed via `WSJ_SECTION_FEEDS` on the live host (markets, finance→RSSMarketsMain; business→WSJcomUSBusiness; economy→socialeconomyfeed; tech→RSSWSJD; world, `/`→RSSWorldNews; opinion→RSSOpinion; personal-finance→RSSPersonalFinance; lifestyle→RSSLifestyle; arts-culture→RSSArtsCulture), site wsj |
| 14 | host `podcasts.apple.com` with `/id<digits>` | iTunes lookup `https://itunes.apple.com/lookup?id=<n>&entity=podcast` → `feedUrl` → kind podcast |
| 15 | host `bsky.app` path `/profile/<handle>` | kind feed, feed_url `<url>/rss` |
| 16 | host `t.me` path `/s/<channel>` or `/<channel>` | kind bridge, TelegramBridge (§5) |
| 17 | host `www.reddit.com` / `reddit.com` path `/r/<sub>` | kind feed, feed_url `https://www.reddit.com/r/<sub>/.rss` (browser UA); if that fetch is not RSS, bridge RedditBridge |
| 18 | host `github.com` path `/<owner>/<repo>` | kind feed, feed_url `<url>/releases.atom` |
| 19 | host `medium.com` `/@user` or `<pub>.medium.com` | kind feed, feed_url `https://medium.com/feed/@user` or `<origin>/feed` |
| 20 | Substack: host ends with `.substack.com`, or the origin's `/feed` answers with header `x-cluster: substack` (every publication, custom domains included) or `<generator>Substack</generator>`, or `/api/v1/archive?limit=1` returns JSON with `publication_id`. Path `/p/<slug>` → one_off `substack_post`, site substack. Otherwise kind feed, connector rss, site substack, feed_url `<origin>/feed`, config `{substack: {origin}}` |
| 21 | Fetch the URL (`fetch.head`). Content-type XML or body starts with `<?xml`/`<rss`/`<feed`: parse. Items with `<enclosure type="audio/…">` → kind podcast; else kind feed (Atom `<entry>` supported by the parser: rss.items must accept Atom `<entry><title/><link href/><updated/>`) |
| 22 | HTML: `<link rel="alternate" type="application/rss+xml|atom+xml" href>` → resolve href, re-run rule 20/21 on it (podcast/feed/substack) |
| 23 | HTML with `og:type=article`, or JSON-LD `NewsArticle`/`Article`, or `<article>` with more than 1,500 chars of `<p>` text → one_off `article`, site by host (ft/wsj) else null |
| 23b | Well-known feed paths when the host answered but nothing above matched (front page walled or JS-only): `<url>/feed`, `<origin>/feed`, `/rss`, `/feed.xml`, `/index.xml` (WordPress, Ghost, Jekyll, Hugo); a missing path is a normal answer, not "unreachable" |
| 24 | Bridge: `GET {CAF_BRIDGE_URL}/?action=findfeed&format=Json&url=<url>` → first result → kind bridge, config `{bridge: {name, params}}` (§5.3) |
| 25 | unsupported: message "No feed found at this address. Paste an article link to add it once, or a feed URL." |

Every network step has an 8 s timeout and a 512 KB cap; a network failure
falls through to the next rule, and the final failure produces `unsupported`
with the message "Could not reach that address." Rules 1-19 need no network
except where a resolution step is named.

`name` suggestions: feed `<title>` (or channel/publication name), else the
host without `www.`; podcasts get `podcast:` prefix as today; YouTube
"YouTube: <channel>"; X "@user"; bridge "<Bridge name>: <first param>".

`credential`: set for site in (ft, wsj, substack, x, youtube); `set` from
credentials.status. Substack: only flagged when at least one recent post has
`audience != 'everyone'` (one archive call), otherwise `credential=None`.

Duplicate detection at create time: same `feed_url` (feed/podcast/bridge) or
same `config.channel_id`/`playlist_id`/`username` among non-dropped sources
→ 409 "Already added as <name>."

---

## 5. Private rss-bridge — service, client, connector

Facts below were verified against the rss-bridge source (release 2025-08-05)
and a local run of the pinned image on 2026-08-18; the long form with
citations is docs/rss-bridge-brief.md.

What rss-bridge is for us, and what it is not:

- It is a PHP service that turns sites without feeds into feeds. Requests
  are GET-only: `?action=findfeed&url=…&format=Json` (which enabled bridges
  claim a URL; 200 + JSON array, or 404 + `{"message": "No bridge found for
  given url"}`), `?action=display&bridge=<Class>&format=Json&<params>` (the
  feed as JSON Feed v1), `?action=list` (every bridge on disk with
  `status: active|inactive`, unaffected by the whitelist), `?action=health`
  (`{"code":200,"message":"all is good"}`).
- `findfeed` only knows bridges that implement `detectParameters()`, about
  43 of 546. Substack, YouTube, TwitterV2, Bluesky, FT and WSJ do not, so it
  can not classify the URLs we care most about; the router (§4) owns those
  and asks the bridge last (rule 24). `?action=detect` is worse: it answers
  HTTP 200 with an HTML error page when nothing matches. Never use it.
- Enabled bridges: `RSSBRIDGE_system_enabled_bridges` (comma-separated
  class names) is the whitelist; a display call to anything else is 400
  "This bridge is not whitelisted". Env vars are `RSSBRIDGE_<Section>_<key>`
  and beat `config.ini.php` and `whitelist.txt`; per-bridge secrets are
  `RSSBRIDGE_<BridgeClass>_<option>` (`RSSBRIDGE_SubstackBridge_sid`,
  `RSSBRIDGE_TwitterV2Bridge_twitterv2apitoken`), instance-global, read at
  request time from the container's env, so changing one means recreating
  the container. Bridge secrets can never be passed per request.
- Display output caches for the bridge's `CACHE_TIMEOUT` (1 h default);
  errors cache 5 to 15 minutes; `&_cache_timeout=0` forces a refresh once
  `cache.custom_timeout` is on. `error.output=http` makes failures HTTP
  errors instead of a fake feed item. `http.timeout` defaults to 5 s (too
  short); nginx inside the image caps a request at 45 s. Do not set
  `http.useragent`: the image LD_PRELOADs curl-impersonate (Chrome TLS
  fingerprint) and a custom UA switches it off. Custom `*Bridge.php` files
  dropped into a `/config` mount are copied into the app at container start.
- Substack: `SubstackBridge` is one authenticated GET of `<pub>/feed` with
  `Cookie: substack.sid=<sid>; substack.lli=1; ab_experiment_sampled=%22false%22`
  sent to the publication host (custom domains included; one backend). The
  same trick in Python is what §6.4 does, with the sid pasted on the Sign-ins
  card and no container restart. So Substack is routed natively (rule 20),
  and the bridge's `SubstackBridge` stays enabled only for sources added
  directly against it (`CAF_BRIDGE_SUBSTACK_SID` in the server `.env`).
  Substack detection that survives custom domains: the response header
  `x-cluster: substack` (with `x-sub: <slug>`), or `<generator>Substack</generator>`
  in `/feed`, or `/api/v1/archive?limit=1` returning JSON with `publication_id`.
- X: `TwitterBridge` and `FarsideNitterBridge` are dead (verified: the
  GraphQL query id 404s; farside/nitter/xcancel are gone or bot-walled).
  `TwitterV2Bridge` works and needs the same X API v2 bearer token our
  x.py (§6.2) uses; X bills per read (about $0.005 per post, deduplicated
  per 24 h, so polling cadence is nearly free and new-post volume is the
  cost). We call the API directly (credential on the Sign-ins card, our own
  429/402 handling) and leave the Twitter bridges disabled.
- FT and WSJ: no bridge exists. One could be written in ~40 lines on the
  `EconomistBridge` pattern (`FeedExpander` + `parseItem()` refetching each
  article with `Cookie: <config cookie>`; config `[FinancialTimesBridge]
  cookie = "…"`, env `RSSBRIDGE_FinancialTimesBridge_cookie`), mounted via
  `/config` and whitelisted. The authentication would be exactly the browser
  session cookies we already collect. It buys nothing over §3.2 (`curl_cffi`
  is the same curl-impersonate) and loses the UI-managed credential and the
  per-source wall reporting, so FT/WSJ stay in our fetcher.
- YouTube: `YoutubeBridge` scrapes channel/playlist pages; the native
  `feeds/videos.xml` is public and simpler, so rule 3-5 use it. Bluesky has
  native RSS (`/profile/<handle>/rss`); Telegram, Reddit, Mastodon and
  Threads are the bridges `findfeed` actually resolves for us.

### 5.1 Compose service (in ~/CAF/docker-compose.yml, done)

`caf-rss-bridge`: `rssbridge/rss-bridge:2025-08-05` (the `stable` release;
`latest` tracks master), `restart: unless-stopped`, `mem_limit: 256m`, no
`ports`, no nginx location. Env:

```
RSSBRIDGE_system_enabled_bridges: SubstackBridge,TelegramBridge,RedditBridge,
  BlueskyBridge,MastodonBridge,ThreadsBridge,YoutubeBridge,
  YouTubeFeedExpanderBridge,CssSelectorBridge,CssSelectorFeedExpanderBridge,
  XPathBridge,FeedMergeBridge,SitemapBridge,EconomistBridge,
  EconomistWorldInBriefBridge
RSSBRIDGE_error_output: http          RSSBRIDGE_http_timeout: 25
RSSBRIDGE_http_retries: 1             RSSBRIDGE_cache_type: file
RSSBRIDGE_cache_custom_timeout: true
RSSBRIDGE_authentication_token: ${CAF_BRIDGE_TOKEN:-}     # empty = no check
RSSBRIDGE_SubstackBridge_sid: ${CAF_BRIDGE_SUBSTACK_SID:-}
```
Health check (verified in the container): `php -r 'exit(strpos((string)
@file_get_contents("http://127.0.0.1/?action=health&token=".getenv(
"RSSBRIDGE_authentication_token")), "all is good") !== false ? 0 : 1);'`.
Vault web + worker get `CAF_BRIDGE_URL: http://caf-rss-bridge` and
`CAF_BRIDGE_TOKEN: ${CAF_BRIDGE_TOKEN:-}`. `.env.example` documents both new
variables. Twitter bridges are deliberately absent from the list.

### 5.2 graph/bridge.py (client, done)

```python
def enabled() -> bool                          # CAF_BRIDGE_URL set
def findfeed(url) -> list[dict]                # [{bridge, params, url, name}], best first, [] on 404
def display(bridge, params) -> dict            # {title, url, items:[{title, url, published,
                                               #   content_html, author, uid}]}
def list_bridges() -> list[dict]
def health() -> bool                           # ?action=health
def display_url(bridge, params) -> str         # relative, token-free, for source.config
```
Timeouts 20 s; bridge HTTP errors raise `BridgeError(status, text[:200])`.
The token (when set) rides on every call and is never stored in
`source.config`. Verified against the live container: findfeed for
`t.me/s/durov` → TelegramBridge `{username: durov}`, for `reddit.com/r/investing`
→ RedditBridge `{context: single, r: investing}`; display returns 20 items
with `author` flattened from `{name}`.

### 5.3 Connector — graph/connectors/bridge.py

Sources with `connector='bridge'`, config `{bridge: {name, params}}`. Poll:
`display()` → for the newest N items (`CAF_BRIDGE_ITEMS_PER_CYCLE`, default
5): dedup on `meta->>'item_url'`; body = HTML-to-text of `content_html`
(BeautifulSoup, paragraphs; if under 400 chars and `url` is set, fetch the
page with `fetch.get` and `extract_article`, mark `thin` if still short);
compose the standard header (`source_type: article`, `origin: <name>`),
ingest connector `bridge`, meta `{feed, title, item_url, bridge}`. Update
`last_polled`; on `BridgeError` set `last_error` and count `errors`.

---

## 6. New connectors

### 6.1 graph/connectors/youtube.py

Channel/playlist sources: `connector='youtube'`, config
`{channel_id | playlist_id, handle?, title}`. Poll the native feed
(`videos.xml`), newest `CAF_YT_PER_CYCLE` (default 3) entries per source not
yet in `event.meta->>'video_id'`, then `transcript(video_id, con)`:

1. Credential `youtube` → temp Netscape file → yt-dlp `cookiefile`. Without a
   credential, still try once (some IPs are not walled); on the bot wall,
   raise `SignInNeeded('youtube')`.
2. `YoutubeDL({"skip_download": True, "quiet": True, "cookiefile": ...,
   "extractor_args": {"youtube": {"player_client": ["web", "mweb"]}}}).extract_info(url)`
   → prefer `subtitles['en'|'en-US'|'en-GB']`, else
   `automatic_captions['en'|'en-orig']`; fetch the `json3` variant, join
   `events[].segs[].utf8`, collapse whitespace. `transcript_source='captions'`.
3. No captions and ASR on (`podcast.asr_engine() != 'off'`): download
   `bestaudio[ext=m4a]/bestaudio` to a temp dir (yt-dlp `format`,
   `outtmpl`), `podcast.transcribe(path)`, `transcript_source='whisper'`.
   Videos over `CAF_YT_MAX_MINUTES` (default 120) are skipped with a reason.
4. No captions and ASR off: skip; count `no_transcript`, remember the video
   in `source.config.skipped[]` so it is not retried every cycle.

Document: `# title / # source_type: video / # published / # channel /
# duration / ---` transcript. Meta `{video_id, item_url, channel, title,
duration_s, transcript_source}`. Errors per video are counted and logged;
`SignInNeeded` sets `source.last_error` ("Sign-in needed") and stops the
source for this cycle.

Channel-id resolution for the router: fetch the channel page with cookies
`SOCS=CAI`, `CONSENT=YES+1`, read `<link rel="canonical" href=".../channel/UC…">`
or `"externalId":"UC…"`, and `<meta property="og:title">` (or `<title>`) for
the name. Fallback: yt-dlp `extract_flat` with `playlist_items: "0"`.

Image: the Dockerfile copies `/usr/local/bin/node` from the node build stage
into the python image so yt-dlp has a JS runtime (`js_runtimes: ["node"]`),
and installs `yt-dlp` and `yt-dlp-ejs`. ffmpeg is not installed; audio is
fetched as m4a and handed to whisper directly.

### 6.2 graph/connectors/x.py

Bearer token from credential `x`. Account sources: `connector='x'`, config
`{username, user_id, since_id}`. Poll:

- `GET https://api.x.com/2/users/by/username/{username}` once (cache `user_id`).
- `GET /2/users/{id}/tweets?max_results=100&exclude=retweets,replies&
  tweet.fields=created_at,text,note_tweet,public_metrics,entities&since_id=…`
- New posts this cycle → one event per poll: title `@user, N posts, <date>`,
  `source_type: social`, `origin: x.com/@user`, body = one block per post
  (`[<created_at>] <text or note_tweet.text>` with expanded t.co URLs, then
  the post URL). Meta `{username, post_ids: [...], newest_id}`. Update
  `since_id`. Nothing new → no event.
- 429 or 402: set `last_error` ("Rate limited, retry next cycle" /
  "X plan does not allow this endpoint"), stop polling X for the cycle. 401:
  `SignInNeeded('x')`.
- Single post (`x_post` link): `GET /2/tweets/{id}?tweet.fields=…&expansions=author_id&user.fields=username`
  → one event, `source_type: social`.

### 6.3 graph/connectors/links.py — the one-off queue

`process(con, limit=20)`: `select … from link_queue where status in
('queued','failed') and attempts < 3 order by created_at limit %s`. Per row:

- `article`: `fetch.get(url, site=row.site)` → `extract_article` → ingest
  into source `link:<host>` (connector `link`, created on demand, `is_internal
  false`), meta `{item_url, title, site}`; `published_at` from the extractor.
- `substack_post`: `<origin>/api/v1/posts/<slug>` with the substack cookie
  → `title`, `post_date`, `body_html` → text; `SignInNeeded` if the post is
  `only_paid` and the body is a preview.
- `youtube_video`: youtube.transcript → same document shape as 6.1, into the
  `link:youtube.com` bucket source.
- `x_post`: x.post → event.
- Outcomes: `done` (event_id set), `duplicate` (ingest returned is_new
  False), `blocked` (SignInNeeded: error text "Sign-in needed for FT",
  attempts not incremented), `failed` (error text, attempts+1).
- `process_one(con, link_id)` runs one row synchronously; the API calls it
  right after insert for non-media kinds so the page can show the outcome
  immediately. Media (`youtube_video`) always waits for the worker.
- On credential save (§7), blocked links for that site go back to `queued`.

### 6.4 rss.py changes

- Items parser accepts Atom entries and `content:encoded` (kept as
  `content_html`).
- Per source `site` from config: item pages fetched via `fetch.get(url,
  site=site)`; `SignInNeeded` → `blocked` count and `source.last_error`.
  For FT and WSJ the wall is the edge, so the item is ingested from the
  feed's own headline + teaser (`meta.thin`), the source keeps going, and
  `last_error` reads "Sign-in needed" without a saved cookie or "Headlines
  only, article pages are blocked" with one. Substack and other sites stop
  for the cycle on the first wall (the cookie really does unlock them).
- Substack sources (`config.substack.origin`): use the feed's
  `content:encoded` when it is over 1,500 chars, else the JSON post API with
  the cookie (paid posts).
- `extract_article` replaces `extract` for item pages.
- Podcast sniff at add time means a feed with enclosures always lands in the
  podcast connector, so rss.py never sees enclosure-only items.

### 6.5 Worker cycle (cli.py)

`edgar → podcast → rss → youtube → x → bridge → links → triage → …`. Same
`stage()` wrapper; each new stage returns its counts dict. `cmd_run` gains
the same four stages. All four are heuristic (no LLM) and never pause.

---

## 7. API (webapp.py)

- `GET /api/sources` →
  ```
  { watchlist: [{ticker, name, sector, industry, exchange, country, active, events, cik}],
    sources:   [{source_id, name, connector, label, site, url, feed_url, status,
                 last_polled, last_error, events, credential: {site, set} | null}],
    links:     [{link_id, url, title, kind, site, status, error, event_id, created_at}]  // newest 30
    credentials: [{site, label, kind, set, updated_at, checked_at, check_ok, check_message, help}] }
  ```
  `sources` excludes `dropped` and internal buckets (`manual:uploads`,
  `link:*`, `edgar:*`). `label` from a shared `source_label(connector,
  config)` helper (also used by /api/status sources).
- `POST /api/watchlist {ticker, sector?}` → `{ok, ticker, name, sector,
  exchange}`; 400 "Unknown ticker T. Yahoo Finance and the SEC registry do
  not list it."
- `GET /api/watchlist/search?q=` → `[{symbol, name, exchange, type}]` (empty
  for q under 2 chars; yfinance errors → empty list, never 500).
- `POST /api/watchlist/{ticker}/toggle` unchanged.
- `POST /api/sources/resolve {url}` → Resolution as JSON. 400 on an empty
  or non-URL string.
- `POST /api/sources {url, name?}` → resolves; `one_off` → insert
  `link_queue` (added_by web) then `links.process_one` for non-media kinds →
  `{ok, kind: "link", link: {...row}}`; else insert source (connector,
  name, url = feed_url or url, config, status active, added_by web) →
  `{ok, kind: "source", source: {...row}}`. 409 on duplicate; 400 with the
  router message when unsupported.
- `POST /api/sources/{id}/toggle` unchanged; `DELETE /api/sources/{id}` →
  status `dropped`, `{ok}`.
- `POST /api/links/{id}/retry` → status queued, attempts 0, run
  `process_one`, return the row.
- `PUT /api/credentials/{site} {value, note?}` → `{ok, site, set: true}`;
  requeues blocked links and clears `last_error` on sources of that site.
  400 on unknown site or unparseable value.
- `DELETE /api/credentials/{site}` → `{ok}`.
- `POST /api/credentials/{site}/test` → live probe, records the result,
  returns `{ok, message}`. Probes: ft → newest `/rss/home` item fetched
  with cookies, body over 400 chars; wsj → newest RSSMarketsMain item, same;
  substack → `https://substack.com/api/v1/reader/posts?limit=1` returns 200
  JSON (else, if any substack source has a paid post, fetch it); x → `GET
  /2/users/by/username/x` returns 200; youtube → yt-dlp extract_info on
  `dQw4w9WgXcQ` with the cookiefile succeeds. Messages: "Signed in." /
  "Sign-in failed: <reason>."
- `POST /api/feeds` is removed (the CLI's `add-feed` becomes `add-source`).
  `GET /api/status` sources rows gain `label` and `last_error`.
- Credential values are never serialized by any endpoint.

CLI: `graph add-source URL [--name N]`, `graph add-ticker T`, `graph
set-credential SITE (--file cookies.txt | --value TOKEN)`, `graph links`
(process the queue once).

---

## 8. Frontend — frontend/src/pages/sources.tsx (rewrite)

Five cards, in order. Copy rules from v2 §7 apply to every string.

**Watchlist.** Table: Ticker (mono) · Company · Sector · Exchange · Docs ·
Status · Pause/Resume. Add row: one input, placeholder "Ticker or company
name", with a suggestion list under it (GET search, debounced 300 ms, only
when the text has a letter and is 2+ chars; each row: symbol mono, name,
exchange muted; click fills the input with the symbol) and an Add button.
Toast: "Added NVIDIA Corporation (Technology, NasdaqGS)." Errors show the
API detail. Empty: "No tickers on the watchlist."

**Sources.** Table: Name · Type (label) · Last polled · Docs · Status ·
actions (Pause/Resume, Remove). Status badge: active / paused / and when
`last_error` is set a warn badge with the error text ("Sign-in needed",
"Feed unreachable"). Add row: URL input (placeholder "Paste a link: feed,
YouTube, X, Substack, FT, WSJ, article"). On paste, Enter, or 600 ms after
typing stops, call resolve; render one preview line under the input:
`<label>: <name>` plus a muted note (`Videos are transcribed from captions.` /
`Posts are fetched with the X token.` / `Full text needs the FT sign-in.`
when the credential is missing → warn badge "Sign-in needed" linking to the
Sign-ins card) and, for sources, a Name input prefilled with the suggestion.
Add button posts; toast "Added <name>." for sources; for links the row moves
into the Links card and the toast says "Added." / "Queued. The worker fetches
it within 15 minutes." / "Sign-in needed for <site>." / the error. Unsupported
→ the message inline in muted red, Add disabled. Empty table: "No sources
yet."

**Links.** Newest 30 one-off links: title or URL (truncated, mono), type
label, status badge (added / queued / sign-in needed / failed / already in
the vault), muted error, Retry for failed/blocked. Empty: "Paste an article,
video, or post link above to add it once."

**Sign-ins.** One row per site (FT, WSJ, Substack, X, YouTube): label, badge
(not set / set / signed in / failing with check message), help line, then a
paste field (textarea for cookies, input for token) with Save, Test, and
Remove buttons. Never displays the stored value; after save the field
clears. Test toasts the message.

**Upload.** Unchanged.

`lib/api.ts`: types for the new payloads; `frontend/src/pages/claims.tsx`
keeps reading `watchlist[].sector`.

---

## 9. Compose + image

- Vault Dockerfile: `COPY --from=frontend /usr/local/bin/node /usr/local/bin/node`;
  pyproject deps add `yfinance`, `yt-dlp`, `yt-dlp-ejs`, `curl_cffi`.
- ~/CAF/docker-compose.yml: `caf-rss-bridge` per §5.1; `CAF_BRIDGE_URL` on
  both Vault services; `caf-vault-worker` mem cap stays 2g (yt-dlp caption
  fetches are light; whisper fallback is already capped).
- No nginx change (the bridge is not routed).

---

## 10. Tests (all offline; monkeypatch network)

- `tests/test_sources_v4.py`:
  - watchlist.resolve: yfinance mocked (EQUITY info; all-null info → SEC
    fallback → None); POST /api/watchlist with mocked resolver stores name,
    sector, exchange; unknown → 400; search endpoint returns mocked quotes.
  - credentials: parse Netscape + header formats; PUT/DELETE/GET status;
    values absent from every response; test endpoint records check.
  - fetch: wall detection for ft (403) and wsj (captcha body) raises
    SignInNeeded; cookies attached from the credential.
  - router: every rule in §4 has at least one URL fixture (no network for
    1-19; 20-24 with fetch/bridge mocked): youtube watch/shorts/playlist/
    channel/@handle (page HTML fixture with canonical link), x post/account,
    ft content/rss/section, wsj article/section/feeds host, apple podcasts
    lookup, bluesky, telegram → bridge, reddit, github, medium, substack
    domain and custom domain (feed generator), raw RSS, podcast RSS with
    enclosure, HTML autodiscovery, article page, bridge findfeed hit,
    unsupported.
  - POST /api/sources: creates a feed source; creates a youtube source with
    channel_id; one-off article inserts link_queue and processes it (fetch
    mocked) → done with event; blocked path (SignInNeeded) → status blocked;
    duplicate feed → 409; unsupported → 400.
  - links.process retries and attempts accounting; credential save requeues
    blocked.
  - youtube.poll: feed XML fixture + mocked yt-dlp (captions json3 fixture)
    → event with transcript_source captions; no captions + ASR off → skipped.
  - x.poll: mocked API responses → one event with N posts, since_id stored;
    429 → last_error and no event.
  - bridge.poll: mocked display JSON → event; findfeed mocked in router.
- Existing tests: `test_api_feeds_post` is replaced by the sources tests;
  `test_api_watchlist_post` updated for the mocked resolver.
- Frontend: `npm run build` green.

---

## 10b. Browser fetcher assessment (2026-08-18): keep curl_cffi; a real browser is only worth it for FT/WSJ article bodies

Question: should `graph/fetch.py` move from curl_cffi (Chrome TLS fingerprint,
no JS) to CloakBrowser (github.com/CloakHQ/CloakBrowser, a patched Chromium
driven through Playwright)? Tested the same day from a datacenter egress
(Vultr) with the same targets, and had the repo, issues and licence read.

Live results (headless unless noted; small samples, one IP, one day):

| Client | ft.com `/content/<uuid>` | wsj.com article | notes |
|---|---|---|---|
| curl_cffi + browser headers (current) | 403 Cloudflare challenge, 0/6 | 401 DataDome, 0/6 | never reaches the origin |
| CloakBrowser free binary (v145 mac / v146 linux), headless | origin reached 3/5 ("Subscribe to read" paywall page); 2/5 stuck on the challenge for 60 s | DataDome served a JS interstitial that auto-cleared 5/5; article DOM rendered; later pages in the same context 200 in ~3 s | ~2.5 CPU-s per page, 600-900 MB RSS across processes on the Mac (vendor: ~190-280 MB), 625 MB image |
| patchright (open source, Apache-2.0), headless shell | challenge 0/3 | DataDome CAPTCHA page 0/2 | |
| patchright, full Chromium, headed | origin reached 2/2 | DataDome served a CAPTCHA (`captcha-delivery.com/captcha/`) 0/2 after 35 s | |

Reading: FT's wall is "run real JS in a real browser" (any headed browser
passes); WSJ's DataDome scores the client and served CloakBrowser a
non-interactive interstitial but plain Chromium a CAPTCHA, so CloakBrowser's
patches do buy something there. curl_cffi is a strictly worse fit for those
two paths and a strictly better one everywhere else (feeds, JSON APIs,
Substack, generic articles: no binary, ~60 MB, no licence, no phone-home).
YouTube is an account-cookie wall, not a fingerprint wall; a browser does not
change it.

CloakBrowser costs, from the repo/issues: the binary is proprietary (wrapper
MIT), ~200 MB, patch set unpublished, non-auditable by licence, built by an
anonymous six-month-old identity (one committing account, ProtonMail
contact); the current v148+/v150 builds need a key and do licence validation
+ session heartbeats at launch (exit 78 when the licence server is
unreachable, #479; an unclean exit strands the single free seat, #477); the
keyless line is v146 and will not be updated ("not expected to pass current
Cloudflare Turnstile", maintainer, #503, though it reached FT's origin 3/5
and cleared WSJ 5/5 in our test); persistent profiles leak memory (#346);
docker restart hazard with a stale X lock (#283); headed mode via Xvfb is
recommended for aggressive sites. Prices: free (1 session, GitHub sign-in),
$19-$699/mo. Our internal use is permitted by the binary licence.

Decision: curl_cffi stays the fetcher. If FT/WSJ article bodies are wanted
before a licensed API exists, the shape is an opt-in, disposable sidecar,
never a change to fetch.py: a separate compose service (image
`cloakhq/cloakbrowser`, `cloakserve` CDP on an internal port, keyless v146
pinned, `CLOAKBROWSER_AUTO_UPDATE=false`, mem 1 GB, cpus 1.0, no DB access);
Vault connects over CDP only for `site in (ft, wsj)` article fetches, injects
that site's cookies into a fresh context per cycle, one page at a time, at
most a few articles per cycle, hard timeouts, and falls back to the teaser
path on any failure. Untested until real subscriber cookies exist: whether
the entitlement layer then returns full text. Not built.

## 11. Out of scope (recorded, not built)

- Full text for FT and WSJ from the server. Verified walls (Cloudflare
  challenge on ft.com, DataDome on wsj.com) reject non-browser clients at
  the edge, cookies or not. Options, all outside this build: FT Content API
  (Datamining licence), Dow Jones Factiva Retrieval API, a Playwright
  fetcher with a persistent signed-in profile on a non-datacenter egress
  (not on the 2-core box without sizing), or transcribing WSJ's public
  full-article narration MP3s through the ASR path.
- myFT personal RSS (`ft.com/myft/following/<uuid>.rss`, "readable by
  anyone" once enabled under My Account → Contact Preferences): works with
  the plain feed path today; each analyst can paste theirs.
- A feed staleness guard (a feed that answers 200 with a newest item weeks
  old, as the legacy WSJ host does) is a small follow-up in rss.py: set
  `last_error` "No new items since <date>" without stopping the poll.
- Per-user credentials (team of three shares one set).
- X search/keyword streams; only accounts and single posts.
- YouTube frame analysis; podcasts and videos remain audio-only.
