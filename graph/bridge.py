"""Client for the private rss-bridge instance (build spec v4 §5.2; protocol
facts from docs/rss-bridge-brief.md).

rss-bridge is a PHP service that turns sites without feeds into feeds. Ours
runs inside the compose network only (`CAF_BRIDGE_URL`, e.g.
http://caf-rss-bridge); nothing routes it publicly. An optional shared token
(`CAF_BRIDGE_TOKEN`, rss-bridge's `[authentication] token`) rides on every
call when set. Three actions matter:

    ?action=findfeed&url=<page>&format=Json   bridges whose detectParameters()
                                              match; 404 JSON when none do
    ?action=display&bridge=<Class>&format=Json&<params>  the feed as JSON Feed v1
    ?action=list                              every bridge on disk, with
                                              status active|inactive

findfeed only covers bridges that implement detectParameters() (about 43 of
548; not Substack, YouTube, TwitterV2, FT or WSJ), so the router uses it as
the last rule, never the first. Display output is JSON Feed v1: items carry
`url`, `title` (truncated to 150 chars upstream), `content_html` or
`content_text`, `date_modified` (RFC 3339), `author.name`, `attachments[]`,
`tags[]`, and `id` (a sha1 of the bridge's uid, stable but opaque). Item
shapes are normalized here so callers never see those key names.
"""
import os
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

TIMEOUT = 20
# preference when several bridges claim one URL (findfeed returns all)
PREFERRED = ("TelegramBridge", "BlueskyBridge", "RedditBridge", "MastodonBridge",
             "YoutubeBridge", "SubstackBridge")


class BridgeError(Exception):
    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = _scrub(detail)[:200]
        super().__init__(f"bridge HTTP {status}: {self.detail}")


def base_url() -> str:
    return (os.environ.get("CAF_BRIDGE_URL") or "").rstrip("/")


def token() -> str:
    return (os.environ.get("CAF_BRIDGE_TOKEN") or "").strip()


def enabled() -> bool:
    return bool(base_url())


def _params(params: dict) -> dict:
    q = dict(params)
    if token():
        q["token"] = token()
    return q


def _scrub(text: str) -> str:
    """The token rides in the query string (rss-bridge takes it nowhere else),
    so it is part of every request URL — and requests' transport errors quote
    that URL in full. Nothing leaves this module with the token in it."""
    out = str(text or "")
    secret = token()
    return out.replace(secret, "***") if secret else out


def _call(params: dict, timeout: int = TIMEOUT):
    """One GET at the bridge. A transport failure (the container is down, DNS
    has not caught up after a reboot) becomes a BridgeError with a fixed
    message, never the library's text."""
    try:
        return requests.get(base_url() + "/", params=_params(params),
                            timeout=timeout,
                            headers={"Accept": "application/json"})
    except requests.RequestException:
        raise BridgeError(0, "bridge unreachable") from None


def _get(params: dict, timeout: int = TIMEOUT):
    r = _call(params, timeout)
    if r.status_code >= 400:
        raise BridgeError(r.status_code, r.text)
    return r


def display_url(bridge: str, params: dict) -> str:
    """The relative display URL for a bridge + params (stored in
    source.config for transparency; the poller rebuilds it from parts). The
    token is never part of it."""
    q = {"action": "display", "bridge": bridge, "format": "Json"}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    return "/?" + urlencode(q)


_RESERVED = ("action", "bridge", "format", "token", "_noproxy",
             "_cache_timeout", "_error_time", "_")


def _split_display_url(u: str) -> tuple[str | None, dict]:
    """'./?action=display&context=x&u=y&bridge=X&format=Json' -> ('X', {...})."""
    q = dict(parse_qsl(urlsplit(u).query, keep_blank_values=True))
    bridge = q.pop("bridge", None)
    for k in _RESERVED:
        q.pop(k, None)
    return bridge, q


def findfeed(url: str, timeout: int = TIMEOUT) -> list[dict]:
    """[{bridge, params, url (relative display url), name}] for enabled
    bridges whose detectParameters() accept the URL, best first; [] when none
    match (rss-bridge answers 404 + JSON for that, which is not an error
    here). The class name lives in bridgeParams.bridge; bridgeMeta.name is the
    human name."""
    if not enabled():
        return []
    r = _call({"action": "findfeed", "url": url, "format": "Json"}, timeout)
    if r.status_code == 404:
        return []
    if r.status_code >= 400:
        raise BridgeError(r.status_code, r.text)
    try:
        data = r.json()
    except ValueError:
        return []
    out = []
    for hit in data if isinstance(data, list) else []:
        if not isinstance(hit, dict):
            continue
        u = hit.get("url") or ""
        bridge, params = _split_display_url(u)
        meta = hit.get("bridgeMeta") or {}
        bp = hit.get("bridgeParams")
        if isinstance(bp, dict) and bp:
            bridge = bp.get("bridge") or bridge
            params = {k: str(v) for k, v in bp.items()
                      if k not in _RESERVED and v not in (None, "")}
        if not bridge:
            continue
        out.append({"bridge": bridge, "params": params,
                    "url": display_url(bridge, params),
                    "name": meta.get("name") or bridge})
    out.sort(key=lambda h: PREFERRED.index(h["bridge"])
             if h["bridge"] in PREFERRED else len(PREFERRED))
    return out


def display(bridge: str, params: dict, timeout: int = TIMEOUT) -> dict:
    """The feed as {title, url, items:[{title, url, published, content_html,
    author, uid}]}. `published` is the JSON Feed date string or ''."""
    q = {"action": "display", "bridge": bridge, "format": "Json"}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    r = _get(q, timeout)
    try:
        data = r.json()
    except ValueError:
        raise BridgeError(r.status_code, "non-JSON response") from None
    items = []
    for it in data.get("items") or []:
        author = it.get("author")
        if isinstance(author, dict):
            author = author.get("name")
        elif not author and it.get("authors"):
            first = it["authors"][0]
            author = first.get("name") if isinstance(first, dict) else first
        items.append({
            "title": (it.get("title") or "").strip(),
            "url": it.get("url") or it.get("external_url") or "",
            "published": it.get("date_published") or it.get("date_modified") or "",
            "content_html": it.get("content_html") or it.get("content_text") or "",
            "author": str(author or ""),
            "uid": it.get("id") or it.get("url") or "",
        })
    return {"title": data.get("title") or bridge, "url": data.get("home_page_url") or "",
            "items": items}


def list_bridges(timeout: int = TIMEOUT) -> list[dict]:
    """[{name, uri, description, parameters}] of enabled bridges."""
    r = _get({"action": "list", "format": "json"}, timeout)
    try:
        data = r.json()
    except ValueError:
        return []
    bridges = data.get("bridges") if isinstance(data, dict) else data
    out = []
    if isinstance(bridges, dict):
        for name, b in bridges.items():
            out.append({"name": name, **(b if isinstance(b, dict) else {})})
    elif isinstance(bridges, list):
        out = [b for b in bridges if isinstance(b, dict)]
    return out


def health(timeout: int = 5) -> bool:
    """?action=health -> {"code": 200, "message": "all is good"}."""
    if not enabled():
        return False
    try:
        r = requests.get(base_url() + "/", params=_params({"action": "health"}),
                         timeout=timeout)
        return r.status_code == 200 and "all is good" in r.text
    except Exception:
        return False
