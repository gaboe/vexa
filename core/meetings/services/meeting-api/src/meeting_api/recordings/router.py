"""The recordings routes — mounted onto the unified meeting-api app (modular monolith, P2).

  * **POST /internal/recordings/upload** — the bot's chunk upload. Auth via the MeetingToken it
    carries (``Authorization: Bearer <token>``, re-verified here — the parent's
    ``require_recording_upload_token``). Multipart form: ``file`` + ``session_uid`` + media metadata.
    Folds the chunk into ``meeting.data['recordings']`` JSONB. ``include_in_schema=False`` (internal).
  * **GET /recordings** — the caller's recordings (from ``meeting.data``), scoped by the
    gateway-injected ``x-user-id``.
  * **GET /recordings/{recording_id}/master?type=audio|video** — finalize-on-read: build + upload the
    master if absent, then return its storage key. (The byte stream / Range download is P3.)
"""
from __future__ import annotations

import json
import os
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile

from ..collector.ports import TranscriptStore
from fastapi.responses import JSONResponse, Response

from .ports import RecordingRepo, Storage
from .service import (
    SIGNAL_MEDIA_TYPE,
    InvalidRecordingChunk,
    InvalidSignalTape,
    SessionNotFound,
    _verify_meeting_token,
    finalize_master,
    upload_chunk,
    upload_signal_tape,
)


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing recording upload token")
    return authorization.split(" ", 1)[1].strip()


def _resolve_user_id(x_user_id: Optional[str]) -> int:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing user identity")
    try:
        return int(x_user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid user identity")


def _parse_range(range_header: Optional[str], total: int) -> Optional[tuple[int, int]]:
    """Parse an HTTP ``Range: bytes=start-end`` header against a known total size.

    Returns the resolved INCLUSIVE ``(start, end)`` byte offsets, or ``None`` when there is no
    range / it is not a byte range we honor (caller serves the full 200 body). Raises
    ``HTTPException(416)`` for a syntactically valid but unsatisfiable range. Forms handled:
    ``bytes=start-end``, ``bytes=start-`` (to EOF), ``bytes=-suffix`` (last N bytes); a multi-range
    header (commas) honors only the FIRST range.
    """
    if not range_header:
        return None
    spec = range_header.strip()
    if not spec.lower().startswith("bytes="):
        return None  # only byte ranges; fall back to full body
    spec = spec[len("bytes="):]
    first = spec.split(",", 1)[0].strip()  # multi-range → honor the first
    if "-" not in first:
        return None
    start_s, _, end_s = first.partition("-")
    start_s, end_s = start_s.strip(), end_s.strip()
    try:
        if start_s == "":
            # bytes=-suffix → the last `suffix` bytes
            if end_s == "":
                return None
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, total - suffix)
            end = total - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else total - 1
    except ValueError:
        return None  # unparseable → fall back to full body
    if start < 0:
        return None
    if start >= total:
        # Syntactically valid but unsatisfiable → 416 with Content-Range: bytes */total.
        raise HTTPException(
            status_code=416,
            detail="Requested range not satisfiable",
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )
    end = min(end, total - 1)  # clamp to the last byte
    if end < start:
        return None  # e.g. bytes=5-3 → ignore, serve full body
    return start, end


async def _storage_size(storage: Storage, key: str) -> Optional[int]:
    """Object size without fetching the body when the adapter exposes ``size``; else ``None``."""
    sizer = getattr(storage, "size", None)
    if sizer is None:
        return None
    return await sizer(key)


async def _storage_get_range(storage: Storage, key: str, start: int, end: int) -> Optional[bytes]:
    """Fetch ONLY ``[start, end]`` (inclusive) when the adapter exposes ``get_range`` (S3 passes the
    Range through to ``get_object``); else ``None`` so the caller slices a full ``get()``."""
    getter = getattr(storage, "get_range", None)
    if getter is None:
        return None
    return await getter(key, start, end)


def build_router(
    repo: RecordingRepo,
    storage: Storage,
    *,
    token_secret: Optional[str] = None,
    transcript_store: Optional[TranscriptStore] = None,
    on_audio_finalized=None,
) -> APIRouter:
    """The recordings routes over the injected ``RecordingRepo`` + ``Storage`` ports."""
    router = APIRouter()

    async def _finalize(**kwargs) -> Optional[str]:
        """Every route's single door to ``finalize_master`` — the read paths assemble on read, so a
        chunk the codec cannot parse must surface as unprocessable INPUT (422), never a 500. Only a
        rebuild parses chunks, so an already-assembled master keeps streaming untouched: a good
        recording's playback can't be turned into a 4xx by this."""
        try:
            return await finalize_master(repo, storage, **kwargs)
        except InvalidRecordingChunk as e:
            raise HTTPException(status_code=422, detail=f"Recording chunk cannot be assembled: {e}")

    async def _owned_recording(recording_id: int, x_user_id: Optional[str]) -> dict:
        user_id = _resolve_user_id(x_user_id)
        recording = next(
            (item for item in await repo.list_meeting_recordings(user_id) if item.get("id") == recording_id),
            None,
        )
        if recording is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        return recording

    def _audio_preflight(recording: dict) -> dict:
        audio = next((item for item in recording.get("media_files", []) if item.get("type") == "audio"), None)
        return {
            "recording_id": recording["id"],
            "meeting_id": recording.get("meeting_id"),
            "eligible": bool(audio and audio.get("is_final")),
            "reason": None if audio and audio.get("is_final") else "Recording audio is not finalized",
            "audio": {
                "is_final": bool(audio and audio.get("is_final")),
                "chunk_count": (audio or {}).get("chunk_count", 0),
                "storage_path": (audio or {}).get("storage_path"),
            },
        }

    @router.get("/recordings/{recording_id}/transcription/preflight")
    async def transcription_preflight(
        recording_id: int, x_user_id: Optional[str] = Header(default=None),
    ):
        return _audio_preflight(await _owned_recording(recording_id, x_user_id))

    @router.post("/recordings/{recording_id}/transcription/retry")
    async def retry_recording_master(
        recording_id: int, x_user_id: Optional[str] = Header(default=None),
    ):
        recording = await _owned_recording(recording_id, x_user_id)
        preflight = _audio_preflight(recording)
        if not preflight["eligible"]:
            raise HTTPException(status_code=409, detail=preflight["reason"])
        master_key = await _finalize(
            meeting_id=recording["meeting_id"], recording_id=recording_id, media_type="audio",
            on_audio_finalized=on_audio_finalized,
        )
        if master_key is None:
            raise HTTPException(status_code=409, detail="Recording master is unavailable")
        if transcript_store is None:
            return {**preflight, "master_key": master_key, "transcription_dispatched": False}
        stt_url = os.getenv("TRANSCRIPTION_SERVICE_URL", "").rstrip("/")
        if not stt_url:
            raise HTTPException(status_code=503, detail="Transcription service is not configured")
        try:
            async with httpx.AsyncClient(timeout=7200.0) as client:
                audio_format = next(
                    (item.get("format") for item in recording.get("media_files", []) if item.get("type") == "audio"),
                    "wav",
                )
                response = await client.post(
                    f"{stt_url}/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {os.getenv('TRANSCRIPTION_SERVICE_TOKEN', '')}"},
                    files={"file": (f"recording.{audio_format}", await storage.get(master_key), f"audio/{audio_format}")},
                    data={"model": os.getenv("TRANSCRIPTION_MODEL") or "whisper-1", "response_format": "verbose_json"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(status_code=502, detail="Transcription retry failed") from error
        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            raise HTTPException(status_code=502, detail="Transcription retry returned no segments")
        segments = [
            {"segment_id": f"recording-retry:{recording_id}:{index}", "start": item.get("start", 0),
             "end": item.get("end", 0), "text": str(item.get("text", "")).strip(), "speaker": None,
             "language": payload.get("language"), "session_uid": f"recording-retry:{recording_id}"}
            for index, item in enumerate(raw_segments)
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        if not segments:
            raise HTTPException(status_code=502, detail="Transcription retry returned empty segments")
        await transcript_store.upsert_segments(recording["meeting_id"], segments)
        return {**preflight, "master_key": master_key, "transcription_dispatched": True,
                "persisted_segments": len(segments), "language": payload.get("language")}

    @router.post("/internal/recordings/upload", include_in_schema=False)
    async def internal_upload_recording(
        file: UploadFile = File(...),
        session_uid: Optional[str] = Form(None),
        media_type: Optional[str] = Form(None),
        media_format: Optional[str] = Form(None),
        chunk_seq: Optional[int] = Form(None),
        is_final: Optional[bool] = Form(None),
        duration_seconds: Optional[float] = Form(None),
        sample_rate: Optional[int] = Form(None),
        metadata: Optional[str] = Form(None),
        authorization: Optional[str] = Header(default=None),
    ):
        # The bot's RecordingService sends a JSON `metadata` part + the `file` (with flat chunk_seq/
        # is_final on chunk uploads). Parse `metadata` for the fields it carries (session_uid lives
        # only there); any flat Form field overrides its metadata counterpart.
        meta: dict = {}
        if metadata:
            try:
                meta = json.loads(metadata)
            except (ValueError, TypeError):
                meta = {}
        session_uid = session_uid or meta.get("session_uid")
        if not session_uid:
            raise HTTPException(status_code=422, detail="session_uid required (flat field or metadata)")
        media_type = media_type or meta.get("media_type") or "audio"
        media_format = media_format or meta.get("media_format") or meta.get("format") or "wav"
        # O-TEL-1 — a captured-signal tape rides this same endpoint (one internal upload edge, one
        # set of credentials, one network path already open from every bot) but takes a DIFFERENT
        # server path: no JSONB fold, no chunking, no master. See service.upload_signal_tape.
        if media_type == SIGNAL_MEDIA_TYPE:
            part = str(meta.get("part") or "")
            bearer = _bearer_token(authorization)
            internal_secret = os.getenv("INTERNAL_API_SECRET")
            token_meeting_id = None
            if not (internal_secret and bearer == internal_secret):
                try:
                    claims = _verify_meeting_token(bearer, secret=token_secret)
                except ValueError as e:
                    raise HTTPException(status_code=401,
                                        detail=f"Invalid recording upload token: {e}")
                token_meeting_id = int(claims["meeting_id"])
            try:
                receipt = await upload_signal_tape(
                    repo, storage,
                    token_meeting_id=token_meeting_id,
                    session_uid=session_uid, data=await file.read(),
                    part=part, media_format=media_format,
                )
            except InvalidSignalTape as e:
                raise HTTPException(status_code=422, detail=str(e))
            except SessionNotFound as e:
                raise HTTPException(status_code=404, detail=str(e))
            return JSONResponse(content=receipt)
        try:
            chunk_seq = chunk_seq if chunk_seq is not None else int(meta.get("chunk_seq", 0) or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="chunk_seq must be an integer")
        is_final = is_final if is_final is not None else bool(meta.get("is_final", True))
        duration_seconds = duration_seconds if duration_seconds is not None else meta.get("duration_seconds")
        sample_rate = sample_rate if sample_rate is not None else meta.get("sample_rate")

        # Auth: accept either the INTERNAL_API_SECRET (the bot's internal upload uses it, like the
        # lifecycle callback; meeting is scoped by session_uid) OR a MeetingToken (carries its meeting_id).
        bearer = _bearer_token(authorization)
        internal_secret = os.getenv("INTERNAL_API_SECRET")
        token_meeting_id: Optional[int] = None
        if internal_secret and bearer == internal_secret:
            token_meeting_id = None  # internal auth → scope by session; skip the MeetingToken cross-check
        else:
            try:
                claims = _verify_meeting_token(bearer, secret=token_secret)
            except ValueError as e:
                raise HTTPException(status_code=401, detail=f"Invalid recording upload token: {e}")
            try:
                token_meeting_id = int(claims["meeting_id"])
            except (KeyError, TypeError, ValueError) as error:
                raise HTTPException(status_code=401, detail="Invalid recording upload token meeting") from error

        data = await file.read()
        try:
            receipt = await upload_chunk(
                repo, storage,
                token_meeting_id=token_meeting_id,
                session_uid=session_uid, data=data,
                media_type=media_type, media_format=media_format,
                chunk_seq=chunk_seq, is_final=is_final,
                duration_seconds=duration_seconds, sample_rate=sample_rate,
            )
        except SessionNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))
        except InvalidRecordingChunk as e:
            raise HTTPException(status_code=422, detail=str(e))
        return JSONResponse(content=receipt)

    @router.get("/recordings")
    async def list_recordings(
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ):
        user_id = _resolve_user_id(x_user_id)
        recs = await repo.list_meeting_recordings(user_id)
        return JSONResponse(content={"recordings": recs})

    @router.get("/recordings/{recording_id}")
    async def get_recording(
        recording_id: int,
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ):
        """Recording detail (api/meetings.mdx: GET /recordings/{recording_id}) — the single recording
        record, scoped to the caller. 404 if the id isn't one of the caller's recordings."""
        user_id = _resolve_user_id(x_user_id)
        recs = await repo.list_meeting_recordings(user_id)
        rec = next((r for r in recs if r.get("id") == recording_id), None)
        if rec is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        return JSONResponse(content=rec)

    @router.get("/recordings/{recording_id}/master")
    async def get_recording_master(
        recording_id: int,
        request: Request,
        type: str = "audio",
        x_user_id: Optional[str] = Header(default=None),
    ):
        user_id = _resolve_user_id(x_user_id)
        # Find which meeting owns this recording (scoped to the caller).
        recs = await repo.list_meeting_recordings(user_id)
        rec = next((r for r in recs if r.get("id") == recording_id), None)
        if rec is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        mf = next((m for m in rec.get("media_files", []) if m.get("type") == type), None)
        master_key = await _finalize(
            meeting_id=rec["meeting_id"], recording_id=recording_id, media_type=type,
            on_audio_finalized=on_audio_finalized,
        )
        if master_key is None:
            raise HTTPException(status_code=404, detail="No such media file to finalize")
        # The dashboard player (api.ts getRecordingMasterStreamUrl) reads ``raw_url`` and streams it via
        # the proxy — the master metadata (``storage_path``) alone is not playable. Point it at the byte
        # route below so playback actually resolves (recordings P3: master byte stream).
        media_file_id = (mf or {}).get("id")
        raw_url = (
            f"/recordings/{recording_id}/media/{media_file_id}/raw?type={type}"
            if media_file_id is not None
            else None
        )
        return JSONResponse(content={
            "id": recording_id,
            "type": type,
            "storage_path": master_key,
            "media_file_id": media_file_id,
            "raw_url": raw_url,
            "duration_seconds": (mf or {}).get("duration_seconds"),
        })

    @router.get("/recordings/{recording_id}/media/{media_file_id}/raw")
    async def get_recording_media_raw(
        recording_id: int,
        media_file_id: int,
        request: Request,
        type: str = "audio",
        x_user_id: Optional[str] = Header(default=None),
    ):
        # Stream the finalized master bytes from object storage (recordings P3). The player fetches
        # /master first (which finalizes), then this; finalize-on-read here too as a safety net.
        user_id = _resolve_user_id(x_user_id)
        recs = await repo.list_meeting_recordings(user_id)
        rec = next((r for r in recs if r.get("id") == recording_id), None)
        if rec is None:
            raise HTTPException(status_code=404, detail="Recording not found")
        mf = next(
            (m for m in rec.get("media_files", []) if str(m.get("id")) == str(media_file_id)),
            None,
        )
        if mf is None:
            raise HTTPException(status_code=404, detail="No such media file")
        # Finalize-on-read, ALWAYS: serve the ASSEMBLED master, never a raw part or the empty
        # final-signal chunk. The ONLY playable object is master.<fmt>. finalize_master is
        # re-assemblable and idempotent (#768) — it rebuilds only when new chunks arrived since the
        # master was last assembled — so calling it unconditionally reflects late chunks and makes a
        # mid-recording read impossible to freeze at the retrieval boundary too. (A prior guard that
        # skipped finalize once storage_path already named the master key was the second freeze door:
        # after a mid-meeting /master it never re-assembled, serving the stale partial forever.)
        await _finalize(
            meeting_id=rec["meeting_id"], recording_id=recording_id,
            media_type=mf.get("type", type), on_audio_finalized=on_audio_finalized,
        )
        recs = await repo.list_meeting_recordings(user_id)
        rec = next((r for r in recs if r.get("id") == recording_id), rec)
        mf = next(
            (m for m in (rec or {}).get("media_files", []) if str(m.get("id")) == str(media_file_id)),
            mf,
        )
        storage_path = mf.get("storage_path")
        if not storage_path:
            raise HTTPException(status_code=404, detail="Media file has no storage path")
        media_format = mf.get("format", "webm")
        if media_format == "wav":
            content_type = "audio/wav"
        elif media_format == "webm":
            content_type = "audio/webm" if mf.get("type") == "audio" else "video/webm"
        else:
            content_type = "application/octet-stream"

        # Honor HTTP Range so the <audio>/<video> element + dashboard proxy can seek without
        # downloading the whole master. Resolve total size cheaply (S3 head) when we can; only fall
        # back to fetching the full body if neither size() nor get_range() are available.
        range_header = request.headers.get("range") or request.headers.get("Range")
        total = await _storage_size(storage, storage_path)
        full_body: Optional[bytes] = None
        if total is None:
            full_body = await storage.get(storage_path)
            total = len(full_body)

        rng = _parse_range(range_header, total)  # may raise 416
        if rng is None:
            data = full_body if full_body is not None else await storage.get(storage_path)
            return Response(
                content=data,
                media_type=content_type,
                headers={"Accept-Ranges": "bytes", "Content-Length": str(len(data))},
            )

        start, end = rng
        slice_bytes: Optional[bytes] = None
        if full_body is None:
            slice_bytes = await _storage_get_range(storage, storage_path, start, end)
        if slice_bytes is None:
            if full_body is None:
                full_body = await storage.get(storage_path)
            slice_bytes = full_body[start : end + 1]
        return Response(
            content=slice_bytes,
            status_code=206,
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Content-Length": str(len(slice_bytes)),
            },
        )

    return router
