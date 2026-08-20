"""Remote ASR (build spec v7): the queue, the connectors' remote branches,
the /api/asr endpoints, and the off-site agent's loop.

Nothing here transcribes or reaches the network: whisper is monkeypatched,
feeds and yt-dlp are faked. API tests commit rows through the app's own
connections, so each cleans up after itself (jobs deleted, events marked
'failed' so later files' triage never sees them, sources 'dropped' so later
polls never fetch their fake URLs).

    CAF_DB_URL=postgresql:///caf_test_asr uv run pytest tests/test_asr_remote.py -q
"""
import sys
import types
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from graph import asr_agent, asr_queue, config, db, envelope, webapp
from graph.connectors import podcast, youtube

TOKEN = "asr-test-token-000"
ENCLOSURE = "https://cdn.example.com/shows/oil/{tag}.mp3"

PODCAST_RSS = """<?xml version="1.0"?><rss><channel>
<item>
  <title>Does the market know anything about oil?</title>
  <pubDate>Tue, 19 Aug 2026 04:00:00 GMT</pubDate>
  <enclosure url="{enclosure}" length="10995733" type="audio/mpeg"/>
</item>
</channel></rss>"""

VIDEOS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns="http://www.w3.org/2005/Atom">
<title>Example Channel</title>
<entry>
  <id>yt:video:{vid}</id>
  <yt:videoId>{vid}</yt:videoId>
  <title>Nvidia earnings call recap</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v={vid}"/>
  <author><name>Example Channel</name></author>
  <published>2026-08-10T12:00:00+00:00</published>
</entry>
</feed>
"""


class Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status = status
        self.bytes = text.encode("utf-8")
        self.headers = {}

    @property
    def ok(self):
        return 200 <= self.status < 300

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


def fake_ytdlp(info, audio=b""):
    """A yt_dlp stand-in; with download=True it writes `audio` into the
    outtmpl directory the way the real downloader would."""
    calls = {"downloads": 0}

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            if download:
                calls["downloads"] += 1
                out = (str(self.opts["outtmpl"]["default"])
                       if isinstance(self.opts.get("outtmpl"), dict)
                       else str(self.opts["outtmpl"]))
                path = Path(out.replace("%(id)s", info["id"])
                               .replace("%(ext)s", "m4a"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(audio)
            return dict(info)

    return types.SimpleNamespace(YoutubeDL=FakeYDL), calls


def no_captions_info(vid):
    return {"id": vid, "title": "Nvidia earnings call recap",
            "channel": "Example Channel", "duration": 60,
            "upload_date": "20260810"}


def add_podcast_source(con, feed, tag):
    return con.execute(
        "insert into source (name, connector, url, status) values "
        "(%s, 'podcast', %s, 'active') returning source_id",
        (f"podcast:{feed}", f"https://feeds.example.com/{tag}")).fetchone()["source_id"]


def add_youtube_source(con, name, channel="UCasr"):
    from psycopg.types.json import Jsonb
    return con.execute(
        "insert into source (name, connector, url, status, config) values "
        "(%s, 'youtube', 'https://www.youtube.com/channel/UCasr', 'active', %s) "
        "returning source_id",
        (name, Jsonb({"channel_id": channel}))).fetchone()["source_id"]


def podcast_feed(con, monkeypatch, tag, feed=None):
    """One active podcast source whose feed fetch is faked; returns
    (source_id, enclosure_url)."""
    feed = feed or f"asrfeed-{tag}"
    enc = ENCLOSURE.format(tag=tag)
    source_id = add_podcast_source(con, feed, tag)
    rss = PODCAST_RSS.format(enclosure=enc)
    monkeypatch.setattr(podcast.requests, "get",
                        lambda url, **kw: Resp(rss))
    return source_id, enc, feed


def job_of(con, external_id):
    return con.execute("select * from asr_job where external_id=%s",
                       (external_id,)).fetchone()


@pytest.fixture
def spool(monkeypatch, tmp_path):
    spool = tmp_path / "asr_spool"
    monkeypatch.setattr(config, "ASR_SPOOL", spool)
    return spool


@pytest.fixture
def wcon(database):
    """A committing connection for API tests (the app commits its own writes;
    fixtures that seed or clean up around it must commit too)."""
    c = db.connect()
    yield c
    c.commit()
    c.close()


def cleanup(con, source_ids):
    """Remove everything an API test committed, scoped to its own sources.
    Later files assert over all events of a connector and poll every active
    source, so rows marked terminal are not enough — they must go."""
    for sid in source_ids:
        con.execute("update event set lineage_id=null where source_id=%s", (sid,))
        con.execute("delete from lineage where root_event_id in "
                    "(select event_id from event where source_id=%s)", (sid,))
        con.execute("delete from asr_job where source_id=%s", (sid,))
        con.execute("delete from event where source_id=%s", (sid,))
        con.execute("delete from source where source_id=%s", (sid,))
    con.commit()


# ------------------------------------------------------- podcast connector


def test_podcast_remote_enqueues_instead_of_transcribing(con, monkeypatch):
    monkeypatch.setenv("CAF_ASR", "remote")
    source_id, enc, feed = podcast_feed(con, monkeypatch, "q1")
    monkeypatch.setattr(podcast, "transcribe",
                        lambda *a, **k: pytest.fail("remote must not transcribe"))
    monkeypatch.setattr(podcast, "download",
                        lambda *a, **k: pytest.fail("remote must not download"))

    out = podcast.poll(con, feeds=[feed])
    assert out["queued"] == 1 and out["new"] == 0

    job = job_of(con, enc)
    assert job["status"] == "pending"
    assert job["connector"] == "podcast"
    assert job["audio_url"] == enc and job["audio_path"] is None
    assert job["published_at"] == "2026-08-19"
    assert job["meta"]["doc_prefix"] == (
        "# title: Does the market know anything about oil?\n"
        "# source_type: podcast\n# published: 2026-08-19\n"
        f"# feed: {feed}\n---\n")
    assert job["meta"]["event_meta"]["enclosure_url"] == enc
    assert con.execute("select count(*) n from event where source_id=%s",
                       (source_id,)).fetchone()["n"] == 0


def test_podcast_remote_enqueue_is_idempotent(con, monkeypatch):
    monkeypatch.setenv("CAF_ASR", "remote")
    _, enc, feed = podcast_feed(con, monkeypatch, "q2")
    assert podcast.poll(con, feeds=[feed])["queued"] == 1
    assert podcast.poll(con, feeds=[feed])["queued"] == 0
    assert con.execute("select count(*) n from asr_job where external_id=%s",
                       (enc,)).fetchone()["n"] == 1


def test_podcast_remote_respects_event_dedup(con, monkeypatch):
    monkeypatch.setenv("CAF_ASR", "remote")
    source_id, enc, feed = podcast_feed(con, monkeypatch, "q3")
    envelope.ingest(con, source_id, "podcast", b"already ingested q3",
                    "text/plain", ".txt", meta={"enclosure_url": enc})

    out = podcast.poll(con, feeds=[feed])
    assert out["duplicate"] == 1 and out["queued"] == 0
    assert job_of(con, enc) is None


def test_podcast_off_still_skips_entirely(con, monkeypatch):
    monkeypatch.setenv("CAF_ASR", "off")
    _, enc, feed = podcast_feed(con, monkeypatch, "q4")
    out = podcast.poll(con, feeds=[feed])
    assert out == {"new": 0, "duplicate": 0, "queued": 0, "errors": 0}
    assert job_of(con, enc) is None


# ------------------------------------------------------------------ queue


def enqueue_one(con, source_id, tag, **kw):
    enc = ENCLOSURE.format(tag=tag)
    return enc, asr_queue.enqueue(
        con, source_id, "podcast", enc, title=f"Episode {tag}",
        published_at="2026-08-19",
        doc_prefix=(f"# title: Episode {tag}\n# source_type: podcast\n"
                    f"# published: 2026-08-19\n# feed: unit\n---\n"),
        event_meta={"feed": "unit", "title": f"Episode {tag}",
                    "enclosure_url": enc},
        audio_url=enc, **kw)


def test_lease_complete_roundtrip_writes_the_exact_document(con):
    source_id = add_podcast_source(con, "unit-rt", "rt")
    enc, job_id = enqueue_one(con, source_id, "rt")
    assert job_id is not None

    job = asr_queue.lease(con, "mac-mini")
    assert job["job_id"] == job_id
    assert job["status"] == "leased" and job["leased_by"] == "mac-mini"
    assert job["attempts"] == 1

    event_id, is_new = asr_queue.complete(con, job, "Oil is weird.")
    assert is_new
    row = con.execute("select * from event where event_id=%s",
                      (event_id,)).fetchone()
    assert Path(row["artifact_uri"]).read_text(encoding="utf-8") == (
        "# title: Episode rt\n# source_type: podcast\n"
        "# published: 2026-08-19\n# feed: unit\n---\nOil is weird.\n")
    assert row["meta"]["enclosure_url"] == enc
    assert row["published_at"] is not None

    done = asr_queue.get(con, job_id)
    assert done["status"] == "done" and done["event_id"] == event_id


def test_lease_empty_queue_returns_none(con):
    assert asr_queue.lease(con, "mac-mini") is None


def test_expired_lease_is_leased_again(con):
    source_id = add_podcast_source(con, "unit-exp", "exp")
    _, job_id = enqueue_one(con, source_id, "exp")
    first = asr_queue.lease(con, "one")
    assert first["job_id"] == job_id
    assert asr_queue.lease(con, "two") is None   # still leased

    con.execute("update asr_job set lease_expires=now() - interval '1 minute' "
                "where job_id=%s", (job_id,))
    second = asr_queue.lease(con, "two")
    assert second["job_id"] == job_id
    assert second["leased_by"] == "two" and second["attempts"] == 2


def test_fail_requeues_then_goes_terminal(con, spool):
    source_id = add_podcast_source(con, "unit-fail", "fail")
    _, job_id = enqueue_one(con, source_id, "fail")
    spool.mkdir(parents=True)
    audio = spool / f"{job_id}.m4a"
    audio.write_bytes(b"x")
    con.execute("update asr_job set audio_path=%s where job_id=%s",
                (str(audio), job_id))

    for n in range(1, config.ASR_MAX_ATTEMPTS + 1):
        job = asr_queue.lease(con, "mac-mini")
        assert job["attempts"] == n
        status = asr_queue.fail(con, job, f"boom {n}")
        terminal = n >= config.ASR_MAX_ATTEMPTS
        assert status == ("error" if terminal else "pending")
        if not terminal:
            # the backoff keeps the failed job out of the next lease...
            assert asr_queue.lease(con, "mac-mini") is None
            # ...until its retry time comes (simulated here)
            con.execute("update asr_job set not_before=now() where job_id=%s",
                        (job_id,))
    assert asr_queue.lease(con, "mac-mini") is None
    row = asr_queue.get(con, job_id)
    assert row["status"] == "error" and row["error"] == "boom 3"
    # fail() leaves the file; the API layer removes it after its commit
    assert audio.exists()
    asr_queue.cleanup_spool(row)
    assert not audio.exists()


def test_remote_completion_is_byte_identical_to_inline(con, monkeypatch, tmp_path):
    """The proof of §2: a remote transcript ingests as a content-hash
    duplicate of the same episode transcribed inline."""
    source_id = add_podcast_source(con, "unit-bytes", "bytes")
    text = "Oil prices blasted higher in March."
    feed = "unit-bytes"
    title, date = "Episode bytes", "2026-08-19"
    enc = ENCLOSURE.format(tag="bytes")

    # the inline path's document, exactly as podcast.poll writes it
    inline_doc = (f"# title: {title}\n# source_type: podcast\n"
                  f"# published: {date}\n# feed: {feed}\n---\n{text}\n")
    envelope.ingest(con, source_id, "podcast", inline_doc.encode("utf-8"),
                    "text/plain", ".txt", published_at=date,
                    meta={"feed": feed, "title": title, "enclosure_url": enc})

    job_id = asr_queue.enqueue(
        con, source_id, "podcast", enc + "?remote", title=title,
        published_at=date,
        doc_prefix=(f"# title: {title}\n# source_type: podcast\n"
                    f"# published: {date}\n# feed: {feed}\n---\n"),
        event_meta={"feed": feed, "title": title, "enclosure_url": enc},
        audio_url=enc)
    job = asr_queue.lease(con, "mac-mini")
    assert job["job_id"] == job_id
    _, is_new = asr_queue.complete(con, job, text)
    assert is_new is False   # identical bytes -> envelope dedup, same lineage


# ----------------------------------------------------------------- youtube


def test_youtube_remote_parks_audio_and_enqueues(con, monkeypatch, spool):
    vid = "VIDASR00001"
    monkeypatch.setenv("CAF_ASR", "remote")
    source_id = add_youtube_source(con, "YouTube: ASR Channel")
    fake, calls = fake_ytdlp(no_captions_info(vid), audio=b"fakeaudio")
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    from graph import fetch
    monkeypatch.setattr(fetch, "get",
                        lambda url, **kw: Resp(VIDEOS_XML.format(vid=vid)))

    out = youtube.poll(con)
    assert out["queued"] == 1 and out["new"] == 0 and out["skipped"] == 0

    job = job_of(con, vid)
    assert job["connector"] == "youtube" and job["status"] == "pending"
    assert job["audio_url"] is None
    audio = Path(job["audio_path"])
    assert audio.parent == spool
    assert audio.name == f"{vid}.m4a"   # named by video id: no rename window
    assert audio.read_bytes() == b"fakeaudio"
    assert job["meta"]["event_meta"]["transcript_source"] == "whisper"
    assert job["meta"]["doc_prefix"].startswith(
        "# title: Nvidia earnings call recap\n# source_type: video\n")
    assert job["meta"]["doc_prefix"].endswith("---\n")

    # the queued video is not on the skip list and is not re-downloaded
    src = con.execute("select config from source where source_id=%s",
                      (source_id,)).fetchone()
    assert vid not in ((src["config"] or {}).get("skipped") or [])
    out2 = youtube.poll(con)
    assert out2["queued"] == 0 and calls["downloads"] == 1

    # completion writes the same document the inline path would
    lease = asr_queue.lease(con, "mac-mini")
    event_id, is_new = asr_queue.complete(con, lease, "Guidance went up.")
    assert is_new
    row = con.execute("select * from event where event_id=%s",
                      (event_id,)).fetchone()
    body = Path(row["artifact_uri"]).read_text(encoding="utf-8")
    assert body == ("# title: Nvidia earnings call recap\n"
                    "# source_type: video\n# published: 2026-08-10\n"
                    "# channel: Example Channel\n# duration: 60s\n---\n"
                    "Guidance went up.\n")
    assert row["meta"]["video_id"] == vid
    assert row["meta"]["feed"] == "YouTube: ASR Channel"
    # the library leaves the file for the API layer to remove after commit
    assert audio.exists()
    asr_queue.cleanup_spool(lease)
    assert not audio.exists()


def test_youtube_captions_ignore_remote_mode(con, monkeypatch, spool):
    vid = "VIDASR00002"
    monkeypatch.setenv("CAF_ASR", "remote")
    add_youtube_source(con, "YouTube: Captioned Channel", channel="UCasr2")
    info = {**no_captions_info(vid),
            "automatic_captions": {"en": [
                {"ext": "json3", "url": "https://captions.example.com/json3"}]}}
    fake, calls = fake_ytdlp(info)
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    import json as _json
    payload = _json.dumps({"events": [{"segs": [{"utf8": "From captions."}]}]})
    from graph import fetch

    def fake_get(url, **kw):
        if "captions" in url:
            return Resp(payload)
        return Resp(VIDEOS_XML.format(vid=vid))

    monkeypatch.setattr(fetch, "get", fake_get)
    out = youtube.poll(con)
    assert out["new"] == 1 and out["queued"] == 0
    assert job_of(con, vid) is None and calls["downloads"] == 0


# ---------------------------------------------------------------- the API


def test_asr_api_refuses_without_the_token(monkeypatch, database):
    client = TestClient(webapp.app)
    monkeypatch.delenv("CAF_ASR_TOKEN", raising=False)
    r = client.post("/api/asr/lease", json={"worker": "x"},
                    headers={"X-CAF-ASR-Token": "anything"})
    assert r.status_code == 403   # unset env = off, not open

    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    assert client.post("/api/asr/lease", json={"worker": "x"}).status_code == 403
    assert client.post("/api/asr/lease", json={"worker": "x"},
                       headers={"X-CAF-ASR-Token": "wrong"}).status_code == 403
    ok = client.post("/api/asr/lease", json={"worker": "x"},
                     headers={"X-CAF-ASR-Token": TOKEN})
    assert ok.status_code == 204   # authorized, queue empty


def test_asr_api_full_flow(monkeypatch, wcon, tmp_path):
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / "artifacts")
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(config, "ASR_SPOOL", spool)
    client = TestClient(webapp.app)
    headers = {"X-CAF-ASR-Token": TOKEN}
    source_id = add_youtube_source(wcon, "YouTube: API Flow", channel="UCapi")
    job_id = asr_queue.enqueue(
        wcon, source_id, "youtube", "VIDAPI00001", title="API flow video",
        published_at="2026-08-10",
        doc_prefix=("# title: API flow video\n# source_type: video\n"
                    "# published: 2026-08-10\n# channel: C\n"
                    "# duration: 60s\n---\n"),
        event_meta={"video_id": "VIDAPI00001", "title": "API flow video",
                    "transcript_source": "whisper", "feed": "YouTube: API Flow"})
    audio = spool / f"{job_id}.m4a"
    audio.write_bytes(b"apiflowaudio")
    wcon.execute("update asr_job set audio_path=%s where job_id=%s",
                 (str(audio), job_id))
    wcon.commit()
    try:
        # audio before lease: not leased yet -> 404
        assert client.get(f"/api/asr/jobs/{job_id}/audio",
                          headers=headers).status_code == 404

        job = client.post("/api/asr/lease", json={"worker": "mac-mini"},
                          headers=headers).json()
        assert job["job_id"] == str(job_id)
        assert job["connector"] == "youtube"
        assert job["audio"] is True and job["audio_url"] is None

        got = client.get(f"/api/asr/jobs/{job_id}/audio", headers=headers)
        assert got.status_code == 200 and got.content == b"apiflowaudio"

        done = client.post(f"/api/asr/jobs/{job_id}/complete",
                           json={"text": "The API flow works."}, headers=headers)
        assert done.status_code == 200 and done.json()["is_new"] is True

        # a second completion answers 409; blank text answers 400
        again = client.post(f"/api/asr/jobs/{job_id}/complete",
                            json={"text": "x"}, headers=headers)
        assert again.status_code == 409
        assert client.post(f"/api/asr/jobs/{job_id}/fail",
                           json={"error": "late"}, headers=headers).status_code == 409

        event_id = uuid.UUID(done.json()["event_id"])
        row = wcon.execute("select * from event where event_id=%s",
                           (event_id,)).fetchone()
        assert row is not None and row["connector"] == "youtube"
        assert not audio.exists()
    finally:
        cleanup(wcon, [source_id])


def test_asr_api_blank_transcript_and_fail_flow(monkeypatch, wcon):
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    client = TestClient(webapp.app)
    headers = {"X-CAF-ASR-Token": TOKEN}
    source_id = add_podcast_source(wcon, "api-fail", "apifail")
    enc, job_id = enqueue_one(wcon, source_id, "apifail")
    wcon.commit()
    try:
        job = client.post("/api/asr/lease", json={"worker": "mac-mini"},
                          headers=headers).json()
        assert job["job_id"] == str(job_id) and job["audio_url"] == enc
        assert client.post(f"/api/asr/jobs/{job_id}/complete",
                           json={"text": "   "}, headers=headers).status_code == 400

        failed = client.post(f"/api/asr/jobs/{job_id}/fail",
                             json={"error": "download refused"}, headers=headers)
        assert failed.status_code == 200
        assert failed.json() == {"status": "pending", "attempts": 1}
        # the retry backoff hides the job until its time comes
        assert client.post("/api/asr/lease", json={"worker": "mac-mini"},
                           headers=headers).status_code == 204
        wcon.execute("update asr_job set not_before=now() where job_id=%s",
                     (job_id,))
        wcon.commit()
        assert client.post("/api/asr/lease", json={"worker": "mac-mini"},
                           headers=headers).json()["attempts"] == 2
    finally:
        cleanup(wcon, [source_id])


# --------------------------------------------------------------- the agent


def agent_api(client):
    return asr_agent.Api("", TOKEN, session=client)


def test_agent_drains_a_podcast_job(monkeypatch, wcon, tmp_path):
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path / "artifacts")
    source_id = add_podcast_source(wcon, "agent-run", "agentrun")
    _, job_id = enqueue_one(wcon, source_id, "agentrun")
    wcon.commit()
    monkeypatch.setattr(podcast, "download",
                        lambda url, path, **kw: path.write_bytes(b"mp3") or path)
    monkeypatch.setattr(podcast, "transcribe",
                        lambda path, engine=None: "Fed cuts rates.")
    try:
        counts = asr_agent.run(agent_api(TestClient(webapp.app)), "mlx",
                               "test-worker", once=True)
        assert counts == {"done": 1, "failed": 0}
        job = wcon.execute("select * from asr_job where job_id=%s",
                           (job_id,)).fetchone()
        assert job["status"] == "done" and job["leased_by"] == "test-worker"
        row = wcon.execute("select * from event where event_id=%s",
                           (job["event_id"],)).fetchone()
        assert Path(row["artifact_uri"]).read_text(encoding="utf-8").endswith(
            "---\nFed cuts rates.\n")
        # a drained queue ends --once immediately
        assert asr_agent.run(agent_api(TestClient(webapp.app)), "mlx",
                             "test-worker", once=True) == {"done": 0, "failed": 0}
    finally:
        cleanup(wcon, [source_id])


def test_agent_reports_a_failure_and_carries_on(monkeypatch, wcon):
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    source_id = add_podcast_source(wcon, "agent-fail", "agentfail")
    _, job_id = enqueue_one(wcon, source_id, "agentfail")
    wcon.commit()

    def broken_download(url, path, **kw):
        raise RuntimeError("the episode is larger than 350 MB")

    monkeypatch.setattr(podcast, "download", broken_download)
    try:
        counts = asr_agent.run(agent_api(TestClient(webapp.app)), "mlx",
                               "test-worker", once=True)
        # the retry backoff means one drain pass fails the job exactly once
        assert counts == {"done": 0, "failed": 1}
        job = wcon.execute("select * from asr_job where job_id=%s",
                           (job_id,)).fetchone()
        assert job["status"] == "pending" and job["attempts"] == 1
        assert "350 MB" in job["error"]
        assert job["not_before"] is not None
    finally:
        cleanup(wcon, [source_id])


# ----------------------------------------------------- review-pass fixes


def test_token_gate_answers_403_for_non_ascii_header(monkeypatch):
    """secrets.compare_digest refuses non-ASCII str; Starlette decodes
    headers as latin-1, so a stray high byte must be a 403, not a 500."""
    from fastapi import HTTPException
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    for bad in ("\xe9", "\xff\xfe", "wrong"):
        with pytest.raises(HTTPException) as e:
            webapp.require_asr_token(bad)
        assert e.value.status_code == 403
    assert webapp.require_asr_token(TOKEN) is None


def test_spool_path_refuses_files_outside_the_spool(spool, tmp_path):
    spool.mkdir(parents=True)
    inside = spool / "ok.m4a"
    inside.write_bytes(b"x")
    outside = tmp_path / "outside.m4a"
    outside.write_bytes(b"y")

    def job(p):
        return {"audio_path": str(p)}

    assert asr_queue.spool_path(job(inside)) == inside.resolve()
    assert asr_queue.spool_path(job(outside)) is None
    assert asr_queue.spool_path(job("/etc/hosts")) is None
    assert asr_queue.spool_path(job(spool / ".." / "outside.m4a")) is None
    assert asr_queue.spool_path({"audio_path": None}) is None
    # a symlink inside the spool pointing out resolves out -> refused
    link = spool / "sneaky.m4a"
    link.symlink_to(outside)
    assert asr_queue.spool_path(job(link)) is None


def test_asr_api_audio_refuses_a_job_pointing_outside_the_spool(
        monkeypatch, wcon, tmp_path):
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(config, "ASR_SPOOL", spool)
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"not audio")
    client = TestClient(webapp.app)
    headers = {"X-CAF-ASR-Token": TOKEN}
    source_id = add_youtube_source(wcon, "YouTube: Containment", channel="UCcont")
    job_id = asr_queue.enqueue(
        wcon, source_id, "youtube", "VIDCONT0001", title="Containment",
        published_at=None, doc_prefix="# title: Containment\n---\n",
        event_meta={"video_id": "VIDCONT0001"})
    wcon.execute("update asr_job set audio_path=%s where job_id=%s",
                 (str(outside), job_id))
    wcon.commit()
    try:
        job = client.post("/api/asr/lease", json={"worker": "w"},
                          headers=headers).json()
        assert job["job_id"] == str(job_id)
        got = client.get(f"/api/asr/jobs/{job_id}/audio", headers=headers)
        assert got.status_code == 404
        assert b"not audio" not in got.content
    finally:
        cleanup(wcon, [source_id])


def add_link(con, vid):
    return con.execute(
        "insert into link_queue (url, kind, site) values "
        "(%s, 'youtube_video', 'youtube') returning link_id",
        (f"https://www.youtube.com/watch?v={vid}",)).fetchone()["link_id"]


def test_pasted_link_waits_queued_and_resolves_on_completion(
        con, monkeypatch, spool):
    """The worker drains the link queue with CAF_ASR=remote: a no-captions
    video must wait as 'queued' without burning attempts or re-downloading,
    and completion must resolve the link row."""
    from graph.connectors import links
    vid = "VIDLNK00001"
    monkeypatch.setenv("CAF_ASR", "remote")
    fake, calls = fake_ytdlp(no_captions_info(vid), audio=b"fakeaudio")
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    link_id = add_link(con, vid)

    row = links.process_one(con, link_id)
    assert row["status"] == "queued" and row["attempts"] == 0
    assert row["error"] is None
    job = job_of(con, vid)
    assert job is not None and job["meta"]["link_id"] == str(link_id)
    assert calls["downloads"] == 1

    # the waiting cycles are one select each, never another download
    row = links.process_one(con, link_id)
    assert row["status"] == "queued" and row["attempts"] == 0
    assert calls["downloads"] == 1

    lease = asr_queue.lease(con, "mac-mini")
    event_id, _ = asr_queue.complete(con, lease, "From the agent.")
    resolved = con.execute("select * from link_queue where link_id=%s",
                           (link_id,)).fetchone()
    assert resolved["status"] == "done"
    assert resolved["event_id"] == event_id


def test_pasted_link_of_a_terminally_failed_job_reports_it(
        con, monkeypatch, spool):
    from graph.connectors import links
    vid = "VIDLNK00002"
    monkeypatch.setenv("CAF_ASR", "remote")
    fake, calls = fake_ytdlp(no_captions_info(vid), audio=b"fakeaudio")
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    link_id = add_link(con, vid)
    links.process_one(con, link_id)
    con.execute("update asr_job set status='error', error='boom' "
                "where external_id=%s", (vid,))

    row = links.process_one(con, link_id)
    assert row["status"] == "failed"
    assert row["error"] == "Transcription failed."
    assert calls["downloads"] == 1   # the terminal job is never re-downloaded


def test_local_engine_skips_an_agent_owned_episode(con, monkeypatch):
    """Switching CAF_ASR from remote to a local engine while a job is
    pending must not transcribe the episode a second time."""
    monkeypatch.setenv("CAF_ASR", "faster-whisper")
    source_id, enc, feed = podcast_feed(con, monkeypatch, "switch")
    enqueue_one(con, source_id, "switch")
    monkeypatch.setattr(podcast, "download",
                        lambda *a, **k: pytest.fail("agent owns this episode"))
    monkeypatch.setattr(podcast, "transcribe",
                        lambda *a, **k: pytest.fail("agent owns this episode"))

    out = podcast.poll(con, feeds=[feed])
    assert out["new"] == 0 and out["queued"] == 0
    assert job_of(con, enc)["status"] == "pending"


def test_tunnel_restart_reuses_the_same_port(monkeypatch):
    """The Api's base URL is built from the tunnel's port once; a restarted
    tunnel must come back on that port or the agent wedges."""
    procs = []

    class FakeProc:
        def __init__(self, argv):
            self.argv = argv
            self.dead = False

        def poll(self):
            return 1 if self.dead else None

        def terminate(self):
            self.dead = True

    def fake_popen(argv, **kw):
        p = FakeProc(argv)
        procs.append(p)
        return p

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(asr_agent.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(asr_agent.socket, "create_connection",
                        lambda *a, **k: FakeSock())
    t = asr_agent.Tunnel("user@host", 8600)
    first = t.start()
    procs[0].dead = True
    t.ensure()
    assert t.port == first
    forwards = [p.argv[p.argv.index("-L") + 1] for p in procs]
    assert forwards == [f"{first}:127.0.0.1:8600"] * 2


def test_agent_keeps_the_lease_when_completion_fails(monkeypatch, wcon):
    """A completion-side failure must not burn the job's attempt budget:
    no fail is posted and the lease is left to expire."""
    monkeypatch.setenv("CAF_ASR_TOKEN", TOKEN)
    source_id = add_podcast_source(wcon, "agent-cfail", "agentcfail")
    _, job_id = enqueue_one(wcon, source_id, "agentcfail")
    wcon.commit()
    monkeypatch.setattr(podcast, "download",
                        lambda url, path, **kw: path.write_bytes(b"mp3") or path)
    monkeypatch.setattr(podcast, "transcribe",
                        lambda path, engine=None: "A transcript.")
    monkeypatch.setattr(asr_agent, "COMPLETE_BACKOFF_S", 0)
    monkeypatch.setattr(asr_agent.Api, "complete",
                        lambda self, job_id, text: (_ for _ in ()).throw(
                            RuntimeError("server blip")))
    try:
        counts = asr_agent.run(agent_api(TestClient(webapp.app)), "mlx",
                               "test-worker", once=True)
        assert counts == {"done": 0, "failed": 1}
        job = wcon.execute("select * from asr_job where job_id=%s",
                           (job_id,)).fetchone()
        assert job["status"] == "leased"   # no fail posted; lease will expire
        assert job["attempts"] == 1 and job["error"] is None
    finally:
        cleanup(wcon, [source_id])
