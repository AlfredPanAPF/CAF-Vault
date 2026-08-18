"""YouTube connector: channels and playlists as transcript documents
(build spec v4 §6.1).

Videos are audio content here. A video becomes one text event whose body is
the transcript — captions when the video has them, whisper only when ASR is
switched on. No frames, no thumbnails.

The public feed (`feeds/videos.xml`) needs no sign-in, but per-video access
from a server IP is bot-walled, so the transcript step runs yt-dlp with a
cookies.txt written from the `youtube` credential. When YouTube answers with
its bot wall anyway, `SignInNeeded` stops that source for the cycle and the
source shows "Sign-in needed" on the sources page.
"""
import json
import os
import re
import shutil
import tempfile
from html import unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from psycopg.types.json import Jsonb

from .. import credentials, db, envelope, fetch
from . import podcast

FEED_CHANNEL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
FEED_PLAYLIST = "https://www.youtube.com/feeds/videos.xml?playlist_id={}"
WATCH_URL = "https://www.youtube.com/watch?v={}"
# consent cookies: without them the channel page redirects to consent.youtube.com
CONSENT_COOKIES = {"SOCS": "CAI", "CONSENT": "YES+1"}
CAPTION_LANGS = ("en", "en-US", "en-GB", "en-orig", "en-auto")
PROBE_VIDEO = "dQw4w9WgXcQ"   # the credential test in §7 reads this one video
# the wall reads "Sign in to confirm you're not a bot. Use --cookies-from-
# browser ...", so these two match it. A bare "cookies" would also match a
# malformed cookiefile or an unrelated extractor message, and those are
# retryable errors, not a source-wide sign-in stop.
BOT_WALL_MARKERS = ("sign in to confirm", "not a bot")
# a floor on audio bitrate, so a mis-reported duration cannot become an
# unbounded write into the worker's temp dir
AUDIO_BYTES_PER_SECOND = 32_000


def per_cycle() -> int:
    try:
        return max(1, int(os.environ.get("CAF_YT_PER_CYCLE", "3")))
    except ValueError:
        return 3


def max_minutes() -> int:
    try:
        return max(1, int(os.environ.get("CAF_YT_MAX_MINUTES", "120")))
    except ValueError:
        return 120


def feed_url(config: dict) -> str | None:
    config = config or {}
    if config.get("channel_id"):
        return FEED_CHANNEL.format(config["channel_id"])
    if config.get("playlist_id"):
        return FEED_PLAYLIST.format(config["playlist_id"])
    return None


# ---------------------------------------------------------------- feed


def entries(xml: str) -> list[dict]:
    """The native videos.xml -> [{video_id, title, published, channel, url}],
    newest first (YouTube already orders it that way)."""
    out = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml or "", re.S):
        vid = re.search(r"<yt:videoId>(.*?)</yt:videoId>", block)
        if not vid:
            m = re.search(r"[?&]v=([\w-]+)", block)
            vid = m
        if not vid:
            continue
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                          block, re.S)
        pub = re.search(r"<published>(.*?)</published>", block)
        author = re.search(r"<author>.*?<name>(.*?)</name>", block, re.S)
        video_id = vid.group(1).strip()
        out.append({
            "video_id": video_id,
            "title": unescape(title.group(1).strip()) if title else video_id,
            "published": (pub.group(1)[:10] if pub else ""),
            "channel": unescape(author.group(1).strip()) if author else "",
            "url": WATCH_URL.format(video_id),
        })
    return out


def feed_title(xml: str) -> str:
    head = (xml or "").split("<entry>", 1)[0]
    m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", head, re.S)
    return unescape(m.group(1).strip()) if m else ""


# ---------------------------------------------------------------- yt-dlp


def _cookiefile(con, tmpdir: str) -> str | None:
    """The youtube credential as a Netscape cookies.txt on disk, for yt-dlp."""
    if con is None:
        return None
    value = credentials.value(con, "youtube")
    if not value:
        return None
    try:
        text = credentials.netscape_text(value, site="youtube")
    except credentials.InvalidCredential:
        return None
    path = Path(tmpdir) / "cookies.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _ydl_opts(cookiefile: str | None, extra: dict | None = None) -> dict:
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "extractor_args": {"youtube": {"player_client": ["web", "mweb"]}},
        # yt-dlp's defaults (10 retries, no socket timeout) turn one walled
        # video into a many-minute stall inside a worker stage
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if shutil.which("node"):
        # Python API form: {runtime: {config}} (the CLI takes a list). Only
        # deno is enabled by default; the image ships node instead.
        opts["js_runtimes"] = {"node": {}}
    if extra:
        opts.update(extra)
    return opts


def _is_bot_wall(err: Exception) -> bool:
    text = str(err).lower()
    return any(marker in text for marker in BOT_WALL_MARKERS)


def _extract_info(url: str, cookiefile: str | None, extra: dict | None = None,
                  download: bool = False):
    import yt_dlp   # lazy: heavy import, and the tests substitute a fake module
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(cookiefile, extra)) as ydl:
            return ydl.extract_info(url, download=download)
    except fetch.SignInNeeded:
        raise
    except Exception as e:
        if _is_bot_wall(e):
            raise fetch.SignInNeeded("youtube", str(e)[:200]) from None
        raise


def _json3_url(url: str) -> str:
    """Caption URLs take the format as a query parameter, so any variant can
    be asked for as json3."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "fmt"]
    query.append(("fmt", "json3"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), ""))


def caption_track(info: dict) -> dict | None:
    """The best English caption track as {url, lang, kind}: real subtitles
    first, then automatic captions, json3 either way."""
    for kind in ("subtitles", "automatic_captions"):
        tracks = info.get(kind) or {}
        for lang in CAPTION_LANGS:
            variants = [v for v in (tracks.get(lang) or []) if v.get("url")]
            if not variants:
                continue
            chosen = next((v for v in variants if v.get("ext") == "json3"),
                          variants[0])
            url = chosen["url"] if chosen.get("ext") == "json3" \
                else _json3_url(chosen["url"])
            return {"url": url, "lang": lang, "kind": kind}
    return None


def caption_text(payload: str) -> str:
    """json3 caption payload -> one whitespace-collapsed block of text."""
    try:
        data = json.loads(payload)
    except ValueError:
        return ""
    parts = []
    for event in data.get("events") or []:
        for seg in event.get("segs") or []:
            parts.append(seg.get("utf8") or "")
    text = "".join(parts)
    return re.sub(r"\s+", " ", text).strip()


def transcript(video_id: str, con=None):
    """(text, transcript_source, info) for one video, spec §6.1.

    transcript_source is 'captions', 'whisper', or None when the video is
    skipped; the reason then sits in info['skip_reason']. Raises
    fetch.SignInNeeded when YouTube answers with its bot wall."""
    url = WATCH_URL.format(video_id)
    with tempfile.TemporaryDirectory() as tmp:
        cookiefile = _cookiefile(con, tmp)
        info = _extract_info(url, cookiefile) or {}
        track = caption_track(info)
        if track:
            payload = fetch.get(track["url"], site="youtube", con=con,
                                timeout=30, allow_wall=True).text
            text = caption_text(payload)
            if text:
                return text, "captions", info

        engine = podcast.asr_engine()
        if engine == "off":
            info["skip_reason"] = "No captions, and transcription is off."
            return "", None, info
        duration = int(info.get("duration") or 0)
        if duration > max_minutes() * 60:
            info["skip_reason"] = (f"No captions, and the video is longer than "
                                   f"{max_minutes()} minutes.")
            return "", None, info
        # a live broadcast, a premiere, or any extraction without a duration
        # has no end for the downloader to stop at: yt-dlp would record until
        # the broadcast finishes and hold the whole youtube stage open
        if not duration or info.get("is_live") or info.get("live_status") in (
                "is_live", "is_upcoming", "post_live"):
            info["skip_reason"] = ("No captions, and the video is live or has "
                                   "no length yet.")
            return "", None, info
        print(f"downloading audio: {video_id}")
        audio_dir = Path(tmp) / "audio"
        audio_dir.mkdir(exist_ok=True)
        got = _extract_info(url, cookiefile, {
            "skip_download": False,
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": str(audio_dir / "%(id)s.%(ext)s"),
            "max_filesize": max_minutes() * 60 * AUDIO_BYTES_PER_SECOND,
        }, download=True) or info
        files = [f for f in audio_dir.glob("*") if f.is_file()]
        if not files:
            info["skip_reason"] = "No captions, and the audio did not download."
            return "", None, info
        audio = max(files, key=lambda f: f.stat().st_size)
        print(f"transcribing ({engine}): {video_id}")
        text = podcast.transcribe(audio, engine)
        info = got or info
        return text, "whisper", info


# ---------------------------------------------------------------- router help


def resolve_channel(url: str, con=None) -> dict | None:
    """Channel page (@handle, /c/name, /user/name) -> {channel_id, title,
    handle}, for the URL router (§4 rule 5). Consent cookies only; no sign-in."""
    try:
        html = fetch.get(url, timeout=8, max_bytes=fetch.HEAD_BYTES,
                         cookies=CONSENT_COOKIES).text
    except Exception as e:
        print(f"youtube: channel page fetch failed ({e})")
        html = ""
    channel_id = ""
    m = re.search(r'<link rel="canonical" href="[^"]*?/channel/(UC[\w-]+)"', html)
    if not m:
        m = re.search(r'"externalId"\s*:\s*"(UC[\w-]+)"', html)
    if m:
        channel_id = m.group(1)
    title = ""
    t = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    if not t:
        t = re.search(r"<title>(.*?)</title>", html, re.S)
    if t:
        title = unescape(t.group(1)).removesuffix(" - YouTube").strip()
    if not channel_id:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                info = _extract_info(url, _cookiefile(con, tmp),
                                     {"extract_flat": True, "playlist_items": "0"})
        except Exception as e:
            print(f"youtube: channel resolve failed ({e})")
            return None
        channel_id = (info or {}).get("channel_id") or (info or {}).get("id") or ""
        title = title or (info or {}).get("channel") or (info or {}).get("title") or ""
        if not channel_id.startswith("UC"):
            return None
    handle = ""
    h = re.search(r"/@([\w.-]+)", url)
    if h:
        handle = "@" + h.group(1)
    return {"channel_id": channel_id, "title": title, "handle": handle}


# ---------------------------------------------------------------- poll


def document(entry: dict, text: str, info: dict) -> str:
    duration = int((info or {}).get("duration") or 0)
    channel = entry.get("channel") or (info or {}).get("channel") or ""
    return (f"# title: {entry['title']}\n# source_type: video\n"
            f"# published: {entry.get('published', '')}\n# channel: {channel}\n"
            f"# duration: {duration}s\n---\n{text}\n")


def _remember_skip(con, source_id, skipped: list, video_id: str) -> list:
    """Add one video to the source's skip list, newest last, and keep the last
    200. Insertion order matters: sorting the list before the cap would keep
    the 200 ids that sort highest, so older ones fall out and are retried every
    cycle — the yt-dlp call the list exists to avoid."""
    if video_id in skipped:
        return skipped
    skipped = [*skipped, video_id][-200:]
    con.execute("update source set config = coalesce(config, '{}'::jsonb) || %s "
                "where source_id=%s", (Jsonb({"skipped": skipped}), source_id))
    return skipped


def poll(con, per_source=None):
    """Poll active youtube source rows: newest N videos per channel/playlist
    that are not already events, each as a transcript document."""
    counts = {"new": 0, "duplicate": 0, "skipped": 0, "blocked": 0, "errors": 0}
    limit = per_source or per_cycle()
    sources = con.execute(
        "select source_id, name, url, config from source "
        "where connector='youtube' and status='active' order by name").fetchall()
    for src in sources:
        name = src["name"]
        config = src["config"] or {}
        url = feed_url(config) or src["url"]
        if not url:
            print(f"skip {name}: no channel or playlist id")
            counts["errors"] += 1
            continue
        try:
            r = fetch.get(url, con=con, timeout=30, allow_wall=True)
            if not r.ok:
                # videos.xml answers a stray 404/5xx now and then even for
                # a live channel; one retry tells that from a deleted one
                r = fetch.get(url, con=con, timeout=30, allow_wall=True)
            if not r.ok:
                # a deleted channel's videos.xml 404s; without this the parse
                # finds nothing, last_error is cleared, and the source looks
                # freshly polled and healthy forever
                raise RuntimeError(f"HTTP {r.status}")
            xml = r.text
        except Exception as e:
            print(f"error {name}: feed fetch failed ({e})")
            _note_error(con, src["source_id"], "Feed unreachable")
            counts["errors"] += 1
            continue
        con.execute("update source set last_polled=now(), last_error=null, "
                    "last_error_at=null where source_id=%s", (src["source_id"],))
        skipped = list(config.get("skipped") or [])
        seen_skipped = set(skipped)
        todo = []
        for entry in entries(xml):
            if entry["video_id"] in seen_skipped:
                continue
            if con.execute("select 1 from event where meta->>'video_id'=%s limit 1",
                           (entry["video_id"],)).fetchone():
                counts["duplicate"] += 1
                continue
            todo.append(entry)
            if len(todo) >= limit:
                break
        for entry in todo:
            try:
                text, source_kind, info = transcript(entry["video_id"], con)
            except fetch.SignInNeeded as e:
                print(f"blocked {name} '{entry['title']}': {e}")
                _note_error(con, src["source_id"], "Sign-in needed")
                counts["blocked"] += 1
                break   # the wall is per IP, not per video
            except Exception as e:
                print(f"error {name} '{entry['title']}': {e}")
                counts["errors"] += 1
                continue
            if not source_kind or not text.strip():
                reason = (info or {}).get("skip_reason") or "No transcript."
                print(f"skip {name} '{entry['title']}': {reason}")
                skipped = _remember_skip(con, src["source_id"], skipped,
                                         entry["video_id"])
                seen_skipped.add(entry["video_id"])
                counts["skipped"] += 1
                continue
            info = info or {}
            meta = {"video_id": entry["video_id"], "item_url": entry["url"],
                    "channel": entry.get("channel") or info.get("channel") or "",
                    "title": entry["title"],
                    "duration_s": int(info.get("duration") or 0),
                    "transcript_source": source_kind, "feed": name}
            doc = document(entry, text, info)
            _, is_new = envelope.ingest(
                con, src["source_id"], "youtube", doc.encode("utf-8"),
                "text/plain", ".txt", published_at=entry.get("published") or None,
                meta=meta)
            counts["new" if is_new else "duplicate"] += 1
    print(f"youtube poll: {counts}")
    return counts


def _note_error(con, source_id, message: str) -> None:
    con.execute("update source set last_error=%s, last_error_at=now() "
                "where source_id=%s", (message, source_id))


def ingest_video(con, video_id: str, source_id=None) -> dict:
    """One video into an event, for the one-off link queue (§6.3). Returns
    {event_id, is_new, title, published, skip_reason}."""
    text, source_kind, info = transcript(video_id, con)
    info = info or {}
    entry = {"video_id": video_id, "title": info.get("title") or video_id,
             "published": _upload_date(info),
             "channel": info.get("channel") or info.get("uploader") or "",
             "url": WATCH_URL.format(video_id)}
    if not source_kind or not text.strip():
        return {"event_id": None, "is_new": False, "title": entry["title"],
                "published": entry["published"],
                "skip_reason": info.get("skip_reason") or "No transcript."}
    if source_id is None:
        source_id = db.get_or_create_source(con, "link:youtube.com", "link",
                                            url=None, is_internal=False)
    meta = {"video_id": video_id, "item_url": entry["url"],
            "channel": entry["channel"], "title": entry["title"],
            "duration_s": int(info.get("duration") or 0),
            "transcript_source": source_kind, "site": "youtube"}
    event_id, is_new = envelope.ingest(
        con, source_id, "youtube", document(entry, text, info).encode("utf-8"),
        "text/plain", ".txt", published_at=entry["published"] or None, meta=meta)
    return {"event_id": event_id, "is_new": is_new, "title": entry["title"],
            "published": entry["published"], "skip_reason": None}


def check(con) -> tuple[bool, str]:
    """Live probe for the youtube credential (§7): yt-dlp reads a known video
    with the cookiefile. Returns (ok, message)."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            info = _extract_info(WATCH_URL.format(PROBE_VIDEO),
                                 _cookiefile(con, tmp))
    except fetch.SignInNeeded as e:
        return False, f"Sign-in failed: {e.detail or 'YouTube asked for a sign-in'}."
    except Exception as e:
        return False, f"Sign-in failed: {str(e)[:200]}."
    if not info:
        return False, "Sign-in failed: YouTube returned nothing."
    return True, "Signed in."


def _upload_date(info: dict) -> str:
    raw = str((info or {}).get("upload_date") or "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return ""
