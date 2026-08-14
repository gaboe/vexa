"""recordings — WAV chunks that are ordinary files, and WAV chunks that are garbage.

``ffmpeg -i in.wav -ac 1 -ar 16000 out.wav`` writes a ``LIST``/``INFO`` chunk between ``fmt `` and
``data``, so ``data`` does NOT start at offset 36. That file is standard, not corrupt: the codec
walks the RIFF chunk list and assembles it, and the assembled PCM carries no ``LIST`` bytes.

Bytes the codec genuinely cannot parse are a caller/data error: rejected at UPLOAD with 422 (the
status the recordings routes already use for unprocessable input) so they never enter
``meeting.data``, and — for anything already stored — surfaced as 422 on the read path too, never a
500.
"""
from __future__ import annotations

import struct

import pytest
from fastapi.testclient import TestClient

from meeting_api.recordings import build_router, finalize_master, upload_chunk
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage
SECRET = "test-admin-token"
USER = 7
MEETING_ID = 1
SESSION_UID = "conn-abc"
_PCM_LEN = 8

_FMT = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, 16000, 32000, 2, 16)


def _ffmpeg_wav(byte_val: int, n_data: int = _PCM_LEN) -> bytes:
    """An ffmpeg-shaped WAV: RIFF/WAVE, ``fmt ``, a LIST/INFO chunk with an ODD-length body (so the
    RIFF pad byte is exercised), then ``data``. PCM is the counting pattern (#509) — byte ``val``
    repeated — so a LIST byte leaking into the master is a pattern mismatch, not just a crash."""
    info = b"INFOISFT" + struct.pack("<I", 13) + b"Lavf60.16.100"  # 13 = odd → 1 pad byte
    lst = struct.pack("<4sI", b"LIST", len(info)) + info + b"\x00"
    data = bytes([byte_val % 256]) * n_data
    dchunk = struct.pack("<4sI", b"data", len(data)) + data
    body = _FMT + lst + dchunk
    return struct.pack("<4sI4s", b"RIFF", 4 + len(body), b"WAVE") + body


def _seeded():
    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=MEETING_ID, user_id=USER, session_uid=SESSION_UID)
    return repo, InMemoryStorage()


def _client_for(repo, storage):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(build_router(repo, storage, token_secret=SECRET))
    return TestClient(app)


def _upload(client, data: bytes, seq: int, is_final: bool = False):
    return client.post(
        "/internal/recordings/upload",
        headers={"Authorization": "Bearer internal-secret"},
        data={"session_uid": SESSION_UID, "media_format": "wav", "media_type": "audio",
              "chunk_seq": seq, "is_final": "true" if is_final else "false"},
        files={"file": ("c.wav", data, "audio/wav")},
    )


@pytest.fixture()
def _internal_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "internal-secret")


# ── an ffmpeg WAV is an ordinary file: accepted end to end ───────────────────────────────────────


async def test_ffmpeg_wav_with_list_chunk_assembles_without_leaking_list_bytes():
    repo, storage = _seeded()
    rid = None
    for seq in range(3):
        receipt = await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=_ffmpeg_wav(seq), media_format="wav", chunk_seq=seq, is_final=False,
        )
        rid = receipt["recording_id"]
    master_key = await finalize_master(repo, storage, meeting_id=MEETING_ID, recording_id=rid)
    master = storage.blobs[master_key]
    # The master is canonical (data at 36) whatever the inputs looked like…
    assert master[:4] == b"RIFF" and master[8:12] == b"WAVE" and master[36:40] == b"data"
    # …and its PCM is EXACTLY the three counting payloads, no LIST/INFO bytes.
    expected = b"".join(bytes([k]) * _PCM_LEN for k in range(3))
    assert master[44:] == expected
    assert struct.unpack("<I", master[40:44])[0] == len(expected)


def test_ffmpeg_wav_uploads_and_plays_over_http(_internal_secret):
    repo, storage = _seeded()
    client = _client_for(repo, storage)
    assert _upload(client, _ffmpeg_wav(0), 0).status_code == 200
    assert _upload(client, _ffmpeg_wav(1), 1, is_final=True).status_code == 200
    recs = client.get("/recordings", headers={"X-User-Id": str(USER)}).json()["recordings"]
    rid = recs[0]["id"]
    r = client.get(f"/recordings/{rid}/master?type=audio", headers={"X-User-Id": str(USER)})
    assert r.status_code == 200, r.text
    raw = client.get(r.json()["raw_url"], headers={"X-User-Id": str(USER)})
    assert raw.status_code == 200, raw.text
    assert raw.content[44:] == bytes([0]) * _PCM_LEN + bytes([1]) * _PCM_LEN


# ── genuinely unparsable bytes: typed 422, never a 500 ───────────────────────────────────────────


def test_unparsable_wav_is_rejected_at_upload_with_422(_internal_secret):
    repo, storage = _seeded()
    client = _client_for(repo, storage)
    r = _upload(client, b"NOTARIFF" + b"\x00" * 60, 0, is_final=True)
    assert r.status_code == 422, r.text
    assert "wav" in r.text.lower()
    # The guard follows the codec's dispatch rule (case-folded), so a cased format can't slip past.
    r = client.post(
        "/internal/recordings/upload",
        headers={"Authorization": "Bearer internal-secret"},
        data={"session_uid": SESSION_UID, "media_format": "WAV", "media_type": "audio",
              "chunk_seq": 0, "is_final": "true"},
        files={"file": ("c.wav", b"NOTARIFF" + b"\x00" * 60, "audio/wav")},
    )
    assert r.status_code == 422, r.text
    # Nothing entered meeting.data, and nothing was stored.
    assert client.get("/recordings", headers={"X-User-Id": str(USER)}).json()["recordings"] == []
    assert storage.blobs == {}


async def test_unparsable_wav_raises_the_typed_error_not_a_bare_valueerror():
    from meeting_api.recordings.service import InvalidRecordingChunk

    repo, storage = _seeded()
    with pytest.raises(InvalidRecordingChunk):
        await upload_chunk(
            repo, storage, token_meeting_id=MEETING_ID, session_uid=SESSION_UID,
            data=b"RIFF" + struct.pack("<I", 4) + b"WAVE" + b"\x00" * 60,
            media_format="wav", chunk_seq=0, is_final=True,
        )


def test_already_stored_unparsable_chunk_reads_as_422_not_500(_internal_secret):
    """A chunk that predates the upload guard is still in object storage. The read path must not
    500 on it — and must not turn a GOOD recording's playback into a 4xx (only the rebuild path
    parses; an assembled master keeps streaming)."""
    repo, storage = _seeded()
    client = _client_for(repo, storage)
    assert _upload(client, _ffmpeg_wav(0), 0, is_final=True).status_code == 200
    key = next(k for k in storage.blobs if "/audio/" in k)
    storage.blobs[key] = b"\x00" * 80  # corrupt it behind the guard's back
    recs = client.get("/recordings", headers={"X-User-Id": str(USER)}).json()["recordings"]
    rid = recs[0]["id"]
    r = client.get(f"/recordings/{rid}/master?type=audio", headers={"X-User-Id": str(USER)})
    assert r.status_code == 422, r.text
