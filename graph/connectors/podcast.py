"""Podcast connector: live RSS -> enclosure mp3 -> local mlx-whisper transcript.

No diarization in v0 — transcripts are plain text. The design's
speakers-are-entities step needs pyannote later.
"""
import re
import subprocess
import tempfile
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import requests

from .. import db, envelope

FEEDS = {
    "unhedged": "https://feeds.acast.com/public/shows/unhedged",
    "aidailybrief": "https://anchor.fm/s/f7cac464/podcast/rss",
}
MODEL = "mlx-community/whisper-large-v3-turbo"
# CDNs (Acast et al.) 403 the default requests UA
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def episodes(rss_text: str):
    for item in re.findall(r"<item>(.*?)</item>", rss_text, re.S):
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        enc = re.search(r'<enclosure[^>]*url="([^"]+)"', item)
        if not (title and enc):
            continue
        date = parsedate_to_datetime(pub.group(1)).date().isoformat() if pub else ""
        yield unescape(title.group(1).strip()), date, unescape(enc.group(1))


def transcribe(mp3: Path) -> str:
    tmp = mp3.parent / "tx"
    tmp.mkdir(exist_ok=True)
    subprocess.run(
        ["uvx", "--from", "mlx-whisper", "mlx_whisper", str(mp3),
         "--model", MODEL, "--output-dir", str(tmp), "--output-format", "txt"],
        check=True,
    )
    return (tmp / (mp3.stem + ".txt")).read_text(encoding="utf-8")


def poll(con, feeds=None, episodes_per_feed=2):
    counts = {"new": 0, "duplicate": 0, "errors": 0}
    for feed in (list(feeds) if feeds else list(FEEDS)):
        url = FEEDS[feed]
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"error {feed}: feed fetch failed ({e})")
            counts["errors"] += 1
            continue
        source_id = db.get_or_create_source(con, f"podcast:{feed}", "podcast", url=url)
        for title, date, enc_url in list(episodes(r.text))[:episodes_per_feed]:
            if con.execute("select 1 from event where meta->>'enclosure_url'=%s limit 1",
                           (enc_url,)).fetchone():
                counts["duplicate"] += 1
                continue
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    mp3 = Path(tmp) / "episode.mp3"
                    print(f"downloading: {title}")
                    audio = requests.get(enc_url, timeout=180, headers=HEADERS)
                    audio.raise_for_status()
                    mp3.write_bytes(audio.content)
                    print(f"transcribing: {title}")
                    text = transcribe(mp3)
            except Exception as e:
                print(f"error {feed} '{title}': {e}")
                counts["errors"] += 1
                continue
            doc = (f"# title: {title}\n# source_type: podcast\n"
                   f"# published: {date}\n# feed: {feed}\n---\n{text}\n")
            _, is_new = envelope.ingest(
                con, source_id, "podcast", doc.encode("utf-8"), "text/plain", ".txt",
                published_at=date or None,
                meta={"feed": feed, "title": title, "enclosure_url": enc_url})
            counts["new" if is_new else "duplicate"] += 1
    print(f"podcast poll: {counts}")
    return counts
