"""recording.v1 MASTER codec (Python) — the PURE master-build core.

Bytes-in -> bytes-out, deterministic, no IO / DB / subprocess. This is the cloud
meeting-api's recording-assembly core: it turns the recording.v1 chunks a meeting
emitted into one master media file when the meeting ends.

PARALLEL PATH (keep in sync). This is the Python TWIN of the Node module
``meetings/modules/recording/src/recording-codec.ts`` (``buildRecordingMaster``).
Same scope, two languages, names aligned and pinned to the SHARED golden vectors
in ``meetings/modules/recording/src/contracts/golden/``::

    _build_recording_master  <->  buildRecordingMaster   (format dispatch)
    _build_webm_master       <->  buildWebmMaster        (WebM byte-concat)
    _build_wav_master        <->  buildWavMaster         (WAV RIFF header-merge)
    _parse_wav_header        <->  parseWavHeader

The deliberate two-language duplication exists because the all-Node desktop has no
Python meeting-api, while prod assembles via this twin. The goldens drift-lock the
two cores: ``test_recording_golden.py`` (here) and ``golden.test.ts`` (there) both
read the SAME vectors and must reproduce them byte-for-byte. Change a builder here
-> mirror it there, and vice-versa.

Two strategies, dispatched on the wire's recording.v1 ``format``:

* **webm** — BYTE-CONCAT in seq order. The MediaRecorder stream emits a
  self-describing chunk 0 (EBML + Segment + first Cluster) then Cluster-only
  chunks; stacking the Clusters inside the Segment yields a valid container. The
  empty final chunk concatenates as a no-op. (ffmpeg's concat demuxer would drop
  the Cluster-only inputs, so this is NOT ffmpeg.)
* **wav** — RIFF-aware merge: walk each chunk's RIFF chunk list for its ``fmt ``
  and ``data`` chunks (a standard WAV carries others between them — ffmpeg writes
  ``LIST``/``INFO`` by default), sum the ``data`` payloads, prepend one corrected
  canonical master header (``fmt`` copied from chunk 0).
"""
from __future__ import annotations

import io
import struct
from typing import List, Sequence, Tuple

# WAV / RIFF container magic: "RIFF" then size (4) then "WAVE".
_WAV_MAGIC = b"RIFF"
_WAV_FORMAT = b"WAVE"

# A canonical PCM WAV header is exactly 44 bytes:
# RIFF<sz4>WAVE fmt <16><fmt-chunk-16-bytes> data<datasz4>
_WAV_HEADER_BYTES = 44


class InvalidRecordingChunk(ValueError):
    """Uploaded bytes the codec cannot parse as the declared media format.

    A caller/data error, not a server fault: the HTTP edge maps it to 422. ``ValueError``
    subclass so anything already catching the codec's raises keeps working.
    """


def _parse_wav_header(buf: bytes) -> Tuple[bytes, bytes]:
    """Return ``(fmt_chunk_bytes, pcm_payload)`` for a WAV chunk.

    Walks the RIFF chunk list from offset 12 (id(4) + size(4) + body + a pad byte when the
    size is odd) to find ``fmt `` and ``data``. A standard WAV carries chunks between them —
    ``ffmpeg`` writes ``LIST``/``INFO`` by default — so ``data`` is located, never assumed at
    offset 36.

    ``fmt_chunk_bytes`` is the 16-byte fmt body (PCM format, channels, sample-rate, byte-rate,
    block-align, bits-per-sample) copied verbatim into the master so it inherits the source PCM
    format; a longer body (WAVE_FORMAT_EXTENSIBLE) is truncated to its 16-byte PCM prefix. The
    payload is the data body by its declared size, falling back to the rest of the buffer when the
    size is 0 or overruns — a streaming writer stamps a placeholder size and appends. Raises
    ``InvalidRecordingChunk`` for bytes that are not a parsable RIFF/WAVE with both chunks.
    """
    if len(buf) < _WAV_HEADER_BYTES:
        raise InvalidRecordingChunk(f"WAV chunk shorter than the 44-byte header: {len(buf)} bytes")
    if buf[:4] != _WAV_MAGIC or buf[8:12] != _WAV_FORMAT:
        raise InvalidRecordingChunk(f"WAV chunk missing RIFF/WAVE magic: head={buf[:12]!r}")

    fmt_body: bytes | None = None
    pos = 12
    while pos + 8 <= len(buf):
        chunk_id = buf[pos:pos + 4]
        size = struct.unpack("<I", buf[pos + 4:pos + 8])[0]
        body_at = pos + 8
        if chunk_id == b"fmt ":
            if body_at + 16 > len(buf):
                raise InvalidRecordingChunk("WAV 'fmt ' chunk runs past the end of the buffer")
            fmt_body = buf[body_at:body_at + 16]
        elif chunk_id == b"data":
            if fmt_body is None:
                raise InvalidRecordingChunk("WAV chunk has 'data' before any 'fmt ' chunk")
            end = body_at + size if 0 < size <= len(buf) - body_at else len(buf)
            return fmt_body, buf[body_at:end]
        if size > len(buf) - body_at:
            break  # a size running past the buffer — nothing further is parsable
        pos = body_at + size + (size & 1)  # RIFF pads odd-sized bodies to even
    raise InvalidRecordingChunk(
        "WAV chunk has no parsable 'data' chunk"
        + ("" if fmt_body is not None else " and no 'fmt ' chunk")
    )


def validate_wav_chunk(buf: bytes) -> None:
    """Raise ``InvalidRecordingChunk`` unless ``buf`` is a WAV chunk this codec can assemble.

    The upload edge's guard, so unparsable bytes never reach ``meeting.data``. Mirrors the
    builder's own skip rule: a chunk shorter than the 44-byte header is the bot's empty final
    chunk, which the builder drops rather than parses.
    """
    if len(buf) >= _WAV_HEADER_BYTES:
        _parse_wav_header(buf)


def _build_wav_master(chunks: Sequence[bytes]) -> bytes:
    """RIFF-aware merge (mirrors ``buildWavMaster``).

    Skips the empty final chunk, takes each remaining chunk's ``data`` payload, sums
    them, and prepends one corrected canonical master header::

        RIFF<36+total_data>WAVE fmt <16><fmt-chunk><data><total_data><payload...>

    The ``fmt`` body is copied verbatim from the FIRST non-empty chunk; every chunk
    must declare the same ``fmt`` (mismatch -> raise).
    """
    real = [c for c in chunks if len(c) >= _WAV_HEADER_BYTES]  # skip the empty final chunk
    if not real:
        raise InvalidRecordingChunk("_build_wav_master requires at least one non-empty chunk")

    fmt_chunk, _ = _parse_wav_header(real[0])
    payloads: List[bytes] = []
    for i, c in enumerate(real):
        c_fmt, payload = _parse_wav_header(c)
        if c_fmt != fmt_chunk:
            raise InvalidRecordingChunk(f"WAV fmt chunk mismatch at chunk index {i}")
        payloads.append(payload)

    total_data = sum(len(p) for p in payloads)
    out = io.BytesIO()
    out.write(_WAV_MAGIC)                          # 0..3   "RIFF"
    out.write(struct.pack("<I", 36 + total_data))  # 4..7   RIFF size = header(36) + data
    out.write(_WAV_FORMAT)                         # 8..11  "WAVE"
    out.write(b"fmt ")                             # 12..15 "fmt "
    out.write(struct.pack("<I", 16))               # 16..19 fmt chunk size = 16
    out.write(fmt_chunk)                           # 20..35 16-byte fmt body
    out.write(b"data")                             # 36..39 "data"
    out.write(struct.pack("<I", total_data))       # 40..43 data chunk size
    for p in payloads:
        out.write(p)
    return out.getvalue()


def _build_webm_master(chunks: Sequence[bytes]) -> bytes:
    """Byte-concat WebM chunks in seq order (mirrors ``buildWebmMaster``).

    The empty final chunk concatenates as a no-op.
    """
    if not chunks:
        raise InvalidRecordingChunk("_build_webm_master requires at least one chunk")
    return b"".join(chunks)


def _build_recording_master(media_format: str, chunks: Sequence[bytes]) -> bytes:
    """Dispatch to the master builder by ``format`` — the twin of
    ``buildRecordingMaster``. ``wav`` -> RIFF header-merge; anything else
    (i.e. ``webm``) -> byte-concat. Pure: ALREADY-ordered chunks -> master bytes.
    """
    if (media_format or "").lower() == "wav":
        return _build_wav_master(chunks)
    return _build_webm_master(chunks)


def build_recording_master(chunks: Sequence[bytes], media_format: str) -> bytes:
    """Public front door: assemble ALREADY-ordered recording.v1 ``chunks`` into a
    single master media buffer for ``media_format`` (``"webm"`` | ``"wav"``).

    The host writes the result to ``master.<format>``. WebM output is a plain
    byte-concat (playable; no top-level duration metadata — meeting-api optionally
    injects it via ffmpeg downstream). WAV output is a RIFF header-merge with the
    data size corrected to the summed PCM length.
    """
    return _build_recording_master(media_format, chunks)
