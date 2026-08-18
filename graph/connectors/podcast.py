"""Podcast connector: live RSS -> enclosure mp3 -> local whisper transcript.

ASR engine is picked via CAF_ASR: "auto" (default; mlx on Apple Silicon,
faster-whisper elsewhere), "mlx" (uvx mlx-whisper subprocess), "faster-whisper"
(python API, CPU int8 large-v3; model cache honors HF_HOME), or "off" (skip
podcast ingestion entirely, with a log line).

No diarization in v0 — transcripts are plain text. The design's
speakers-are-entities step needs pyannote later.
"""
import os
import platform
import re
import subprocess
import sys
import tempfile
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import requests

from .. import envelope

# Seed data only: `graph seed` upserts these as source rows (connector
# 'podcast', name 'podcast:<feed>'); poll() reads the DB, never this dict.
FEEDS = {
    "unhedged": "https://feeds.acast.com/public/shows/unhedged",
    "aidailybrief": "https://anchor.fm/s/f7cac464/podcast/rss",
}
MODEL = "mlx-community/whisper-large-v3-turbo"
# faster-whisper model, overridable per deployment. "small" holds ~500MB and
# runs near realtime on two CPU cores; "large-v3" wants ~3GB and froze the
# 2-core production box on 2026-08-17. Larger models are opt-in via env.
FW_MODEL = os.environ.get("CAF_ASR_MODEL", "small")
# CDNs (Acast et al.) 403 the default requests UA
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
# ceiling for one episode download: about six hours of 128 kbps audio. The
# worker has a 2 GB cap, so an enclosure with no Content-Length (or a lying
# one) must not be buffered or written without a limit.
MAX_AUDIO_BYTES = int(os.environ.get("CAF_PODCAST_MAX_BYTES") or 350_000_000)


def asr_engine() -> str:
    eng = (os.environ.get("CAF_ASR") or "auto").strip().lower()
    if eng == "auto":
        return ("mlx" if sys.platform == "darwin" and platform.machine() == "arm64"
                else "faster-whisper")
    return eng


def episodes(rss_text: str):
    for item in re.findall(r"<item>(.*?)</item>", rss_text, re.S):
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        enc = re.search(r'<enclosure[^>]*url="([^"]+)"', item)
        if not (title and enc):
            continue
        date = parsedate_to_datetime(pub.group(1)).date().isoformat() if pub else ""
        yield unescape(title.group(1).strip()), date, unescape(enc.group(1))


def download(url: str, path: Path, max_bytes: int = MAX_AUDIO_BYTES) -> Path:
    """One episode to disk, streamed and capped. Buffering the whole response
    in memory first is what a 2-core box cannot afford, and a feed that hands
    back a multi-gigabyte file must stop the episode, not the worker."""
    r = requests.get(url, timeout=180, headers=HEADERS, stream=True)
    try:
        r.raise_for_status()
        written = 0
        with path.open("wb") as fh:
            for chunk in r.iter_content(262_144):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeError(
                        f"the episode is larger than {max_bytes // 1_000_000} MB")
                fh.write(chunk)
    finally:
        r.close()
    return path


_fw_model = None  # loaded once per process; large-v3 int8 is ~1.5GB resident


def transcribe(mp3: Path, engine: str | None = None) -> str:
    engine = engine or asr_engine()
    if engine == "mlx":
        tmp = mp3.parent / "tx"
        tmp.mkdir(exist_ok=True)
        subprocess.run(
            ["uvx", "--from", "mlx-whisper", "mlx_whisper", str(mp3),
             "--model", MODEL, "--output-dir", str(tmp), "--output-format", "txt"],
            check=True,
        )
        return (tmp / (mp3.stem + ".txt")).read_text(encoding="utf-8")
    if engine == "faster-whisper":
        # lazy import — the dep is optional (pip install ".[asr]")
        global _fw_model
        from faster_whisper import WhisperModel
        if _fw_model is None:
            _fw_model = WhisperModel(FW_MODEL, device="cpu", compute_type="int8")
        segments, _info = _fw_model.transcribe(str(mp3))
        return "".join(seg.text for seg in segments).strip()
    raise ValueError(f"unknown CAF_ASR engine: {engine!r}")


def poll(con, feeds=None, episodes_per_feed=2):
    """Poll active podcast source rows. `feeds` optionally restricts to those
    feed names (source name minus the 'podcast:' prefix)."""
    counts = {"new": 0, "duplicate": 0, "errors": 0}
    engine = asr_engine()
    if engine == "off":
        print("podcast poll: CAF_ASR=off — skipping podcast ingestion")
        return counts
    sources = con.execute(
        "select source_id, name, url from source "
        "where connector='podcast' and status='active' order by name").fetchall()
    for src in sources:
        feed = src["name"].removeprefix("podcast:")
        if feeds and feed not in feeds:
            continue
        source_id = src["source_id"]
        try:
            r = requests.get(src["url"], headers=HEADERS, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"error {feed}: feed fetch failed ({e})")
            counts["errors"] += 1
            continue
        con.execute("update source set last_polled=now() where source_id=%s",
                    (source_id,))
        for title, date, enc_url in list(episodes(r.text))[:episodes_per_feed]:
            if con.execute("select 1 from event where meta->>'enclosure_url'=%s limit 1",
                           (enc_url,)).fetchone():
                counts["duplicate"] += 1
                continue
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    mp3 = Path(tmp) / "episode.mp3"
                    print(f"downloading: {title}")
                    download(enc_url, mp3)
                    print(f"transcribing ({engine}): {title}")
                    text = transcribe(mp3, engine)
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
