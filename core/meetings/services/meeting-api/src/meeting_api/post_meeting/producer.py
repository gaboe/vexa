from __future__ import annotations

import asyncio
import os
from typing import Any, Optional, Protocol

import httpx

from ..bot_spawn.env_flags import env_flag
from ..obs import log_event


class PostMeetingJobClient(Protocol):
    async def enqueue(self, identity: dict[str, Any]) -> bool: ...


class AdminPostMeetingJobClient:
    def __init__(self, base_url: str, internal_secret: str, *, transport=None):
        self._base_url = base_url.rstrip("/")
        self._internal_secret = internal_secret
        self._transport = transport

    async def enqueue(self, identity: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}/internal/post-meeting-jobs",
                    headers={"X-Internal-Secret": self._internal_secret},
                    json=identity,
                )
        except httpx.HTTPError as error:
            log_event(
                "local_diarization_enqueue_failed", audience="operator", level="warning",
                span="post_meeting.enqueue", meeting_id=str(identity["meeting_id"]),
                fields={"error_type": type(error).__name__},
            )
            return False
        if response.status_code != 200:
            log_event(
                "local_diarization_enqueue_failed", audience="operator", level="warning",
                span="post_meeting.enqueue", meeting_id=str(identity["meeting_id"]),
                fields={"status_code": response.status_code},
            )
            return False
        return True


class LocalDiarizationProducer:
    def __init__(self, client: Optional[PostMeetingJobClient] = None):
        self._client = client
        self._sent: set[tuple[str, int, str, int]] = set()
        self._lock = asyncio.Lock()

    def _client_from_env(self) -> Optional[PostMeetingJobClient]:
        if self._client is not None:
            return self._client
        base_url = (os.getenv("ADMIN_API_URL") or "").rstrip("/")
        internal_secret = os.getenv("INTERNAL_API_SECRET") or ""
        if not (base_url and internal_secret):
            return None
        self._client = AdminPostMeetingJobClient(base_url, internal_secret)
        return self._client

    async def enqueue_completed_recordings(self, meeting: dict[str, Any]) -> None:
        if not env_flag("LOCAL_DIARIZATION_ENABLED", default=False) or meeting.get("status") != "completed":
            return
        meeting_id = meeting.get("id")
        if not isinstance(meeting_id, int) or meeting_id < 1:
            return
        recordings = (meeting.get("data") or {}).get("recordings")
        if not isinstance(recordings, list):
            return
        client = self._client_from_env()
        if client is None:
            return
        for recording in recordings:
            if not isinstance(recording, dict) or recording.get("id") is None:
                continue
            audio = next(
                (media for media in recording.get("media_files", [])
                 if isinstance(media, dict) and media.get("type") == "audio"),
                None,
            )
            version = (audio or {}).get("assembled_chunk_count")
            if not (
                audio and audio.get("is_final")
                and audio.get("finalized_by") == "recording_finalizer.master"
                and isinstance(version, int) and version > 0
            ):
                continue
            identity = {
                "kind": "local_diarization",
                "meeting_id": meeting_id,
                "recording_id": str(recording["id"]),
                "recording_version": version,
            }
            key = (identity["kind"], meeting_id, identity["recording_id"], version)
            async with self._lock:
                if key in self._sent:
                    continue
                try:
                    sent = await client.enqueue(identity)
                except Exception as error:  # noqa: BLE001 — post-meeting work never breaks lifecycle
                    log_event(
                        "local_diarization_enqueue_failed", audience="operator", level="warning",
                        span="post_meeting.enqueue", meeting_id=str(meeting_id),
                        fields={"error_type": type(error).__name__},
                    )
                    sent = False
                if sent:
                    self._sent.add(key)
