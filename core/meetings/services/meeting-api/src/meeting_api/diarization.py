from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .collector.app import _resolve_user_id
from .collector.ports import TranscriptStore


class RttmImport(BaseModel):
    rttm: str


def _rttm_turns(rttm: str) -> list[tuple[float, float, str]]:
    turns = []
    for line in rttm.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 10 or fields[0] != "SPEAKER":
            raise ValueError("RTTM must contain SPEAKER records with 10 fields")
        try:
            start, duration = float(fields[3]), float(fields[4])
        except ValueError as exc:
            raise ValueError("RTTM times must be numbers") from exc
        if not isfinite(start) or not isfinite(duration) or start < 0 or duration <= 0:
            raise ValueError("RTTM duration must be positive and start non-negative")
        turns.append((start, start + duration, fields[7]))
    speakers = sorted({turn[2] for turn in turns})
    if not turns or len(speakers) > 2:
        raise ValueError("RTTM must contain one or two speakers")
    return turns


def _label_segments(segments: list[dict[str, Any]], turns: list[tuple[float, float, str]]) -> list[dict[str, Any]]:
    labels = {speaker: f"Speaker {'AB'[index]}" for index, speaker in enumerate(sorted({t[2] for t in turns}))}
    changed = []
    for segment in segments:
        try:
            start_value = segment.get("start") if segment.get("start") is not None else segment.get("start_time")
            end_value = segment.get("end") if segment.get("end") is not None else segment.get("end_time")
            if start_value is None or end_value is None:
                continue
            start, end = float(start_value), float(end_value)
        except (TypeError, ValueError):
            continue
        if not isfinite(start) or not isfinite(end) or end <= start:
            continue
        overlap: defaultdict[str, float] = defaultdict(float)
        for turn_start, turn_end, speaker in turns:
            duration = min(end, turn_end) - max(start, turn_start)
            if duration > 0:
                overlap[speaker] += duration
        if overlap and segment.get("segment_id"):
            winner = min(overlap, key=lambda speaker: (-overlap[speaker], speaker))
            label = labels[winner]
            if segment.get("speaker") != label:
                changed.append({**segment, "speaker": label})
    return changed


def build_router(store: TranscriptStore) -> APIRouter:
    router = APIRouter()

    @router.post("/meetings/{platform}/{native_meeting_id}/diarization/rttm")
    async def import_rttm(
        platform: str, native_meeting_id: str, body: RttmImport,
        x_user_id: str | None = Header(None),
    ):
        user_id = _resolve_user_id(x_user_id)
        try:
            turns = _rttm_turns(body.rttm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        transcript = await store.get_transcript(user_id, platform, native_meeting_id)
        if transcript is None:
            raise HTTPException(status_code=404, detail="Meeting not found")
        changed = _label_segments(transcript.get("segments", []), turns)
        if changed:
            await store.upsert_segments(transcript["id"], changed)
        return {"updated_segments": len(changed)}

    return router
