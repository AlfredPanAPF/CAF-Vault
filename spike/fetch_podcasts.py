#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["requests"]
# ///
"""Download recent episodes from the saved RSS feeds and transcribe them locally
with mlx-whisper (invoked through uvx; Apple Silicon).

No diarization in the spike — transcripts are plain text. The design's
speakers-are-entities step needs pyannote later.
"""
import argparse
import re
import subprocess
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import requests

ROOT = Path(__file__).parent
FEEDS = {
    "unhedged": ROOT / "corpus/raw/podcasts/unhedged.rss",
    "aidailybrief": ROOT / "corpus/raw/podcasts/anchor_feed.rss",
}
AUDIO = ROOT / "corpus/raw/podcasts/audio"
OUT = ROOT / "corpus/text"
MODEL = "mlx-community/whisper-large-v3-turbo"
# CDNs (Acast et al.) 403 the default requests UA
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


def slugify(s: str, maxlen: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:maxlen].rstrip("-")


def episodes(feed_path: Path):
    data = feed_path.read_text(encoding="utf-8", errors="replace")
    for item in re.findall(r"<item>(.*?)</item>", data, re.S):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="episodes per feed")
    args = ap.parse_args()
    AUDIO.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    for feed, path in FEEDS.items():
        for title, date, url in list(episodes(path))[: args.n]:
            slug = slugify(title)
            out = OUT / f"podcast_{feed}_{date}_{slug}.txt"
            if out.exists():
                print(f"skip (exists): {out.name}")
                continue
            mp3 = AUDIO / f"{feed}_{date}_{slug}.mp3"
            try:
                if not mp3.exists():
                    print(f"downloading: {title}")
                    r = requests.get(url, timeout=180, headers=HEADERS)
                    r.raise_for_status()
                    mp3.write_bytes(r.content)
                print(f"transcribing: {title}")
                text = transcribe(mp3)
            except Exception as e:
                print(f"SKIP {title}: {e}")
                continue
            out.write_text(
                f"# title: {title}\n# source_type: podcast\n# published: {date}\n"
                f"# origin: {feed}\n---\n{text}\n",
                encoding="utf-8",
            )
            print(f"{out.name}: {len(text)} chars")


if __name__ == "__main__":
    main()
