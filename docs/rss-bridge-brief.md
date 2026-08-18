<!-- Produced 2026-08-18 by the rss-bridge research pass (47 documentation
pages read one agent per page, plus targeted source reads of the bridges and
actions that matter to Vault, plus live probes). Companion to
docs/build-spec-v4-sources.md section 5. Verified locally the same day: the
pinned image rssbridge/rss-bridge:2025-08-05 with the compose env in
~/CAF/docker-compose.yml answers ?action=health, enforces the whitelist (400
for TwitterBridge), gates on the token when set (401 without it), and returns
the findfeed / display shapes described here for t.me and reddit URLs. -->

# Design brief: private rss-bridge instance for CAF Vault

Audience: engineers adding rss-bridge to the CAF Vault docker-compose stack and wiring it into the paste-a-URL source router. Everything here is verified against `RSS-Bridge/rss-bridge@master` (commit `cf86742`, `Configuration::VERSION = '2025-08-05'`) plus live probes run 2026-08-18. Anything not verified is marked and repeated in section 7.

---

## 1. rss-bridge in one page

### What it is

A PHP web application that generates feeds for sites that do not publish one. It is a single-entry-point app: every call is `GET /` plus a query string, handled by `index.php`. There is no REST path structure. Per-site logic lives in one PHP class per file under `bridges/` (548 files on master, of which 547 are `*Bridge.php`). Output formatters live in `formats/`, cache backends in `caches/`, request middlewares in `middlewares/`, HTTP controllers in `actions/`.

It is pull-only. There is no internal scheduler. A feed is only as fresh as the last time something called it. Our FastAPI scheduler is the sole clock.

### Request model

Action dispatch, from `lib/RssBridge.php`:

```php
$action = $request->get('action', 'Frontpage');
$actionName = strtolower($action) . 'Action';
$actionName = implode(array_map('ucfirst', explode('-', $actionName)));
```

Action names are case-insensitive. Unknown action returns HTTP 400 with an HTML body `Invalid action`. The complete set of valid `action` values, derived from the files in `actions/`, is: `connectivity`, `detect`, `display`, `findfeed`, `frontpage`, `health`, `list`.

Only GET query parameters exist. `Request::fromGlobals()` sets `$self->get = $_GET;` and there is no body parsing. `SecurityMiddleware` rejects any non-string GET value (for example `?u[]=x`) with 400 `Query parameter "u" is not a string.`

**`?action=display&bridge=<Class>&format=<Fmt>&<bridge params>`** is the feed endpoint. It also accepts `context=<ContextName>` for multi-context bridges. Guard order and status codes, from `actions/DisplayAction.php`:

| Condition | Code | Body |
|---|---|---|
| missing `bridge` | 400 | `Missing bridge name parameter` |
| bridge class not resolvable | 404 | `Bridge not found` |
| missing `format` | 400 | `You must specify a format` |
| bridge not enabled | 400 | `This bridge is not whitelisted` |

Before the bridge sees the input, these keys are stripped:

```php
$remove = ['token','action','bridge','format','_noproxy','_cache_timeout','_error_time','_'];
```

Any remaining query parameter that the bridge has not declared in its `PARAMETERS` const causes `Invalid parameters value(s): ...`. Do not pass stray params.

**`?action=list`** takes no parameters, always returns 200 and `content-type: application/json`, shaped `{"bridges": {"<ClassName>": {...}}, "total": N}`. Per-bridge fields: `status` (`active` or `inactive`), `uri`, `donationUri`, `name`, `icon`, `parameters` (the raw `PARAMETERS` const), `maintainer`, `description`. The whitelist does **not** filter this action; disabled bridges appear with `"status": "inactive"`. The response is large (548 entries) and is not cached by rss-bridge. Cache it on our side.

**`?action=findfeed&url=<urlencoded>&format=<Fmt>`** iterates every *enabled* bridge, calls `detectParameters($url)` on each, and returns every match. Status codes:

| Condition | Code | Body |
|---|---|---|
| `url` missing | 400 | plain text `You must specify a url` |
| `format` missing | 400 | plain text `You must specify a format` |
| no bridge matched | 404 | `{"message": "No bridge found for given url"}`, JSON |
| one or more matches | 200 | bare JSON **array**, JSON content type |
| a bridge's `detectParameters()` throws | 500 | HTML; the whole call dies, no partial results |

Element shape:

```json
{
  "url": "./?action=display&context=By+username&u=nasa&bridge=TwitterBridge&format=Json",
  "bridgeParams": {"context":"By username","u":"nasa","bridge":"TwitterBridge","format":"Json"},
  "bridgeData":   {"u": {"name":"username","value":"nasa"}},
  "bridgeMeta":   {"name":"Twitter","description":"...","parameters":{...},"icon":"..."}
}
```

The `url` field is **relative** (`./?action=display&...`). Join it against our base URL. The bridge class name is in `bridgeParams.bridge`, not at the top level. `findfeed` is never cached (`CacheMiddleware` short-circuits for anything that is not `DisplayAction`), but the 43 `detectParameters()` implementations are pure regex with no network I/O, so the call is cheap.

**`?action=detect&url=<urlencoded>&format=<Fmt>`** does the same scan but stops at the first match and returns `301` with a **relative, query-only** `location: ?action=display&bridge=...&format=...&<params>`. Its three failure paths call `new Response(render(...))` with no status argument, and `Response::__construct(string $body = '', int $code = 200, array $headers = [])`, so **failures return HTTP 200 with an HTML error page**. The published docs claim 400; the docs are wrong. Use `findfeed`, not `detect`.

There is also a `null` versus `[]` asymmetry: `findfeed` skips on `=== null`, `detect` skips on `!$bridgeParams`, so parameterless bridges returning `[]` are silently invisible to `detect`.

**`?action=health`** returns `{"code":200,"message":"all is good"}` with `content-type: application/json`. It is undocumented but real. It sits behind both auth middlewares, so if we enable auth the healthcheck must carry the credential.

**`?action=connectivity`** returns 403 `This action is only available in dev environment!` unless `[system] env` is `dev`.

**`?action=frontpage`** is the default when `action` is absent. It instantiates every enabled bridge and calls `getURI()`, `getName()`, `getIcon()`, `getDescription()`, `getParameters()` on each, per request, uncached. With `enabled_bridges[] = *` that is 548 objects. It is the most expensive endpoint in the app and it is what a bare `GET /` hits.

CLI is also supported: `index.php` runs `parse_str(implode('&', array_slice($argv, 1)), $cliArgs)`, so `php index.php action=display bridge=SubstackBridge format=Json url=...` works via `docker exec`.

### `format=` values and the Json shape

`FormatFactory` discovers formats from `formats/*Format.php`. Valid values: `Atom`, `Html`, `Json`, `Mrss`, `Plaintext`, `Sfeed`. Matching is case-insensitive and tolerates a trailing `Format` or `.php`, so `json`, `JSON`, `JsonFormat` all resolve. An invalid name throws and surfaces as a 500 HTML page.

**Use `format=Json`.** The output is JSON Feed v1, not the internal item array. Top level:

```json
{
  "version": "https://jsonfeed.org/version/1",
  "title": "...", "home_page_url": "...", "feed_url": "...",
  "icon": "...", "favicon": "...",
  "items": [ ... ]
}
```

Per item, every key is `if (!empty(...))`-guarded, so **absent keys mean unset, not null**:

| Internal `FeedItem` field | JSON Feed key |
|---|---|
| `uid` | `id` |
| `title` | `title` |
| `uri` | `url` |
| `content` | `content_html` **or** `content_text` |
| `timestamp` (unix int) | `date_modified`, RFC3339 via `gmdate(DATE_ATOM, ...)` |
| `author` (string) | `author.name` |
| `enclosures` (string[]) | `attachments[].url` plus guessed `attachments[].mime_type` |
| `categories` (string[]) | `tags[]` |
| anything else the bridge set | `_rssbridge.<key>` |

The item structure bridges are expected to produce, from the README:

```php
['uri','title','timestamp' /* unix int */,'author','content','enclosures'=>[],'categories'=>[],'uid']
```

Four traps in that mapping, all in `lib/FeedItem.php` and `formats/JsonFormat.php`:

- `id` is **not** the bridge's raw uid. `setUid()` does `if (preg_match('/^[a-f0-9]{40}$/', $uid)) { $this->uid = $uid; } else { $this->uid = sha1($uid); }`. Stable, so usable as a dedup key, but useless as provenance. If `uid` is empty, `id` falls back to the item URL, then to `sha1($title . $content)`.
- Titles are truncated: `setTitle()` calls `truncate(trim($title))`, and `truncate(string $s, int $length = 150, $marker = '...')`. Never treat an rss-bridge title as the authoritative headline.
- `uri` is dropped unless it matches `#^https?://#i`, so `url` can be missing.
- `is_html()` is `strlen(strip_tags($text)) !== strlen($text)`, so plain text containing a `<` flips to `content_html`. Always read both keys.
- `feed_url` is built from `$_SERVER` via `get_current_url()` and will read `rss-bridge:80` inside our network. Do not propagate it.

### How bridges are enabled

Three mechanisms, applied in this order (later wins):

1. `config.ini.php`, section `[system]`, repeated INI array key:
   ```ini
   [system]
   enabled_bridges[] = *
   ```
   or
   ```ini
   [system]
   enabled_bridges[] = TwitchBridge
   enabled_bridges[] = GettrBridge
   ```
2. `whitelist.txt` in the app root, newline-separated names or the single character `*`. This **overwrites** whatever `config.ini.php` set. The file is no longer shipped in the repo but is still honoured and is still copied by the docker entrypoint.
3. `RSSBRIDGE_system_enabled_bridges=A,B,C` as an environment variable. Comma-split and trimmed. Highest precedence.

The shipped default is `enabled_bridges[] = *`, meaning every one of the 548 bridges is on out of the box.

Name matching is forgiving. `BridgeFactory::normalizeBridgeName()`:

```php
if (preg_match('/(.+)(?:\.php)/', $name, $matches)) { $name = $matches[1]; }
if (!preg_match('/(Bridge)$/i', $name)) { $name = sprintf('%sBridge', $name); }
```

plus a case-insensitive class lookup. So `Youtube`, `youtube`, `YoutubeBridge`, `YoutubeBridge.php` all resolve. A file must be named `*Bridge.php` to be discovered at all. Names that do not resolve are collected into `getMissingEnabledBridges()` and rendered as `Warning : Bridge "%s" not found` on the frontpage.

Effect per action: `findfeed` and `detect` skip non-enabled bridges (so whitelisting gates *discovery*, not only serving); `display` rejects with 400; `list` ignores the whitelist entirely.

### Config precedence

`Configuration::loadConfiguration($customConfig, $env)` applies, in order:

1. `config.default.ini.php` (always parsed; absence throws `The default configuration file is missing`)
2. `config.ini.php` in the app root, parsed with `parse_ini_file(..., true, INI_SCANNER_TYPED)`; a parse error is fatal (`exit("Error parsing config.ini.php\n")`, HTTP 500)
3. a `DEBUG` file in the app root: if it exists **and is empty after trim**, forces `system.env = 'dev'` and `cache.type = 'array'`
4. `whitelist.txt` (overwrites `system.enabled_bridges`)
5. `RSSBRIDGE_*` environment variables, highest precedence

Env variable parsing, `lib/Configuration.php`:

```php
$nameParts = explode('_', $envName);
if ($nameParts[0] === 'RSSBRIDGE') {
    if (count($nameParts) < 3) { continue; }
    $header = $nameParts[1];
    $key = implode('_', array_slice($nameParts, 2));
    $key = strtolower($key);
    if ($key === 'enabled_bridges') { $envValue = explode(',', $envValue); $envValue = array_map('trim', $envValue); }
    if ($envValue === 'true' || $envValue === 'false') { $envValue = filter_var($envValue, FILTER_VALIDATE_BOOLEAN); }
    self::setConfig($header, $key, $envValue);
}
```

Rules that follow:

- Format is `RSSBRIDGE_<section>_<key>`. Prefix must be exactly uppercase `RSSBRIDGE`.
- The section is `$nameParts[1]`, a **single token**. A section name containing an underscore is unreachable. No shipped section has one, but this constrains any bridge class we write.
- Both `setConfig` and `getConfig` lowercase section and key, so casing is irrelevant on both halves.
- Only the literal strings `true` and `false` become booleans. `1`, `yes`, `on` stay strings and will fail the boot-time `is_bool()` validation with a hard 500 and `exit(1)`.
- Fed from `getenv()` with no allowlist, so plain compose `environment:` and `env_file:` both work. This depends on `clear_env = no` in the image's php-fpm pool config.

Boot-time validation (any failure prints `Config [$section] => [$key] is invalid` and exits 1): `system.env` in `{dev,prod}`; `system.enabled_bridges` array; `system.timezone` in `timezone_identifiers_list()`; `proxy.url`/`proxy.name` strings; `proxy.by_bridge` bool; `cache.type` string; `cache.custom_timeout` bool; `authentication.enable` bool; `authentication.username`/`password` strings; `admin.email` valid email if non-empty; `admin.donations` bool; `error.output` in `{feed,http,none}`; `error.report_limit` numeric and >= 1.

### Config sections and keys, quoted literally

```ini
[system]
env = "prod"                    ; must be "dev" or "prod"
enabled_bridges[] = *
timezone = "UTC"
;message = "Hello world"
enable_maintenance_mode = false ; if true, every request returns 503
max_file_size = 10000000        ; simple_html_dom cap, bytes

[http]
timeout = 5                     ; seconds
retries = 1
;useragent = "..."              ; leave commented, see curl-impersonate below
max_filesize = 20               ; max http response size in MB

[cache]
type = "file"                   ; file, sqlite, memcached, array, null
custom_timeout = false          ; allows ?_cache_timeout=N

[FileCache]
path = ""                       ; "" means the repo cache/ folder
enable_purge = true

[SQLiteCache]
file = "cache.sqlite"
enable_purge = true
timeout = 5000                  ; busy wait in ms

[MemcachedCache]
host = "localhost"
port = 11211

[proxy]
url = ""                        ; CURLOPT_PROXY
name = "Hidden proxy name"
by_bridge = false               ; enables per-request &_noproxy=1

[authentication]
enable = false                  ; HTTP basic
username = "admin"
password = ""
token = ""                      ; URL token, ?token=<value>

[error]
output = "feed"                 ; "feed" | "http" | "none"
report_limit = 1

[logging]
;file_path = "/var/log/rss-bridge.log"
;file_level = "INFO"            ; DEBUG, INFO, WARNING or ERROR

[admin]
email = ""                      ; displayed on the main page, visible to everyone
telegram = ""
donations = true

[youtube]
iframe = true
nocookie = false

[webdriver]
selenium_server_url = "http://localhost:4444"
headless = false

[TelegramBridge]
max_pages = 1                   ; 1 page => 20 messages, min=1 max=100

[DiscogsBridge]
personal_access_token = ""
```

Per-bridge credential sections follow the pattern `[<PHP class short name>]`, resolved by `(new \ReflectionClass($this))->getShortName()`. Live examples: `[SubstackBridge] sid`, `[EconomistBridge] cookie`, `[EconomistWorldInBriefBridge] cookie`, `[InstagramBridge] session_id`/`ds_user_id`/`cache_timeout`, `[TwitterV2Bridge] twitterv2apitoken`, `[MastodonBridge] private_key`/`key_id`, `[PixivBridge] cookie`/`proxy_url`, `[FurAffinityBridge] aCookie`/`bCookie`, `[Vk2Bridge] access_token`.

The 12 bridges shipping a `const CONFIGURATION`: `DiscogsBridge`, `EconomistBridge`, `EconomistWorldInBriefBridge`, `FurAffinityBridge`, `GithubReleaseBridge`, `InstagramBridge`, `MastodonBridge`, `PixivBridge`, `SubstackBridge`, `TelegramBridge`, `TwitterV2Bridge`, `Vk2Bridge`.

Bridge-side declaration and read:

```php
const CONFIGURATION = ['sid' => ['required' => false]];
...
$this->getOption('sid')
```

`loadConfiguration()` is called from exactly one place, `actions/DisplayAction.php` line 74, immediately before `collectData()`. So `getOption()` returns `null` if called from a constructor, and `detect`/`findfeed`/`list` never load bridge config. Only `'required'` and `'defaultValue'` are supported per option; there is no type or validation.

### Cache

Two independent layers.

**Response cache** (`middlewares/CacheMiddleware.php`, `actions/DisplayAction.php`). Only applies to `DisplayAction`. Key: `'http_' . json_encode($request->toArray())`, that is the entire query string including `format` and `token`. TTL is the bridge's `CACHE_TIMEOUT` const (default 3600; YouTube 3h; Twitter 15m; Reddit 2h) unless `[cache] custom_timeout = true`, in which case `&_cache_timeout=<seconds>` wins and `0` disables caching for that feed. Honours `If-Modified-Since` and can return 304. **Error responses (400, 403, 404, 429, 500, 503) are cached for `60*5 + rand(1, 60*10)`, that is 5 to 15 minutes.** On 1% of requests it calls `$this->cache->prune()`, described in source as potentially resource intensive.

**Upstream HTTP cache** (`getContents()` in `lib/contents.php`). Key: `implode('_', ['server', $url, $requestBodyHash])`. **TTL is `86400 * 10`, ten days.** It respects `no-cache`/`no-store` in the upstream `Cache-Control` and revalidates with `etag` and `last-modified`, converting a 304 into the cached body. `getSimpleHTMLDOMCached()` uses `'pages_' . $url`.

The upstream cache key **does not include request headers**. An unauthenticated fetch of an article URL and an authenticated fetch of the same URL collide. If anything ever fetches an FT or WSJ article without our cookie, the paywall stub is pinned for ten days and every subsequent authenticated request returns it. Substack happens to send `cache-control: no-cache` on `/feed`, which saves us there, but nothing else guarantees this.

Backends: `file` (default), `sqlite`, `memcached`, `array` (per-request, in memory), `null`. Maintenance CLI: `bin/cache-clear`, `bin/cache-prune`.

Bridge-scoped helpers, keyed by class short name, useful for rotating credentials:

```php
protected function loadCacheValue(string $key, $default = null)
protected function saveCacheValue(string $key, $value, int $ttl = 86400)
```

### HTTP client

`getContents(string $url, array $httpHeaders = [], array $curlOptions = [], bool $returnFull = false)`. Headers are passed as flat `'Name: value'` strings, then normalised into an associative array keyed by header name, so **only one `Cookie:` header per request survives**. `$returnFull = true` returns the `Response` object with `getBody()`, `getHeaders()`, `getHeader($name)`, `getCode()`, which is how a bridge reads `Set-Cookie` back for rotation.

Status handling: 200/201/202 cached and returned; 301/302/303 followed; 304 replaced with the cached body; **anything else throws `HttpException`**. So one 403 on one article aborts the whole feed unless the bridge catches it.

Fixed curl options: `CURLOPT_RETURNTRANSFER`, `CURLOPT_FOLLOWLOCATION => true`, `CURLOPT_MAXREDIRS => 5`, `CURLOPT_ENCODING => ''`, `CURLOPT_PROTOCOLS => CURLPROTO_HTTP | CURLPROTO_HTTPS`. `$curlOptions` is applied last and can override anything.

### Error handling

`[error] output = "feed"` is the default and is a poison pill for an ingestion pipeline. On a bridge failure it returns **HTTP 200** with one synthetic feed item:

```php
$item['title'] = sprintf('Bridge returned error %s! (%s)', $e->getCode(), $uniqueIdentifier);
$item['uri']   = get_current_url();
$item['uid']   = $bridge->getName() . '_' . $uniqueIdentifier;
```

with `$uniqueIdentifier = urlencode((int)(time() / 86400))`, so a **fresh fake item every 24 hours per broken source**. The rendered item also contains a prefilled GitHub issue URL built from `$_SERVER['QUERY_STRING']`, which is how a credential passed as a query parameter would end up one click from a public issue tracker.

Set `output = "http"` so failures return HTTP 500 and our worker can reject them. `RateLimitException` maps to 429 and `HttpException` with code 429 or 503 passes through, both bypassing `report_limit`.

`report_limit` suppresses reporting until an error has occurred N times; counters live in cache under `'error_reporting_' . $bridgeName . '_' . $code` with a 5-day TTL and reset daily.

### Proxy

`[proxy] url` is passed straight to `CURLOPT_PROXY`, so any curl proxy string works (`http://`, `https://`, `socks5://`, `socks5h://`). It is a single global egress for all outbound fetches, not per-bridge. `[proxy] by_bridge = true` plus a set `url` exposes `&_noproxy=1`, which does `define('NOPROXY', true)` in `DisplayAction`.

### Authentication

Two modes, both global, both gating every action including `health`.

- **HTTP Basic**, `BasicAuthMiddleware`, active when `[authentication] enable = true`. Returns 500 `The authentication password cannot be the empty string` if enabled with a blank password. Compares username with `!==`, password with `hash_equals()`. Reads `PHP_AUTH_USER`/`PHP_AUTH_PW`, which depend on the SAPI forwarding the `Authorization` header.
- **Token**, `TokenAuthenticationMiddleware`, active when `[authentication] token` is non-empty. Requires `&token=<value>` as a query parameter (not a header). 401 `Missing token` or `Invalid token`, compared with `hash_equals()`. **An empty token silently disables auth entirely**, so a misconfigured env var fails open.

Middleware order in `lib/RssBridge.php` (built with `array_reverse`, so `BasicAuthMiddleware` runs outermost and `TokenAuthenticationMiddleware` innermost):

```php
new BasicAuthMiddleware(),
new CacheMiddleware($this->container['cache']),
new ExceptionMiddleware($this->container['logger']),
new SecurityMiddleware(),
new MaintenanceMiddleware(),
new TokenAuthenticationMiddleware(),
```

The response cache key includes `token`, so there is no cross-token cache leak.

---

## 2. Docker deployment for our compose stack

### Image and tag

Two registries, in sync: `rssbridge/rss-bridge` on Docker Hub (the one the README documents) and `ghcr.io/rss-bridge/rss-bridge`. Labels confirm the source repo.

Tag families: `latest` (head of master, rebuilt on every push), `stable` (alias for the newest dated release), `YYYY-MM-DD` dated releases, and `sha-<7 hex>` per commit. The newest dated release is `2025-08-05`, and `stable` resolves to the same GHCR digest `sha256:569f01f3faecd0d34d702e01b34eb0a769f7bedb84caf6dff29821d18b46f971`. `lib/Configuration.php` carries `private const VERSION = '2025-08-05'`.

There is **no** `:webdriver`, `:fpm`, `:alpine`, `:php8`, or semver tag. `latest-amd64`, `latest-arm64v8`, `latest-arm32v7` are abandoned 2021 artefacts; do not use them.

Pin `stable` or the dated tag, ideally by digest. Do not use `latest`.

Platforms: `linux/amd64`, `linux/arm64`, `linux/arm/v7`.

### Port

`EXPOSE 80`. nginx listens on 80 inside the container, `root /app`, `index index.php`, `server_tokens off`, `location ~ /(\.|vendor|tests) { deny all; return 403; }`, and critically `fastcgi_read_timeout 45s`, which is a hard ceiling on any single request. The `HTTP_PORT` env var rewrites the listen port via a naive `sed -i "s/80/$HTTP_PORT/g"`; we do not need it.

### Compose service

```yaml
  rss-bridge:
    image: ghcr.io/rss-bridge/rss-bridge:stable
    container_name: caf-rss-bridge
    restart: unless-stopped
    expose:
      - "80"                      # no `ports:` mapping, backend-only
    volumes:
      - ./rss-bridge/config:/config
      - rss-bridge-cache:/app/cache
    mem_limit: 512m
    environment:
      RSSBRIDGE_system_env: "prod"
      RSSBRIDGE_system_timezone: "UTC"
      RSSBRIDGE_system_enabled_bridges: "SubstackBridge,YoutubeBridge,YouTubeFeedExpanderBridge,CssSelectorBridge,CssSelectorComplexBridge,CssSelectorFeedExpanderBridge,SitemapBridge,XPathBridge,WordPressBridge,FeedMergeBridge,FilterBridge,ModifyBridge,RedditBridge,TelegramBridge,BlueskyBridge,MastodonBridge"
      RSSBRIDGE_error_output: "http"
      RSSBRIDGE_error_report_limit: "1"
      RSSBRIDGE_http_timeout: "25"
      RSSBRIDGE_http_retries: "2"
      RSSBRIDGE_cache_type: "sqlite"
      RSSBRIDGE_cache_custom_timeout: "true"
      RSSBRIDGE_admin_donations: "false"
    env_file:
      - ./secrets/rssbridge-credentials.env
    healthcheck:
      test: ["CMD-SHELL", "php -r \"exit(@file_get_contents('http://127.0.0.1/?action=health') ? 0 : 1);\""]
      interval: 60s
      timeout: 10s
      retries: 3
```

Secrets file (gitignored, `chmod 600`):

```
RSSBRIDGE_SubstackBridge_sid=<substack.sid value>
RSSBRIDGE_FinancialTimesBridge_cookie=<if we build that bridge>
RSSBRIDGE_WsjBridge_cookie=<if we build that bridge>
```

Do **not** set `RSSBRIDGE_http_useragent`. See curl-impersonate below.

### Keeping it internal only

There is no configuration option to disable the HTML frontend. I checked every key. The three near-misses are each wrong for us: `[system] enable_maintenance_mode = true` returns 503 for everything including `display`; `[authentication] token` gates the UI but also every feed URL; shrinking `enabled_bridges` shrinks the frontpage but does not remove it.

So the answer is network isolation: omit `ports:` entirely and use `expose:`. Only our FastAPI backend reaches it, as `http://rss-bridge/`. Two supporting reasons: `frontpage` is the default action and the most expensive endpoint in the app, and the image ships `app/docs/`, `app/README.md`, and `app/Dockerfile` under the nginx root (nginx only denies `/(\.|vendor|tests)`).

Setting `RSSBRIDGE_authentication_token` on top is cheap defence in depth, at the cost of appending `&token=...` to every call and to the healthcheck. Either choice is defensible; network isolation is the load-bearing control.

### Health check

`?action=health` returns `{"code":200,"message":"all is good"}`. The compose healthcheck above uses `php -r` rather than `curl` because `curl` is purged from the image in the same layer that installs it. If token auth is enabled, append `&token=...` to the healthcheck URL.

### Resource footprint

Measured from the actual layers:

| | |
|---|---|
| Compressed pull | 96,870,477 bytes, 92.4 MiB |
| On disk | 284,233,728 bytes, 271 MiB |

Runtime is nginx plus php-fpm 8.2 in one container, started by `docker-entrypoint.sh` which runs `nginx` (daemonized) then `exec php-fpm8.2 --nodaemonize`. The image's own pool file sets only logging plus `clear_env = no`; it declares no `pm`, `pm.max_children`, or `memory_limit`, so Debian's stock defaults apply. Those are believed to be `pm = dynamic`, `pm.max_children = 5`, `memory_limit = 128M`, but I did not extract `www.conf` from the image to confirm.

For calibration, upstream's own bare-metal recommendation targets a 1 vCPU / 512 MB VM with `pm = static`, `pm.max_children = 10`, `pm.max_requests = 500`, `max_execution_time = 15`, `memory_limit = 64M`. Idle usage is tens of MB. Set `mem_limit: 512m` and move on. Note that the `/config` volume cannot deliver php-fpm tuning (the entrypoint copies only five filename patterns), so tightening it needs a separate bind mount onto `/etc/php/8.2/fpm/pool.d/` or a derived image.

### curl-impersonate

The Dockerfile sets:

```dockerfile
ENV LD_PRELOAD=/usr/local/lib/curl-impersonate/libcurl-impersonate.so
ENV CURL_IMPERSONATE=chrome142
```

curl-impersonate v1.2.5, from `lexiforest/curl-impersonate`, SHA-512 verified per architecture. `CurlHttpClient::request()` branches on `curl_version()['ssl_version'] == 'BoringSSL'`; when true it sets **no** default User-Agent and **no** default headers, leaving the Chrome 142 TLS/JA3 fingerprint and header set intact. This is the single strongest reason to route premium fetches through rss-bridge rather than Python `httpx`.

Setting `[http] useragent` disables it. The shipped config says so: "Use only if you know what you're doing, otherwise you may stop libcurl-impersonate from doing its job impersonating real browser."

There is also explicit Cloudflare detection, `CloudFlareException::isCloudFlareResponse()`, keyed on body titles including `<title>Just a moment...` and `<title>Attention Required!`.

### Adding custom bridge PHP files

`docker-entrypoint.sh` scans `/config/` recursively and dispatches by basename:

| Pattern | Destination |
|---|---|
| `*Bridge.php` | `/app/bridges/` |
| `*Format.php` | `/app/formats/` |
| `config.ini.php` | `/app/` |
| `whitelist.txt` | `/app/` |
| `DEBUG` | `/app/` |

Files are copied then `chown www-data:www-data`. Filenames containing a space are skipped with a printed warning. Anything else is silently ignored. **This runs only at container start; a restart is required for changes to take effect.** The README says so three times.

Constraints on a custom bridge, from `BridgeFactory` (`preg_match('/^([^.]+Bridge)\.php$/U', $file, $m)`) and the `spl_autoload_register` in `lib/bootstrap.php`:

- filename must end in `Bridge.php`
- filename must contain no other `.`
- class name must equal the filename stem
- new files must start with `<?php` then `declare(strict_types=1);`
- avoid underscores in the class name, because the env-var section is a single `_`-delimited token
- the bridge must be added to `enabled_bridges`

An alternative that skips the entrypoint copy: bind-mount straight onto `/app/bridges/OurBridge.php`. Cleaner for a git-tracked file.

If we ever ship a `config.ini.php`, its first line **must** be:

```
; <?php exit; ?> DO NOT REMOVE THIS LINE
```

because `/app` is the nginx root and `config.ini.php` matches `location ~ \.php$`, so it is executed by php-fpm. Without that line its contents render to anyone who can reach the port. This is another argument for env vars over a config file.

### What the image does not contain

No browser, no Selenium, no node. The `apt-get install` list is `ca-certificates nginx nss-plugin-pem php-curl php-fpm php-intl php-mbstring php-memcached php-sqlite3 php-xml php-zip`. More importantly, `composer install` is never run and **there is no `/app/vendor/` directory** (verified by listing the shipped layer). `php-webdriver/webdriver` is a `suggest`, not a `require`, so `Facebook\WebDriver\*` does not exist and `WebDriverAbstract` bridges would fatal even with a Selenium sidecar running. Only two shipped bridges extend it and neither is relevant to us. If we ever need JS execution, it lives in a separate Playwright worker, not here.

---

## 3. URL routing table for pasted links

### The headline constraint

Only **43 of 548** bridges implement `detectParameters()`, and the gaps land exactly on our cases. `YoutubeBridge`, `SubstackBridge`, `SubstackProfileBridge`, `TwitterV2Bridge`, `BlueskyBridge`, `MastodonBridge`, `CssSelectorBridge`, `XPathBridge`, `SitemapBridge` all lack it. There is no bridge at all for FT or WSJ, and no passthrough bridge for an arbitrary native feed URL.

Therefore: **our router owns classification.** Call `findfeed` only as a fallback after our own ordered rules miss, and never use `detect`.

The default `BridgeAbstract::detectParameters()` only fires for bridges with **no** `PARAMETERS` at all, matching the URL host against `static::URI` host:

```php
$regex = '/^(https?:\/\/)?(www\.)?(.+?)(\/)?$/';
if (empty($contexts)
    && preg_match($regex, $url, $urlMatches) > 0
    && preg_match($regex, static::URI, $bridgeUriMatches) > 0
    && $urlMatches[3] === $bridgeUriMatches[3]) { return []; }
return null;
```

The 43 bridges that do implement it: `AssociatedPressNewsBridge`, `AutoJMBridge`, `BadDragonBridge`, `BandcampBridge`, `BMDSystemhausBlogBridge`, `CentreFranceBridge`, `CodebergBridge`, `CraigslistBridge`, `DerpibooruBridge`, `DockerHubBridge`, `FacebookBridge`, `FarsideNitterBridge`, `FirefoxAddonsBridge`, `FreeTelechargerBridge`, `FunkBridge`, `FurAffinityBridge`, `GithubIssueBridge`, `GithubReleaseBridge`, `GoogleGroupsBridge`, `GooglePlayStoreBridge`, `IKWYDBridge`, `ImgsedBridge`, `IndeedBridge`, `InstagramBridge`, `InternetArchiveBridge`, `NHKWorldJapanShowBridge`, `PatreonBridge`, `PirateCommunityBridge`, `RedditBridge`, `SkimfeedBridge`, `StravaBridge`, `TelegramBridge`, `TheBellBridge`, `ThePintBridge`, `ThreadsBridge`, `TikTokBridge`, `TraktBridge`, `TrelloBridge`, `TwitchBridge`, `TwitterBridge`, `Vk2Bridge`, `VkBridge`, `YouTubeCommunityTabBridge`.

### Summary table

| Family | findfeed can detect? | Route to | Auth needed | Confidence |
|---|---|---|---|---|
| Native RSS/Atom URL | No (no passthrough bridge) | our own feed ingest | none | high |
| HTML page with `<link rel=alternate>` | No | our own autodiscovery, then feed ingest | none | high |
| Substack, free | No (`SubstackBridge` has no `detectParameters`) | our own Python fetch of `<host>/feed` | none | high |
| Substack, paid | No | our own fetch with `substack.sid`, or `SubstackBridge` | account cookie | medium-high, unverified end to end |
| YouTube channel or `@handle` | No (`YoutubeBridge` has no `detectParameters`) | native `feeds/videos.xml?channel_id=` | none | high |
| YouTube playlist | No | native `feeds/videos.xml?playlist_id=` (recent) or yt-dlp (full) | none | high |
| YouTube single video | No, and no bridge context exists | yt-dlp path directly | cookies likely needed for captions | low for captions |
| X account | Partial, `twitter.com` only, and the bridge is dead | `TwitterV2Bridge` with a paid token, or skip | paid X API bearer token | low |
| X post | Partial, wrongly resolves to a username | do not ingest | | very low |
| FT article or section | No bridge exists | FT RSS for discovery, `api.ft.com` for full text | API key under licence | high for RSS, blocked for HTML |
| WSJ article or section | No bridge exists | `feeds.content.dowjones.io` plus the Dow Jones GraphQL gateway | none for discovery | high for discovery, blocked for HTML |
| Reddit | Yes, `RedditBridge` | `RedditBridge` | none | medium |
| Telegram channel | Yes, `TelegramBridge` | `TelegramBridge` | none, public channels only | medium |
| Bluesky | No | `BlueskyBridge` | none | medium-high |
| Mastodon | No | `MastodonBridge` | keypair only for Authorized Fetch instances | medium |
| Generic article page | No | our extractor, or `CssSelectorBridge` recipe | none | medium |
| Generic site with CSS selector | No | `CssSelectorBridge` / `CssSelectorComplexBridge` / `SitemapBridge` | cookie only via `CssSelectorComplexBridge` | medium, recipes rot |

### Per-family detail

**Native RSS/Atom feed URL.** Detect: `GET`/`HEAD` and check `content-type` for `application/rss+xml`, `application/atom+xml`, `text/xml`, or parse the body root element. rss-bridge has no passthrough bridge, so `findfeed` returns 404. Ingest directly with our existing feed path. rss-bridge is only useful here for enrichment: `FilterBridge` (regex include/exclude, params `url`, `filter`, `filter_type` with values `permit`/`block`, `target_title` defaults to checked), `ModifyBridge` (regex find/replace for URL canonicalisation), `FeedMergeBridge` (params `feed_name`, `feed_1` through `feed_10`, `limit`; hard cap of 10 items per source feed, and a failing source is injected as a synthetic item titled `RSS-Bridge: <message>` with `timestamp => time()` so it sorts to the top).

**HTML page with feed autodiscovery.** Detect: parse `<link rel="alternate" type="application/rss+xml">` or `type="application/atom+xml"` and take `href`. Do this ourselves; there is no rss-bridge action for it. If the discovered feed is truncated, expand with `CssSelectorFeedExpanderBridge` (params `feed` required, `content_selector` required, `content_cleanup`, `dont_expand_metadata`, `discard_thumbnail`, `thumbnail_as_header`, `limit`). Note this bridge calls `getContents($url)` with no headers, so it cannot carry a cookie.

**Substack.** Detect by header, not by domain: issue one request and classify as Substack if the response carries `x-cluster: substack`; capture `x-sub: <slug>` as the publication ID. Verified positive on `www.astralcodexten.com` and `newsletter.pragmaticengineer.com` (both custom domains), negative on `stratechery.com`. A `*.substack.com` regex misses most real publications. Canonicalise to `https://<host>/feed`.

`SubstackBridge` is a `FeedExpander` with a single unnamed context and one parameter:

```
url  (required, text, defaultValue https://newsletter.pragmaticengineer.com/feed)
```

Display URL:

```
http://rss-bridge/?action=display&bridge=SubstackBridge&url=https%3A%2F%2Fnewsletter.pragmaticengineer.com%2Ffeed&format=Json
```

Its entire logic is one authenticated GET of `<pub>/feed`. It does not override `parseItem()`, does not scrape post pages, and does not call the Substack API. See section 4 for the recommendation to reimplement this in Python.

**YouTube.** Detect with our own regexes on `youtube.com` and `youtu.be`:

- `/channel/(UC[\w-]{22})` → native feed `https://www.youtube.com/feeds/videos.xml?channel_id=<id>`
- `/@handle`, `/c/NAME`, `/user/NAME` → resolve to a `UC…` id first, then native feed
- `/playlist?list=(PL[\w-]+)` → `https://www.youtube.com/feeds/videos.xml?playlist_id=<id>`
- `/watch?v=(\w{11})` or `youtu.be/(\w{11})` → single video, no feed exists

Verified live: `?channel_id=`, `?playlist_id=`, and legacy `?user=` all return 200 with no key, no cookie, and no User-Agent required. `?channel_id=@handle`, `?user=@handle` return 404, `?handle=` returns 400. There is no handle parameter.

Handle resolution: use `yt-dlp` with `{'extract_flat': 'in_playlist', 'skip_download': True}` against `https://www.youtube.com/@handle/videos`. It returns `channel_id` plus per-entry `id`/`title`/`duration`/`url`, and it is not bot-blocked (unlike the player). Fallback is the HTML `<link rel="alternate" type="application/rss+xml">` tag, which requires `Cookie: SOCS=CAI` from EU/UK egress or the request 302s to `consent.youtube.com`. **Never grep the first `"channelId":"…"` in the HTML**: on the Linus Tech Tips handle page the first three hits were unrelated sidebar channels. Use `"externalId"` or the `rss+xml` link tag.

`YoutubeBridge` contexts if we use it at all:

| Context | Param |
|---|---|
| `By username` | `u` |
| `By channel id` | `c` |
| `By custom name` | `custom` |
| `By playlist Id` | `p` |
| `Search result` | `s`, `pa` |

plus `global` params `duration_min`, `duration_max`, `skip_members_only`. `const CACHE_TIMEOUT = 60 * 60 * 3`.

```
http://rss-bridge/?action=display&bridge=Youtube&context=By+channel+id&c=UCXuqSBlHAE6Xw-yeJA0Tunw&format=Json
```

Setting any duration filter forces the fragile `var ytInitialData` HTML scrape path, which from EU/UK egress hits the consent interstitial (the image ships curl-impersonate with a browser fingerprint, and the bridge sends `Accept-Language: en-US`, which is exactly the combination that redirects). The bridge has no cookie knob. Prefer the native feed, or `YouTubeFeedExpanderBridge` (params `channel` required, `embed`, `embedurl`, `nocookie`, `hideshorts`) which is feed-based.

Two native-feed parsing rules to encode as tests: **the feed-level `<yt:channelId>` and `<id>` are missing the `UC` prefix** (observed `yt:channel:w38-8_Ibv_L6hlKChHO9dQ` for channel `UCw38-8_Ibv_L6hlKChHO9dQ`, on two independent feeds; per-entry `<yt:channelId>` is correct), and **there is no duration element** anywhere in the feed. Feeds return 15 entries with no pagination and `cache-control: public, max-age=900`, so 15 minutes is the poll floor.

**X / Twitter.** Normalise `x.com`, `www.twitter.com`, `mobile.twitter.com` to a canonical handle first; **no bridge in the repo matches `x.com`** (verified: the string appears nowhere in `TwitterBridge.php`, `TwitterV2Bridge.php`, or `FarsideNitterBridge.php`). Strip `/status/<id>`, `/with_replies`, query strings. Reject reserved paths: `home`, `i`, `search`, `explore`, `notifications`, `messages`, `settings`, `compose`, `hashtag`.

`TwitterBridge::detectParameters` regexes, applied in this order:

```
/^(https?:\/\/)?(www\.)?twitter\.com\/search.*(\?|&)q=([^\/&?\n]+)/     → context 'By keyword or hashtag', q=$4
/^(https?:\/\/)?(www\.)?twitter\.com\/hashtag\/([^\/?\n]+)/            → context 'By keyword or hashtag', q=$3
/^(https?:\/\/)?(www\.)?twitter\.com\/([^\/?\n]+)\/lists\/([^\/?\n]+)/ → context 'By list', user=$3, list=$4
/^(https?:\/\/)?(www\.)?twitter\.com\/([^\/?\n]+)/                     → context 'By username', u=$3
```

The last one is a catch-all, so `twitter.com/i/status/123` resolves to user `i`. `FarsideNitterBridge::detectParameters` is `'/^(https?:\/\/)?(www\.)?(nitter\.net|twitter\.com)\/([^\/?\n]+)/'` and also fires on plain twitter.com URLs, so with both enabled the `detect` winner depends on `scandir` order. Disable `FarsideNitterBridge` explicitly.

`TwitterBridge` is dead. Reproduced live: guest token activate returns 200, `UserByScreenName` returns 200, but the timeline query `graphql/3JNH4e9dq1BifLxAa3UMWg/UserWithProfileTweetsQueryV2` returns **404** (the queryId has been rotated by X and never updated), and the `cdn.syndication.twimg.com/tweet-result` fallback also 404s. Matches open issue #4445. `FarsideNitterBridge` also dead: `farside.link/nitter/NASA/rss` returns 404, `nitter.net/NASA/rss` returns 200 with an empty body, `xcancel.com` redirects to a 403.

`TwitterV2Bridge` is structurally intact; all four v2 endpoints returned 401 (not 404) with an invalid token, proving the routes exist. It requires `const CONFIGURATION = ['twitterv2apitoken' => ['required' => true]]`, so the bridge throws `Missing configuration option: twitterv2apitoken` at instantiation if unset. Contexts: `By username` (`u`), `By keyword or hashtag` (`query`, note not `q`, and search is a 7-day window only), `By list ID` (`listid`). Global params include `maxresults` (hard-capped at 100), `norep`, `noretweet`, `nopinned`, `filter`. It has no `detectParameters`.

```
http://rss-bridge/?action=display&bridge=TwitterV2Bridge&context=By+username&u=<handle>&maxresults=100&norep=on&format=Json
```

**FT.** Detect on host `ft.com`; extract the article UUID with `/content/([0-9a-f-]{36})`. No bridge exists (checked all 548 filenames and a content grep). Section and topic RSS is live at `https://www.ft.com/<slug>?format=rss` (verified across 20 slugs, all 200, 25 items each). Note `/rss/companies`, `/rss/markets`, `/rss/world` exist only as 301 redirects to the `?format=rss` form. Article HTML is blocked from datacenter IPs; see section 4.

**WSJ.** Detect on host `wsj.com`. No bridge exists. Discovery uses `https://feeds.content.dowjones.io/public/rss/<SLUG>`; see section 4 for the stale-feed trap on the legacy host. Article HTML is blocked; see section 4.

**Reddit.** `RedditBridge` has the cleanest `detectParameters` in the codebase: it uses `Url::fromString($url)`, rejects any host that is not `www.reddit.com` or `old.reddit.com`, then `/r/<x>` maps to `['context'=>'single','r'=>$x]` and `/user/<x>` to `['context'=>'user','u'=>$x]`. `findfeed` handles this correctly.

```
http://rss-bridge/?action=display&bridge=RedditBridge&context=single&r=investing&format=Json
```

Contexts: `single` (`r` required, `f` flair), `multi` (`rs` comma-separated), `user` (`u` required, `comments`). Global: `score`, `min_comments`, `d` (Hot/Relevance/New/Top/Comments), `t` (All/Year/Month/Week/Day/Hour), `search`, `frontend`. `const CACHE_TIMEOUT = 60*60*2`. 6 open issues.

**Telegram.** `TelegramBridge::detectParameters` regex:

```
/^https?:\/\/(?:(?:t|telegram)\.me\/(?:s\/)?([\w]+)|([\w]+)\.t\.me\/?)$/
```

Note the `$` anchor: it matches bare channel URLs only, no path suffix and no query string. Public channels only. One parameter `username` (required). `const CONFIGURATION = ['max_pages' => ['required'=>false, 'defaultValue'=>1]]`, where 1 page is roughly 20 messages and each page costs one HTTP request.

```
http://rss-bridge/?action=display&bridge=TelegramBridge&username=rssbridge&format=Json
```

**Bluesky.** `BlueskyBridge` has no `detectParameters`, so match `bsky.app` ourselves and extract the handle or DID from `/profile/<id>`. Context `Posts from a user`: `user_id` (required, handle or DID), `feed_filter` (list, default `posts_and_author_threads`, also `posts_with_replies`, `posts_no_replies`, `posts_with_media`), `include_reposts` (default checked), `include_reply_context`, `verbose_title`. No auth, uses the public AppView API. 2 open issues, currently one of the more reliable social bridges.

```
http://rss-bridge/?action=display&bridge=BlueskyBridge&context=Posts+from+a+user&user_id=<handle>&format=Json
```

**Mastodon.** The class is `MastodonBridge` but `NAME = 'ActivityPub'`. No `detectParameters`. Match `@user@instance` syntax or an instance profile URL ourselves and build `canusername` as `@sebsauvage@framapiaf.org`. Other params: `noregular`, `norep`, `noboost` (boosts are slow because they refetch from remote instances), `signaturetype` (list: `noquery` for Mastodon default, `query` for GoToSocial, `nosig`). Works for Mastodon, Pleroma, and Misskey by reading the ActivityPub outbox.

Only instances running Authorized Fetch / Secure Mode need credentials, and the setup is heavyweight: generate an RSA 2048 keypair (`openssl genrsa -out private.pem 2048`), serve a WebFinger document at `https://DOMAIN/.well-known/webfinger` and an actor document at `https://DOMAIN/actor` carrying `publicKeyPem`, then set `[MastodonBridge] private_key = "/absolute/path/to/private.pem"` and `key_id = "https://DOMAIN/actor#main-key"`. Skip this unless a specific instance requires it.

**Generic article page.** No bridge auto-detects. Prefer our own extractor. If the site is WordPress (test `/feed/atom/`, or look for `wp-content` in the HTML), `WordPressBridge` is the best generic full-text expander: params `url` (required, must start with `http`), `limit` (code default 10), `content-selector` (note the hyphen). Its selector cascade is user selector, then `[itemprop=articleBody]`, `.article-content`, `article`, `.single-content`, `.post-content`, `.post`. Most analyst and VC blogs are WordPress.

**Generic site with CSS selector.** `CssSelectorBridge` params:

| param | required | notes |
|---|---|---|
| `home_page` | yes | index page URL |
| `url_selector` | yes | matches link elements or a parent |
| `url_pattern` | no | regex, wrapped as `'/' . str_replace('/', '\/', $pattern) . '/'`, so no delimiters |
| `content_selector` | no | **if set, fetches each article page** |
| `content_cleanup` | no | |
| `title_cleanup` | no | |
| `discard_thumbnail`, `thumbnail_as_header` | no | checkboxes |
| `limit` | no | code default 10 |

It has **no** cookie or header parameter. `SitemapBridge` (extends it, adds `site_map`, reads `/robots.txt` for `Sitemap:` then falls back to `/sitemap.xml`, requires both `<loc>` and `<lastmod>` per URL) and `XPathBridge` also have none.

`CssSelectorComplexBridge` is **the only generic scraper with a cookie parameter**:

```php
protected function getHeaders() {
    $headers = [];
    $cookie = $this->getInput('cookie');
    if (!empty($cookie)) { $headers[] = 'Cookie: ' . $cookie; }
    return $headers;
}
```

and it applies those headers to the index page, the title fetch, and every article page. Params: `home_page` (required), `cookie`, `title_cleanup`, `entry_element_selector` (required), `url_selector` (default `a`), `url_pattern`, `limit`, `use_article_pages`, `article_page_content_selector`, `content_cleanup`, `title_selector` (default `h1`), `category_selector`, `author_selector`, `time_selector`, `time_format`, `remove_styling`. Article pages are cached for a hardcoded 86400 seconds. The cookie arrives as a **request parameter**, with all the leakage consequences in section 4.

Store selector recipes in a `source_recipes` table keyed on domain with `bridge`, a params dict, and `verified_at`. Selectors rot; treat them as data with an owner. Flag two strings as recipe-rot signals rather than transport errors: the inline marker `Could not extract full content, selector may need to be updated.` and any item whose title starts `RSS-Bridge: `.

### Router order

1. Native feed sniff (content-type, then root element).
2. Feed autodiscovery from HTML `<link rel=alternate>`.
3. Host rules, in this order: youtube.com/youtu.be, x.com/twitter.com (after normalisation), reddit.com, t.me/telegram.me, bsky.app, ft.com, wsj.com.
4. Substack header probe (`x-cluster: substack`).
5. Mastodon handle syntax.
6. Stored per-domain recipe from `source_recipes`.
7. `?action=findfeed&url=<enc>&format=Json` as fallback. Treat 404 as no match. Rank the returned array; never take `[0]`. Reconstruct the display URL from `bridgeParams` rather than storing the returned relative `url`, so stored sources survive a host change.
8. Generic article extraction.

---

## 4. Premium sources

### Credential supply, the general rule

**Supply every credential as an environment variable in the form `RSSBRIDGE_<BridgeClassShortName>_<option>`. Never as a URL parameter.**

Two shipped bridges do take cookies as request parameters (`MediapartBridge` with `mpsessid`, `HeiseBridge` with `sessioncookie`, plus `CssSelectorComplexBridge` with `cookie`), and there is no sanitisation that helps: `ParameterValidator::validateTextValue()` calls `filter_var($value)` with no filter, which is a no-op. A credential in the query string leaks by four verified paths:

1. **nginx access log**, which the Dockerfile symlinks to `/dev/stdout`, so it lands in `docker logs` and whatever ships those.
2. **Echoed back into the feed body on error.** `DisplayAction::createGithubIssueUrl()` builds a `github.com/.../issues/new` URL whose body contains `$_SERVER['QUERY_STRING']`, and `templates/bridge-error.html.php` renders that as a clickable "Create GitHub Issue" button inside the feed item. With the default `error.output = "feed"` any bridge failure emits that item.
3. **Persisted to disk in the response cache**, keyed on `'http_' . json_encode($request->toArray())`.
4. Referer headers, browser history, and our own `sources` table if we store the bridge URL.

The trade-off: config-based credentials are **instance-wide and per-bridge-class**. One rss-bridge container can hold exactly one `substack.sid`, one FT cookie, one WSJ cookie. If team members hold different subscriptions we need either separate containers or credentialed fetching in our own Python layer. Given we already have Postgres and an encryption story, I lean toward Python owning the cookie jar and rss-bridge covering unauthenticated sources plus its curl-impersonate advantage.

Also note there is **no generic cookie facility** in rss-bridge. Cookie injection is per-bridge PHP inside `collectData()` or `parseItem()`.

### Paid Substack

**Feasible today, and the mechanism is simple enough that I recommend not using rss-bridge for it.**

Cookie name: `substack.sid`. Obtained manually: log in at `https://substack.com/`, DevTools, Application, Cookies, `https://substack.com`, copy `substack.sid`. Login is sometimes CAPTCHA-gated, so this cannot be automated. Documented lifetime is three months, but the maintainer's own warning is that "the session might die after e.g. 7 days or e.g. 24 hours of inactivity."

`SubstackBridge` sends four cookies plus a hardcoded Chrome 112 UA:

```
ab_experiment_sampled=%22false%22; substack.sid=<sid>; substack.lli=1; intro_popup_last_hidden_at=<ISO8601 with ms and Z>
```

The actual trick is that `substack.sid` is browser-scoped to `substack.com` but the bridge sends it to the publication host (including custom domains). A browser would never do that; Substack's backend accepts it because every publication runs on the same backend. One sid covers every publication that account subscribes to.

Config: `[SubstackBridge] sid = "<your-sid>"` or `RSSBRIDGE_SubstackBridge_sid`.

**Recommended approach: reimplement in Python, roughly 30 lines.** The bridge adds nothing over a direct fetch and costs us a 1-hour opaque cache, a hard 13 to 20 item ceiling with no pagination, error responses cached 5 to 15 minutes, and no `detectParameters` for the router. Two tiers:

- Recent: `GET https://<host>/feed` with the cookie header. Returns the last 13 to 20 posts with `content:encoded`. The `?limit=` query parameter is ignored; item count varies between requests.
- Backfill: page `GET https://<host>/api/v1/archive?sort=new&offset=N&limit=12` for metadata (`slug`, `id`, `title`, `post_date`, `audience`, `wordcount`, `publishedBylines`, `postTags`, `canonical_url`), then one `GET https://<host>/api/v1/posts/<slug>` per post for `body_html`. The archive endpoint returns `body_html: ""` for every item.

**Do not point anything at `https://substack.com/api/...` from the server**; that host returns a Cloudflare "Just a moment..." 403 to datacenter IPs (this is why upstream PR #3969 was abandoned). Per-publication hosts are fine.

Health check for the cookie: pick one known `only_paid` post per publication, fetch it authenticated, assert `body_html` length is much greater than `truncated_body_text` length. **Do not gate on HTTP status**: `/api/v1/posts/<slug>` returns 200 with a 473-byte body for a locked post. Fire an in-app alert on failure.

Paywall-truncation detector for QA: a truncated item's `content:encoded` ends with the literal block

```html
<p><a href="{item link}">Read more</a></p>
```

Also record `audience` (`everyone` or `only_paid`) and compare our extracted word count against the API's `wordcount`.

**Unverified:** I never had a real `substack.sid`, so the claim that an authenticated `/feed` returns untruncated `content:encoded` for `only_paid` posts rests on the maintainers' statements plus a merged CI-green PR. Validate with one real cookie before building on it.

Fallback if we do want the bridge:

```
http://rss-bridge/?action=display&bridge=SubstackBridge&url=<urlencoded https://host/feed>&format=Json
```

with `RSSBRIDGE_SubstackBridge_sid`, `RSSBRIDGE_http_timeout=30` (the 680 KB feed exceeds the 5 second default), and an image tag at or after `2024-08-*` (PR #4174 merged 2024-07-31 added the bridge, the `$headers` parameter on `FeedExpander::collectExpandableDatas`, and `content:` namespace support in `FeedParser`).

### FT

**Cookie injection from our server cannot work.** Verified: `https://www.ft.com/content/<uuid>` returns `HTTP/2 403` with `cf-mitigated: challenge`, `server: cloudflare`, and a 270 KB Cloudflare JS challenge body. Every header permutation tried still 403s: no UA, Chrome 126 UA, full browser header set with `sec-ch-ua*` and `sec-fetch-*`, Firefox UA with `Referer: https://www.google.com/` (calibre's method), Googlebot UA, HTTP/1.1, and with the `?syn-25a6b1a6=1` syndication parameter. The whole HTML app is blocked (`/`, `/markets`, `enterprise.ft.com`), while `robots.txt`, `sitemaps/index.xml`, and all RSS feeds return 200 even with no User-Agent. Two independent datacenter networks produced the same result. The 403 is issued at the Cloudflare edge before the origin sees any cookie, so cookies are necessary but nowhere near sufficient.

Cookie names, for the record: `FTSession` and `FTSession_s` are the auth pair but are not publicly documented (a GitHub code search for `FTSession` returned no FT-related hits). `FTConsent` is consent only, confirmed from FT's own JS (`Cookies.get("FTConsent",{path:'/',domain:".ft.com"})`, tested with `cookieVal.includes("cookiesOnsite:on")`). `spoor-id` is analytics for `https://spoor-api.ft.com/px.gif`. Session lifetime and any device cap are unverified; no FT documentation found.

Login is CAPTCHA-gated (the FT login page loads `https://js.hcaptcha.com/1/api.js`), matching the dead code comment in calibre's recipe: "ft.com uses a CAPTCHA on its login page so this sadly doesn't work". Cookies must be harvested from a real human browser session.

**Recommended approach: two decoupled layers.**

*Layer 1, discovery via RSS, build now.* `https://www.ft.com/<slug>?format=rss` on a 15-minute cadence (`<ttl>15</ttl>`, robots `Crawl-Delay: 1`). Verified live for 20 slugs including `markets`, `companies`, `global-economy`, `lex`, `private-equity`, `banks`, `energy`, `semiconductors`, `artificial-intelligence`, `china`, `us`, `world`, `opinion`, `firstft`, plus nested slugs like `companies/energy`. Plain `feedparser` plus `httpx`; no rss-bridge involved. Items carry `title`, `description` (standfirst, max observed 120 chars), `link`, `guid`, `pubDate`. **The `<guid>` is the bare article UUID and is exactly the `{itemId}` for `GET https://api.ft.com/content/{itemId}`**, which makes RSS to Content API a clean join. Strip the `?syn-25a6b1a6=1` tracking parameter from links. There is no full-text switch: `?format=rss&fullText=true`, `&content=full`, `&count=100` all return byte-identical responses; `?format=atom` and `?format=json` return 403.

*Layer 2, full text via the official Content API.* `GET https://api.ft.com/content/{uuid}` with header `X-Api-Key: <key>` (or `?apiKey=`). Verified: the endpoint exists and returns `403 {"error": "Access to this API has been disallowed"}` without a valid key. Response schema from FT's own Go client includes `bodyXML` (the full article text), `title`, `standfirst`, `byline`, `canBeSyndicated` (values `yes`, `with-contributor-payment`, `verify`, `no`), `editorialDesk`, `annotations[]`, `mainImage`, `embeds[]`, `publishedDate`, `firstPublishedDate`, `webUrl`. The annotation model maps directly onto a knowledge graph: predicates `about`, `mentions`, `implicitlyAbout`, `hasAuthor`, `isClassifiedBy`, `hasDisplayTag` over typed concepts `Organisation`, `PublicCompany`, `Person`, `Topic`, `Location`, `Genre`, `Brand`, **with FIGI and LEI codes attached to companies**. That is free entity resolution to tickers.

Client gotcha from FT's own code: disable automatic redirect following or re-attach `X-Api-Key`, because Go's default redirect handling drops it.

The relevant product is the **Datamining Licence**, whose published description is "Host the full text of FT articles and meta data on your servers to run your own search algorithms for data mining purposes (royalty required)". That is verbatim what CAF Vault does. **Reader subscriptions at any tier do not include API access**; keys are gated on a separate commercial licence. Contacts: `content.licensing@ft.com`, `https://developer.ft.com/portal/contact`.

*Free win worth taking:* each team member can enable **My Account, Contact Preferences, myFT RSS**, producing `https://www.ft.com/myft/following/{uuid}.rss`. FT states these feeds are "readable by anyone" and contain no personal information, so our backend polls them with no cookies and no Cloudflare exposure. Verified the endpoint exists (401 for a bogus UUID). Whether the `{uuid}` is a user ID or a rotating secret is unverified.

*Liveness probe if we ever do store FT credentials:* `GET https://session-next.ft.com/{sessionToken}` is reachable from a server and is not Cloudflare-challenged. Invalid tokens return `404 "Session {token} is not valid."`; a valid token is expected to return the `NextSession` JSON with `Products` and `Groups` reporting the entitlement tier (unverified, no real token available).

*Compliance note, not legal advice:* FT's robots.txt and T&C both carry an explicit machine-learning prohibition ("we expressly prohibit any use of our content or data ... for any machine learning and/or artificial intelligence purposes"), with `ClaudeBot`, `anthropic-ai`, and `Claude-Web` named among the disallowed agents. The T&C also states "Each Subscription is for a single user only" and permits suspension for sharing access. FT litigated exactly this pattern in *FT v. Blackstone* (2009), where the evidence was "the number of cookies recorded and the IP addresses associated with the credentials" across networked servers, which is the fingerprint a shared server-side cookie pool produces. The same T&C reserves the right to license ML use, so it is a purchasable right rather than a hard no. Settle this before building scrapers.

### WSJ

**Same conclusion, different anti-bot vendor, plus a live discovery path that is better than RSS.**

*The stale-feed trap, fix this first.* `feeds.a.dj.com/rss/*.xml` (the URLs everyone cites) return **HTTP 200 with valid XML frozen at 27 Jan 2025**. Cache-busting confirms it is origin state, not a CDN artefact. The `last-modified` header on the S3 object confirms it. A naive poller ingests them forever and never errors. The live host is:

```
https://feeds.content.dowjones.io/public/rss/<SLUG>      (no .xml extension)
```

Verified-live slugs: `RSSMarketsMain` (61 items), `RSSWorldNews` (70), `WSJcomUSBusiness` (85), `RSSWSJD` (40), `RSSOpinion` (100), `socialeconomyfeed` (36), `RSSLifestyle` (55), `RSSPersonalFinance` (31), `RSSArtsCulture` (51), `RSSMarkets` (20). Confirmed 404: `RSSHealth`, `RSSRealEstate`, `RSSPolitics`, `RSSSports`, `RSSHeardOnTheStreet`, `RSSPro`, and about ten others.

The new format drops the `xmlns:wsj`/`xmlns:dj` namespaces, so `<category domain="AccessClassName">PAID</category>`, `<wsj:articletype>`, and the stable `WP-WSJ-…` guid are gone; guids are now slugs. Strip the `?mod=rss_markets_main` suffix from links.

*Article HTML is blocked.* `https://www.wsj.com/articles/...` returns `HTTP/2 401` with `server: CloudFront`, `x-datadome: protected`, `x-dd-b: 1`, and a 767-byte DataDome JS challenge. Seven header and protocol permutations all fail (browser UA, no UA, HTTP/1.1, full browser header set, Google referer, Googlebot UA). `robots.txt` returns 200; static assets pass. `datadome` cookie is `Max-Age=31536000`, `Domain=.wsj.com`, `SameSite=Lax`. Cookie names `djcs_session`, `djcs_auto`, `djcs_info` from the brief are **unverified**; only `djcs_route` has a secondhand description (Cookiepedia). Session lifetime unverified. AMP is retired (`/amp/articles/<slug>` 308s to canonical), Google referer is dead, Googlebot UA is dead, and `archive.ph` returns 429 plus CAPTCHA from a datacenter IP.

*The best discovery source is an unauthenticated Dow Jones GraphQL gateway.*

```
https://shared-data.dowjones.io/gateway/graphql
```

Requires **both** `apollographql-client-name: wsj-mobile-android-release` and a browser-like User-Agent; either alone returns 403. No DataDome. Two persisted queries:

- `operationName=IssueQuery`, `variables={"publication":"WSJ","region":"US","masthead":"ITPNEXTGEN"}`, `sha256Hash=d938226e7d1c1fff050e7d084c72179e2713dcf4736d3a442c618c55b896f847`. Returns `data.mobileIssuesByMasthead`, the last 7 print editions with `{id, datedLabel, publishedDateUtc, sections[]}`.
- `operationName=SectionQuery`, `variables={"id":"<section key>"}`, `sha256Hash=207fe93376f379bf223ed2734cf9313a28291293366a803db923666fa6b45026`. Returns article objects with `originId` (the stable `WP-WSJ-0003820518` format the new RSS dropped), `articleIsFree`, `availabilityFlags`, `sourceUrl` (clean, no `?mod=`), `publishedDateTimeUtc`, `mobileSummary.headline.text`, `meta.metrics.timeToReadMinutes`, and `articleByline.flattened` with structured `PERSON` decorations that feed the graph directly.

Sleep 3 seconds between section queries (calibre does). Treat as unofficial: circuit-break it and alert on 403 or schema drift.

*Full-article TTS audio is public.* `readToMe.audioUrl` from the gateway, for example `https://m.wsj.net/audio/20260816/wp-wsj-0003820518/1/ele-wp-wsj-0003820518-full.mp3`, verified 200, `audio/mpeg`, 4.45 MB, no auth, no DataDome. The `-full` suffix and size indicate a complete narration. Since Vault already runs podcast transcription, this reaches full WSJ text for paywalled articles with no browser and no anti-bot exposure. It loses tables and charts, so use it as the fallback tier.

*Article markup, if we ever get through.* Body root `article[style*="article-body"]`; paragraphs are `div[data-type="paragraph"]`; images `div[data-type="image"]` with `img[currentsourceurl]` plus `?width=1200`. Strip `data-spot-im-*`, `data-testid="ad-container"`, `ufc-follow-author-widget`, and ids prefixed `comments_sector`, `wrapper-INLINE`, `audio-tag-inner-audio-`.

*Official route.* Factiva Retrieval API, `POST https://api.dowjones.com/content/gen-ai/retrieve`, OAuth2 at `https://accounts.dowjones.com/oauth2/v1/token` with `FACTIVA_CLIENTID`, `FACTIVA_USERNAME`, `FACTIVA_PASSWORD`. It is explicitly described as providing "the retrieval functionality that returns news articles as part of the trusted data sources in a RAG stack". `response_limit` max 100; `search_filters` scopes `Language`, `Organization`, `NewsSubject`, `Industry`, `Source`, `Region`; `Source` codes include `WSJO` (WSJ Online) and `J` (WSJ print). Pricing unpublished, sales-negotiated. It removes all anti-bot fragility in one move; worth one conversation before investing in browser automation.

*One cheap probe worth running from the production host* (blocked in the research sandbox by DNS interception): `https://mats.mobile.dowjones.io/translate/<originId>/jpml` with `x-api-key: e05995ff442143255eb8381f72d4913bf7503d6c` and `User-Agent: okhttp/4.10.0`. This was calibre's full-body JPML route until it was removed on 2025-10-04. Assume dead, but one request is cheap.

### X

**Cookie auth does not exist in rss-bridge.** The strings `auth_token`, `ct0`, and `x.com` appear nowhere in `TwitterBridge.php`, `TwitterV2Bridge.php`, `FarsideNitterBridge.php`, or `lib/TwitterClient.php`. `TwitterClient` builds only:

```php
$headers = [
    'authorization' => sprintf('Bearer %s', $this->authorization),
    'x-guest-token' => $this->data['guest_token'] ?? null,
];
```

Its `oauth_token` and `oauth_token_secret` are blank strings marked `//Fill here`, editable only by patching `lib/TwitterClient.php` inside the container. There is no config key or env var for them.

**The only working path is `TwitterV2Bridge` plus a paid X API token.** X moved off fixed tiers to pay-per-use credits: "$0.005 per resource" for post reads, "$0.010" for users, cap of "3 million Post reads per monthly billing cycle", and "Resources are deduplicated within a 24-hour UTC day window". The 24-hour dedup means poll frequency is nearly free and new-post volume is what costs. Roughly 30 accounts polled hourly lands near 90 to 100 USD per month at the quoted rates (an estimate, not a quote). The rss-bridge doc page still recommends the old 200 USD "Basic" tier, which is stale; buy credits at `console.x.com`.

Config: `RSSBRIDGE_TwitterV2Bridge_twitterv2apitoken=<bearer>`. **The camelCase in that section name is load-bearing**, because `Configuration.php` does not case-fold the section segment during the split (although both `setConfig` and `getConfig` lowercase on store and read, so in practice it works either way; keep the exact class name for clarity).

RSSHub does support cookie auth via `TWITTER_AUTH_TOKEN` (comma-separated `auth_token` values) and understands `x.com` natively via `radar: [{ source: ['x.com/:id'] }]`. Its `TWITTER_USERNAME`/`TWITTER_PASSWORD` variables are commented out in current master despite the public docs still listing them. Standing up a second engine for one flaky, ToS-violating source is poor value on a 2-core box.

**Recommendation:** ship the router with X detection but no X ingestion, and surface a clear "X ingestion not configured" state. Explicitly exclude `TwitterBridge` and `FarsideNitterBridge` from `enabled_bridges` so a future `*` default cannot resurrect them. If the team names specific must-have accounts, buy credits and wire `TwitterV2Bridge`; it is a short job.

### If we do write FT and WSJ bridges

The precedent to copy is `EconomistBridge`, which is the closest existing analogue (single opaque cookie from a browser session, paywalled financial publisher). Shape:

```php
<?php

declare(strict_types=1);

class FinancialTimesBridge extends FeedExpander
{
    const NAME = 'Financial Times';
    const URI = 'https://www.ft.com/';
    const DESCRIPTION = 'FT with full text for an active subscription';
    const MAINTAINER = 'caf-vault';
    const CACHE_TIMEOUT = 1800;

    const CONFIGURATION = ['cookie' => ['required' => false]];

    const PARAMETERS = ['' => [
        'feed' => ['name' => 'FT RSS feed URL', 'type' => 'text', 'required' => true,
                   'defaultValue' => 'https://www.ft.com/markets?format=rss'],
        'limit' => self::LIMIT,
    ]];

    public function collectData()
    {
        $this->collectExpandableDatas($this->getInput('feed'), $this->getInput('limit') ?: 20);
    }

    protected function parseItem(array $item)
    {
        $cookie = $this->getOption('cookie');
        if (!$cookie) { return $item; }
        try {
            $dom = getSimpleHTMLDOM($item['uri'], ['Cookie: ' . $cookie]);
        } catch (Exception $e) {
            $this->logger->debug(sprintf('FT fetch failed for %s: %s', $item['uri'], $e->getMessage()));
            return $item;
        }
        $body = $dom->find('article#article-body', 0) ?: $dom->find('div.article__content-body', 0);
        if ($body) { $item['content'] = defaultLinkTo($body->innertext, static::URI); }
        return $item;
    }
}
```

Notes: the `try/catch` is mandatory because `getContents()` throws on any non-2xx/3xx and one 403 would otherwise abort the whole feed; returning the unmodified item degrades to the public excerpt; do **not** set a User-Agent header (it defeats curl-impersonate); `$this->logger` is available as `protected Logger $logger`; `self::LIMIT` is the shared const on `BridgeAbstract`.

FT's confirmed body selectors, from two independent maintained extractors: `//div[contains(concat(' ',normalize-space(@class),' '),' article__content-body ')]` (legacy) and `//article[@id='article-body']` (current). Calibre's current primary path is the JSON-LD `NewsArticle` block, taking `data['articleBody']` (paragraphs separated by `\n\n`, inline images as bracketed URLs). There is no `__NEXT_DATA__` on FT.

The realistic caveat: I did not test either bridge against live FT or WSJ, and both are behind edge blocks that reject plain HTTP clients regardless of cookies. curl-impersonate materially improves the odds versus stock curl but may not be enough, particularly for WSJ's DataDome. Budget for the possibility that the answer is the official API for FT and the audio-transcription path for WSJ.

---

## 5. YouTube audio path

Target chain: channel URL, resolve to `UC…`, native Atom feed for discovery, per-video captions via yt-dlp, whisper fallback on audio.

### Step 1: resolve to a channel ID

Preferred, and verified working from a datacenter-adjacent egress (unlike the player):

```python
opts = {'quiet': True, 'extract_flat': 'in_playlist', 'skip_download': True}
info = ydl.extract_info('https://www.youtube.com/@LinusTechTips/videos', download=False)
# info['channel_id'] == 'UCXuqSBlHAE6Xw-yeJA0Tunw'
# info['entries'][i] -> {'id', 'title', 'duration', 'url'}
```

This is a key-free handle resolver, full-history backfill source, and duration source in one call. It accepts `/@handle`, `/@handle/videos`, `/channel/UC…`, `/playlist?list=…`.

Fallback: fetch the channel HTML and extract, in preference order, the `<link rel="alternate" type="application/rss+xml">` href (which gives the feed URL outright), `<link rel="canonical">`, `<meta property="og:url">`, or `"externalId":"UC…"`. From EU/UK egress a browser User-Agent triggers a 302 to `consent.youtube.com`; adding `Cookie: SOCS=CAI` returns 200. The legacy `CONSENT=YES+cb` cookie no longer works. Pages are 2.3 to 2.4 MB.

### Step 2: discovery via the native Atom feed

```
https://www.youtube.com/feeds/videos.xml?channel_id=UC…
https://www.youtube.com/feeds/videos.xml?playlist_id=PL…
```

No key, no cookie, no User-Agent needed. `cache-control: public, max-age=900`, so poll at 15 minutes minimum. 15 entries for channels, no pagination, no historical backfill. Parse `yt:videoId`, `title`, `published`, `updated`, `media:description`, `media:statistics/@views`. The feed has no duration element, and the feed-level `yt:channelId` is missing the `UC` prefix (see section 3).

Store `media:description` as a fallback text body. For many research and finance channels the description carries real content, and it is the graceful degradation when captions are unavailable.

For backfill beyond 15 items, use yt-dlp `extract_flat` again with `playlistend`.

### Step 3: captions via yt-dlp

Preferred because it supports PO tokens, which `youtube-transcript-api` does not.

```python
opts = {
    'skip_download': True,
    'writesubtitles': True,        # --write-subs
    'writeautomaticsub': True,     # --write-auto-subs
    'subtitleslangs': ['en'],      # --sub-langs
    'subtitlesformat': 'json3',    # --sub-format ; json3 / srv1 / srv3 / ttml / vtt
}
```

On success, captions appear in `info['automatic_captions'][lang]` and `info['subtitles'][lang]` as lists of `{'ext', 'url', 'name'}`; fetch the `json3` URL directly rather than writing files. **This dict shape is unverified end to end** because every candidate video was bot-blocked during research.

`youtube-transcript-api` 1.2.4 is a lighter opportunistic first try (pure `requests`, no ffmpeg, no JS runtime):

```python
api = YouTubeTranscriptApi()          # or YouTubeTranscriptApi(proxy_config=...)
tl  = api.list(video_id)
ft  = api.fetch(video_id, languages=['en'])
ft.to_raw_data()[0]                   # {'text': ..., 'start': 1.36, 'duration': 1.68}
```

Exceptions: `RequestBlocked`, `IpBlocked`, `TranscriptsDisabled`, `NoTranscriptFound`, `VideoUnavailable`, `AgeRestricted`, `VideoUnplayable`. Proxy support via `WebshareProxyConfig(proxy_username=..., proxy_password=..., filter_ip_locations=["de","us"])` or `GenericProxyConfig`. Its cookie auth is currently broken per its own README.

### Bot detection reality

This is the part to size expectations around before writing code. From the research egress (GB, datacenter-adjacent):

```
yt_dlp.utils.DownloadError: ERROR: [youtube] MQeJYEN_lrg: Sign in to confirm you're not a bot.
```

**Every player client failed identically**: `default`, `tv_simply`, `tv`, `android_vr`, `visionos`, `mweb`, `ios`, `web_embedded`, `web_safari`, `android`. There is no client-switching workaround; older advice recommending `player_client=android` or `ios` is dead. With `ignore_no_formats_error: True` only the title survives, with `n_formats=0` and empty caption lists, because captions come from the player response.

`youtube-transcript-api` succeeded on **1 of 8** videos, deterministically per video (the one success repeated 5 out of 5). Its error text is explicit: "You are doing requests from an IP belonging to a cloud provider ... Unfortunately, most IPs from cloud providers are blocked by YouTube."

Cookies: `--cookies FILE` with a Netscape-format file exported from a workstation. `--cookies-from-browser` is useless on a headless server (no browser profile). Use a **throwaway** Google account; both yt-dlp and youtube-transcript-api warn the account gets banned.

PO tokens, from the yt-dlp README:

```
--extractor-args "youtube:po_token=web.gvs+XXX,web.player=XXX"
```

with context values `gvs` (video server URLs), `player` (Innertube player request), and **`subs` (Subtitles)**. The existence of a `subs` PO-token context is the likely explanation for the per-video transcript blocking pattern. Related: `fetch_pot` (`always`/`never`/`auto`), `formats=missing_pot`, `pot_trace`.

**Design for this being blocked.** Record a per-source `transcript_status` and degrade to metadata plus `media:description` rather than failing the ingest. Do not build the pipeline assuming a fixed success rate.

**Re-test both of these on the production server before committing to the caption tier**, since the research egress geolocates to GB and may not be representative: whether the production datacenter IP changes the transcript hit rate, and whether a throwaway-account `cookies.txt` clears the block.

### Step 4: whisper fallback on audio

Download audio only with **no postprocessor**:

```python
opts = {'format': 'm4a/bestaudio/best'}
```

This is a pure network copy with near-zero CPU, no ffmpeg merge and no transcode. Adding `FFmpegExtractAudio` (the `-x` equivalent) transcodes and is the one step that will saturate 2 cores. Prefer native m4a or opus passthrough and let the ASR consume it directly. The README warns: "What you need is ffmpeg binary, NOT the Python package of the same name."

### CPU and dependency constraints on the 2-core box

- **`yt-dlp-ejs` plus a JavaScript runtime is now a hard dependency for full YouTube support.** `pip install "yt-dlp[default]"` installs `yt-dlp-ejs` but **not** a runtime. The image must add `deno` (recommended) or `node`. Without it the `web` client is silently dropped from the default client list and signature deciphering degrades. There is `--extractor-args "youtube-ejs:jitless=true"` for better security at a CPU cost, which we cannot afford here.
- Install `pip install "yt-dlp[default,curl-cffi]"` for impersonation support (it did not help in testing, but it is cheap to have).
- Throttling knobs: `--sleep-requests SECONDS`, `--limit-rate RATE`, `--retries RETRIES`. **Pin `retries` low**; yt-dlp defaults to 10, which is a long stall.
- Run all yt-dlp work in a background worker, never inside a FastAPI request handler.
- Always pass info dicts through `ydl.sanitize_info()` before persisting; the README states the return value of `extract_info` is not guaranteed JSON-serialisable.
- Python 3.10+ (CPython).

If captions turn out to be reliably blocked, the two realistic alternatives are a residential proxy (already supported by `youtube-transcript-api` via `WebshareProxyConfig`) or downloading audio and running our own ASR. These imply very different infrastructure; decide which before building.

---

## 6. Facts we must not get wrong

Configuration and deployment:

- **`enabled_bridges[] = *` is the shipped default.** All 548 bridges are on out of the box. Set an explicit allowlist.
- **`whitelist.txt` overrides `config.ini.php`, and env vars override both.** Pick one mechanism (env, for compose) and document it.
- **Env section names cannot contain an underscore.** `RSSBRIDGE_<section>_<key>` splits on `_` and takes `$nameParts[1]` as the section. Name any custom bridge class without underscores.
- **Only the literal strings `true` and `false` coerce to boolean via env.** `1`, `yes`, `on` stay strings and fail boot validation with a hard 500 and `exit(1)`.
- Boot validation is fatal, not a warning. A bad `system.timezone` or `error.output` takes the whole container down at startup.
- **`config.ini.php` must start with `; <?php exit; ?> DO NOT REMOVE THIS LINE`.** `/app` is the nginx root and the file matches `location ~ \.php$`.
- **Changes in `/config` require a container restart.** The entrypoint runs once at start.
- The `/config` entrypoint only recognises five basename patterns: `*Bridge.php`, `*Format.php`, `config.ini.php`, `whitelist.txt`, `DEBUG`. Anything else is silently ignored, and files with a space in the name are skipped.
- Custom bridge filenames must end `Bridge.php` with no other dot, and the class name must equal the filename stem.
- **Do not set `[http] useragent`.** It disables curl-impersonate (`CURL_IMPERSONATE=chrome142`), which is the main reason this container gets past TLS fingerprinting.
- `[http] timeout = 5` is the default and is too low for any content-expanding bridge. Raise it, but stay under nginx's `fastcgi_read_timeout 45s`.
- `[system] max_file_size = 10000000` is the `simple_html_dom` cap and is a **different** setting from `[http] max_filesize` (20, in MB).
- **`/app/cache` is not under `/config`** and is lost on container recreation unless mounted.
- **There is no `vendor/` directory in the official image**, so `WebDriverAbstract` bridges fatal even with Selenium running. There is no `:webdriver` tag. rss-bridge gives us no JS execution.
- There is **no option to disable the HTML frontend**. Rely on not publishing the port. `frontpage` is the default action and instantiates every enabled bridge per request, uncached.
- The image serves `/app/docs/` and `/app/README.md` under the nginx root; only `/(\.|vendor|tests)` is denied.
- **An empty `authentication.token` silently disables token auth.** A misconfigured env var fails open. Assert on a 401 in a smoke test rather than trusting the config.
- Both auth modes gate **every** action including `health`, so a token-protected instance needs a credential-carrying healthcheck.

Error and cache behaviour:

- **`[error] output = "feed"` (the default) returns HTTP 200 with a synthetic feed item on failure**, with a `uid` that rotates daily, so a broken source manufactures a new fake article every 24 hours. Set `output = "http"`.
- That synthetic item embeds a GitHub issue URL containing the full `QUERY_STRING`, which is how a query-string credential ends up one click from a public tracker.
- **The upstream HTTP cache ignores request headers.** Key is `implode('_', ['server', $url, $requestBodyHash])`, TTL `86400 * 10` (ten days). An unauthenticated fetch and an authenticated fetch of the same URL collide.
- **Error responses are cached for 5 to 15 minutes** (`60*5 + rand(1, 60*10)`), so a transient 403 sticks.
- The response cache key is the entire query string including `format` and `token`, so `format=Json` and `format=Atom` are separate entries and any extra parameter busts the cache.
- `_cache_timeout` only works when `[cache] custom_timeout = true` (default false). `0` disables caching for that feed.
- `CacheMiddleware` calls `prune()` on 1% of requests, described in source as potentially resource intensive.

Routing:

- **`?action=detect` returns HTTP 200 with an HTML body on failure**, not 400. The published docs are wrong. Use `findfeed`.
- `detect`'s `location` header is relative and query-only (`?action=display&...`). Set `follow_redirects=False` if you only want classification.
- `findfeed` returns a **relative** `url` (`./?action=display&...`). Join it against our base.
- **Only 43 of 548 bridges implement `detectParameters()`**, and `YoutubeBridge`, `SubstackBridge`, `TwitterV2Bridge`, `BlueskyBridge`, `MastodonBridge`, and all the generic scrapers are not among them.
- **No bridge matches `x.com`.** Normalise before routing.
- `TwitterBridge`'s username regex is a catch-all, so `twitter.com/i/status/123` detects as user `i`. `FarsideNitterBridge` also claims plain twitter.com URLs and sorts earlier in `scandir` order.
- `TelegramBridge`'s detect regex is `$`-anchored: bare channel URLs only, no path suffix or query string.
- `ThreadsBridge::detectParameters` returns `$matches[3]` (the optional `@` capture group) instead of `$matches[4]`, which looks like a live bug. Do not rely on Threads detection.
- `findfeed` searches **active bridges only**, so a tight whitelist silently shrinks discovery as well as serving.
- One throwing bridge takes the entire `findfeed` call to 500. There are no partial results.
- `list` is **not** filtered by the whitelist; it returns everything with a `status` field.

Output format:

- `format=Json` is **JSON Feed v1**, not the internal item array.
- **`id` is a sha1**, not the source's native ID. Derive our own provenance key from `url`.
- **Titles are truncated to 150 characters plus `...`.** Never treat an rss-bridge title as authoritative.
- Keys are absent, not null, when unset. `content_html` and `content_text` are mutually exclusive and the `is_html()` heuristic is a `strip_tags` length comparison, so read both.
- `url` is dropped unless the URI matches `^https?://`.
- `feed_url` is built from `HTTP_HOST` and will read `rss-bridge:80`.
- Only `Plaintext` and `Json` carry a bridge's custom item keys through (as `_rssbridge.*`).
- `enclosures` versus `enclosure`: `FeedParser::parseRss2Item` populates the singular `enclosure` key, which `FeedItem::__set` does not recognise, so podcast audio may not reach `attachments[]`. Unverified end to end.

Per-source:

- **`feeds.a.dj.com` (WSJ legacy) returns HTTP 200 with content frozen at 27 Jan 2025.** Use `feeds.content.dowjones.io/public/rss/<SLUG>` and add a generic feed-staleness guard.
- FT and WSJ article HTML both return an anti-bot challenge to plain HTTP clients regardless of cookies (Cloudflare `cf-mitigated: challenge` and DataDome `x-datadome: protected` respectively). Add a `BLOCKED_BOT_CHALLENGE` status to the generic fetcher so a 270 KB challenge page is never stored as article text.
- Substack `/api/v1/posts/<slug>` returns **200 with a 473-byte body** for a locked post. Never gate on HTTP status; gate on `audience` plus body length.
- Substack `/feed` ignores `?limit=` and returns a variable 13 to 20 items. Use `/api/v1/archive` for pagination.
- YouTube's native feed has **no duration element** and its **feed-level `yt:channelId` is missing the `UC` prefix**.
- `YoutubeBridge` sets a global 16-minute rate-limit gate on any upstream 429 and then throws `RateLimitException` for **every** YouTube request in that window. Circuit-break per bridge, not per feed.
- rss-bridge per-bridge credentials are **instance-wide**. One container holds one `substack.sid`, one FT cookie, one WSJ cookie.
- Cookie lifetimes: Substack and Economist both documented at three months, with the maintainer warning sessions may die much sooner. Build expiry detection, not expiry prediction.
- FT robots.txt and T&C both prohibit machine-learning use of content by default and name `ClaudeBot`, `anthropic-ai`, and `Claude-Web`. FT sells a licence covering this.

---

## 7. Open questions and unverified items

Blocking or near-blocking:

1. **Substack authenticated fetch is unverified end to end.** No `substack.sid` was available. Validate with one real cookie against one known `only_paid` post, on both `/feed` and `/api/v1/posts/<slug>`, before building the pipeline on it.
2. **FT and WSJ full text.** Nothing verified works from a server. Decide between (a) the official APIs (FT Datamining Licence, Factiva Retrieval API), (b) a headless browser on residential egress, or (c) headlines-only plus the WSJ audio transcription path. This is a commercial and legal question first, an engineering one second.
3. **FT/WSJ licensing.** FT's T&C says "Each Subscription is for a single user only" and prohibits ML use without a licence; Dow Jones has equivalent restrictions. Three seats feeding a shared knowledge graph is materially different from personal reading. Get a decision before writing scrapers.
4. **YouTube caption availability from the production host.** Research egress geolocates to GB. Re-test (a) the transcript hit rate from the production IP and (b) whether a throwaway-account `cookies.txt` clears the block. If both fail, the choice is a residential proxy or our own ASR, and that changes the infrastructure plan.

Cheap probes worth running:

5. `https://mats.mobile.dowjones.io/translate/<originId>/jpml` with `x-api-key: e05995ff442143255eb8381f72d4913bf7503d6c` and `User-Agent: okhttp/4.10.0`. Blocked by DNS interception in the sandbox. Assume dead; one request confirms.
6. Whether `FTSession`/`FTSession_s` behave as assumed, and what `https://session-next.ft.com/{token}` returns for a valid token (expected: the `NextSession` JSON with `Products` and `Groups`).
7. Whether a headless browser with valid FT or WSJ subscriber cookies actually returns full body from a datacenter IP. Not testable without credentials; strongly expected to work for FT, less certain for WSJ's DataDome behavioural checks.

Facts I could not confirm:

8. **Docker Hub dated tag count.** One enumeration of all 1,744 tags found 8 date tags (newest `2025-08-05`); another pass reported 15, including 2022 and early-2023 tags. Confirm before pinning anything older than `2025-08-05`. Pinning `stable` or the digest `sha256:569f01f3faecd0d34d702e01b34eb0a769f7bedb84caf6dff29821d18b46f971` sidesteps this.
9. **php-fpm defaults in the image.** The shipped pool file sets only logging plus `clear_env = no`, and declares `[www]`, the same pool name as Debian's stock `www.conf` in the same directory. Whether php-fpm merges these or errors on a duplicate pool name was not verified. Watch the first-boot log. The presumed defaults (`pm = dynamic`, `pm.max_children = 5`, `memory_limit = 128M`) were not extracted from the image.
10. **HTTP Basic behind FastCGI.** `PHP_AUTH_USER`/`PHP_AUTH_PW` depend on the `Authorization` header reaching FastCGI. The shipped nginx config should forward it, but this was not run.
11. **WSJ cookie names** `djcs_session`, `djcs_auto`, `djcs_info` have no authoritative source; only `djcs_route` has a secondhand description. FT `FTSession`/`FTSession_s` are likewise undocumented.
12. **Session lifetimes** for FT and WSJ subscriber cookies are undocumented. Detect expiry rather than predict it.
13. **FT myFT RSS**: whether the `{uuid}` in `https://www.ft.com/myft/following/{uuid}.rss` is a user ID or a rotating secret, and whether the feed contains anything beyond headline plus teaser.
14. **JSON-LD on live WSJ article pages** was not confirmed (only the `/not-found` route was retrievable).
15. **yt-dlp caption dict shape** (`info['automatic_captions'][lang]` entries) was never observed live, because every candidate video was bot-blocked.
16. **Substack podcast enclosures through rss-bridge**: `FeedParser` populates `enclosure` (singular) while `FeedItem` recognises `enclosures` (plural), so audio may land in vendor fields rather than `attachments[]`. Not verified end to end.
17. **X API pricing history** (Basic tier closure dates, the 2M versus official 3M read cap) comes from secondary SEO-blog sources and conflicts with the official docs page. Trust `https://docs.x.com/x-api/getting-started/pricing`.
18. **Whether `TwitterV2Bridge` actually returns data** against the live X API today. File existence and 401-not-404 responses prove the routes exist; nothing was tested with a real token.
19. Whether the response cache can serve a cached body across differing auth states. The cache key includes `token`, so this appears safe, but `CacheMiddleware` runs before `TokenAuthenticationMiddleware` in the pipeline.
20. Whether upgrading rss-bridge ever breaks bridge names or parameter keys. The `Updating` documentation page is a zero-byte file, so there is no documented upgrade procedure. Pin a dated tag and re-read the source against that tag before relying on `bridgeParams` or any helper signature.
