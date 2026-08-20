"""Off-site transcription agent (build spec v7 §6): `python -m graph.asr_agent`.

Runs on a Mac (or any machine with a working whisper engine), leases jobs
from the vault's queue API, downloads the audio — the podcast enclosure
directly, or the server-held file for YouTube — transcribes locally, and
posts the text back. No database access; only the four /api/asr endpoints,
authenticated by the shared token, reached either directly (--server) or
through an SSH tunnel to the server's loopback port (--ssh).

The token is never printed. The agent holds no other credential.
"""
import argparse
import atexit
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .connectors import podcast


class Api:
    """The four queue endpoints. `session` is swappable so the tests can run
    the loop against fastapi's TestClient."""

    def __init__(self, base: str, token: str, session=None):
        self.base = base.rstrip("/")
        self.headers = {"X-CAF-ASR-Token": token}
        self.s = session if session is not None else requests.Session()

    def lease(self, worker: str):
        r = self.s.post(f"{self.base}/api/asr/lease", json={"worker": worker},
                        headers=self.headers, timeout=30)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def audio(self, job_id: str, path: Path,
              max_bytes: int = podcast.MAX_AUDIO_BYTES) -> Path:
        url = f"{self.base}/api/asr/jobs/{job_id}/audio"
        try:
            r = self.s.get(url, headers=self.headers, timeout=600, stream=True)
        except TypeError:   # a test client without a stream kwarg
            r = self.s.get(url, headers=self.headers, timeout=600)
        r.raise_for_status()
        chunks = (r.iter_content(262_144) if hasattr(r, "iter_content")
                  else [r.content])
        written = 0
        with path.open("wb") as fh:
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise RuntimeError(
                        f"the audio is larger than {max_bytes // 1_000_000} MB")
                fh.write(chunk)
        return path

    def complete(self, job_id: str, text: str) -> dict:
        r = self.s.post(f"{self.base}/api/asr/jobs/{job_id}/complete",
                        json={"text": text}, headers=self.headers, timeout=120)
        r.raise_for_status()
        return r.json()

    def fail(self, job_id: str, error: str) -> None:
        # best-effort: a failure report that itself fails must not stop the loop
        try:
            self.s.post(f"{self.base}/api/asr/jobs/{job_id}/fail",
                        json={"error": error}, headers=self.headers, timeout=30)
        except Exception as e:
            print(f"could not report the failure: {e}", file=sys.stderr)


class Tunnel:
    """ssh -N -L <free local port>:127.0.0.1:<remote port> <dest>, restarted
    by ensure() when the process dies (the server rebooting, the Mac waking)."""

    def __init__(self, dest: str, remote_port: int):
        self.dest = dest
        self.remote_port = remote_port
        self.port = None
        self.proc = None

    def start(self) -> int:
        if self.port is None:
            # picked once and reused across restarts: the Api's base URL is
            # built from this port, so a restart must come back on it
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
             "-L", f"{self.port}:127.0.0.1:{self.remote_port}", self.dest])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("the SSH tunnel exited before it was up")
            try:
                with socket.create_connection(("127.0.0.1", self.port), 1):
                    return self.port
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("the SSH tunnel did not come up within 15s")

    def ensure(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            print("restarting the SSH tunnel", file=sys.stderr)
            self.start()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


# completion retry policy: a finished transcript is minutes of work — post it
# stubbornly before giving up, and never report a completion-side failure as
# the job's failure (the lease expiry re-queues it without burning the
# attempt budget on a server-side blip)
COMPLETE_TRIES = 3
COMPLETE_BACKOFF_S = 5.0


def transcribe_job(api: Api, job: dict, engine: str) -> str:
    """One job's audio to a tempdir, transcribed. Raises on any failure."""
    with tempfile.TemporaryDirectory() as tmp:
        if job.get("audio"):
            path = Path(tmp) / "audio.m4a"
            api.audio(job["job_id"], path)
        elif job.get("audio_url"):
            ext = Path(urlsplit(job["audio_url"]).path).suffix or ".mp3"
            path = Path(tmp) / f"audio{ext}"
            podcast.download(job["audio_url"], path)
        else:
            raise RuntimeError("the job carries no audio")
        text = podcast.transcribe(path, engine)
    if not text.strip():
        raise RuntimeError("the transcript came back empty")
    return text


def post_transcript(api: Api, job: dict, text: str):
    """Post a finished transcript, retrying; returns the completion result
    or None when the server would not take it."""
    for attempt in range(1, COMPLETE_TRIES + 1):
        try:
            return api.complete(job["job_id"], text)
        except Exception as e:
            print(f"complete failed ({attempt}/{COMPLETE_TRIES}): {e}",
                  file=sys.stderr)
            if attempt < COMPLETE_TRIES:
                time.sleep(COMPLETE_BACKOFF_S * attempt)
    return None


def run(api: Api, engine: str, worker: str, once: bool = False,
        poll_s: float = 60, tunnel: Tunnel | None = None) -> dict:
    """The loop. --once drains the queue and returns; otherwise polls forever.
    Returns counts (for the tests and the --once exit line)."""
    counts = {"done": 0, "failed": 0}
    while True:
        try:
            job = api.lease(worker)
        except Exception as e:
            print(f"lease failed: {e}", file=sys.stderr)
            if once:
                raise
            if tunnel is not None:
                try:
                    tunnel.ensure()
                except Exception as te:
                    print(f"tunnel: {te}", file=sys.stderr)
            time.sleep(poll_s)
            continue
        if job is None:
            if once:
                return counts
            time.sleep(poll_s)
            continue
        label = job.get("title") or job["job_id"]
        t0 = time.monotonic()
        try:
            text = transcribe_job(api, job, engine)
        except Exception as e:
            counts["failed"] += 1
            print(f"error: {label}: {e}", file=sys.stderr)
            api.fail(job["job_id"], str(e)[:500])
            continue
        out = post_transcript(api, job, text)
        if out is None:
            # completion-side failure: the transcript is lost but the job is
            # NOT failed — the lease expires and the work is redone, rather
            # than a server blip burning the job's attempt budget
            counts["failed"] += 1
            print(f"error: {label}: could not post the transcript; "
                  "leaving the lease to expire", file=sys.stderr)
            continue
        counts["done"] += 1
        print(f"done: {label} ({time.monotonic() - t0:.0f}s, "
              f"event {out['event_id']})")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m graph.asr_agent",
        description="Lease transcription jobs from the vault and run whisper here.")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--server", help="queue API base URL, e.g. http://127.0.0.1:8642")
    where.add_argument("--ssh", help="user@host: tunnel to the server's loopback port")
    p.add_argument("--remote-port", type=int, default=8600,
                   help="the vault port on the server's loopback (default 8600)")
    p.add_argument("--token", default=os.environ.get("CAF_ASR_TOKEN", ""),
                   help="shared token (default: CAF_ASR_TOKEN)")
    p.add_argument("--token-file", default="",
                   help="file holding the shared token (wins over --token; "
                        "keeps the token out of launchd plists and ps output)")
    p.add_argument("--engine", default="",
                   help="whisper engine (default: CAF_ASR / auto)")
    p.add_argument("--name", default=socket.gethostname(), help="worker name")
    p.add_argument("--poll", type=float, default=60,
                   help="seconds between empty-queue polls (default 60)")
    p.add_argument("--once", action="store_true",
                   help="drain the queue, then exit")
    args = p.parse_args(argv)

    engine = args.engine or podcast.asr_engine()
    if engine in ("off", "remote"):
        print(f"CAF_ASR={engine} cannot transcribe here; pass --engine "
              "(mlx or faster-whisper)", file=sys.stderr)
        return 2
    token = args.token
    if args.token_file:
        try:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            print(f"could not read the token file: {e}", file=sys.stderr)
            return 2
    if not token:
        print("no token: set CAF_ASR_TOKEN, or pass --token/--token-file",
              file=sys.stderr)
        return 2

    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    tunnel = None
    base = args.server
    if args.ssh:
        tunnel = Tunnel(args.ssh, args.remote_port)
        atexit.register(tunnel.stop)
        port = tunnel.start()
        base = f"http://127.0.0.1:{port}"

    api = Api(base, token)
    print(f"asr agent: {args.name} ({engine}) -> {base}")
    counts = run(api, engine, args.name, once=args.once, poll_s=args.poll,
                 tunnel=tunnel)
    print(f"asr agent: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
