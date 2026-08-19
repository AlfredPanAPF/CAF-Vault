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


def read_bounded(artifact_uri: str, cap_bytes: int):
    """The web's reader (build spec v5 §5): the path must be a regular file
    under config.ARTIFACTS, and at most cap_bytes are held in memory, so a
    page view can neither read an arbitrary path from a stale artifact_uri
    nor load a huge upload into the web process. Returns (text, chars,
    partial): the decoded head, the character count of the WHOLE file less
    its trailing newlines (counted in chunks past the head, never held; the
    body the page shows is stripped the same way), and whether the head is
    all of it. Raises FileNotFoundError for anything else; callers 404."""
    import codecs
    from pathlib import Path
    path = Path(artifact_uri).resolve()
    root = config.ARTIFACTS.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise FileNotFoundError(artifact_uri)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as f:
        head = decoder.decode(f.read(cap_bytes))
        chars, trailing, partial = 0, 0, False
        for piece in _pieces(head, f, decoder):
            if piece is None:
                partial = True
                continue
            chars += len(piece)
            kept = piece.rstrip("\n")
            trailing = (trailing + len(piece) if not kept
                        else len(piece) - len(kept))
    return head, chars - trailing, partial


def _pieces(head, f, decoder):
    """The decoded head, then the rest of the file in 1 MB chunks (each
    preceded by a None marker so the caller knows the head was not all),
    then the decoder's final flush."""
    yield head
    while True:
        chunk = f.read(1 << 20)
        if not chunk:
            break
        yield None
        yield decoder.decode(chunk)
    yield decoder.decode(b"", final=True)
