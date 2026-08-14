"""Raw artifact store: local filesystem, per-source directories, content-addressed
(design §4.9 — swaps for object storage without changing callers)."""
import hashlib

from . import config


def put(source_slug: str, content: bytes, ext: str = ".bin") -> tuple[str, bytes]:
    """Store bytes; return (artifact_uri, sha256 digest)."""
    digest = hashlib.sha256(content).digest()
    hexd = digest.hex()
    path = config.ARTIFACTS / source_slug / hexd[:2] / f"{hexd}{ext}"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return str(path), digest


def get(artifact_uri: str) -> bytes:
    from pathlib import Path
    return Path(artifact_uri).read_bytes()
