"""``python -m meeting_api.post_meeting.diarize_once`` — the post-meeting local-diarization CONSUMER.

One shot, one job: claim a ``local_diarization`` job from admin-api's lease API, read the
recording's finalized audio master through the ``Storage`` port, run the external diarizer, import
the RTTM onto the meeting's transcript segments, complete the lease. Then exit.

A one-shot CLI, not a loop: admin-api's lease already provides mutual exclusion between workers,
so cadence belongs to whatever drives the process (cron, a k8s CronJob, an operator shell). Exit 0
means "nothing left to do for now" — either a job finished or there was none to claim.

Failure discipline (the lease API's two retry classes):
  * TRANSIENT (storage read, diarizer timeout/crash, admin-api unreachable) → ``fail(retryable=True)``;
    admin-api backs off (30s base, 1h cap) and re-offers until ``max_attempts`` (5).
  * STRUCTURAL (no finalized master, version drift, unparsable RTTM, transcript gone) →
    ``fail(retryable=False)``; retrying the same input can only fail the same way.
  * A 409 (stale/expired lease) means ANOTHER worker owns the job — abandon immediately and write
    NOTHING. The lease is re-checked (``renew``) after diarization and before the segment write, so
    a lost lease can never land labels under a foreign worker's job.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import httpx

from ..diarization import _label_segments, _rttm_turns
from ..obs import log_event

KIND = "local_diarization"
SPAN = "post_meeting.diarize"
LEASE_SECONDS = 900
# ponytail: fixed subprocess ceiling — one env knob less. Make it configurable if a real
# recording ever needs longer than 30 minutes of diarizer wall clock.
SUBPROCESS_TIMEOUT_S = 1800


class Transient(Exception):
    """Retryable — the same job may well succeed on the next attempt."""


class Permanent(Exception):
    """Structural — the input cannot become valid by retrying."""


class StaleLease(Exception):
    """Another worker owns this job (HTTP 409). Abandon; write nothing."""


class AdminLeaseClient:
    """The four lease edges on admin-api, authenticated with the post-meeting worker token."""

    def __init__(self, base_url: str, worker_token: str, *, worker_id: Optional[str] = None, transport=None):
        self._base_url = base_url.rstrip("/")
        self._worker_token = worker_token
        self._worker_id = worker_id or f"meeting-api/{uuid.uuid4().hex[:12]}"
        self._transport = transport

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as client:
                response = await client.post(
                    f"{self._base_url}/internal/post-meeting-jobs{path}",
                    headers={"X-Post-Meeting-Worker-Key": self._worker_token},
                    json=payload,
                )
        except httpx.HTTPError as error:
            raise Transient(f"admin-api unreachable: {type(error).__name__}") from error
        if response.status_code == 409:
            raise StaleLease("stale or expired job lease")
        if response.status_code >= 300:
            raise Transient(f"admin-api returned {response.status_code}")
        return response

    async def claim(self) -> Optional[dict[str, Any]]:
        """``{job, lease_token}``, or ``None`` when the queue has nothing ready (204)."""
        response = await self._post("/claim", {
            "kind": KIND, "lease_seconds": LEASE_SECONDS, "worker_id": self._worker_id,
        })
        return None if response.status_code == 204 else response.json()

    async def renew(self, job_id: int, lease_token: str) -> None:
        await self._post(f"/{job_id}/renew", {"lease_token": lease_token, "lease_seconds": LEASE_SECONDS})

    async def complete(self, job_id: int, lease_token: str) -> None:
        await self._post(f"/{job_id}/complete", {"lease_token": lease_token})

    async def fail(self, job_id: int, lease_token: str, *, retryable: bool) -> None:
        await self._post(f"/{job_id}/fail", {"lease_token": lease_token, "retryable": retryable})


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, timeout=SUBPROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired as error:
        raise Transient(f"{command[0]} timed out") from error
    except OSError as error:
        raise Transient(f"{command[0]} is not runnable: {type(error).__name__}") from error
    if result.returncode != 0:
        raise Transient(f"{command[0]} exited {result.returncode}")


def diarize_with_cli(audio: bytes) -> str:
    """The manual pipeline of ``scripts/diarize-recording.sh``, in-process: master → 16k mono wav →
    ``<LOCAL_DIARIZATION_CLI> diarize`` → RTTM text."""
    cli = os.getenv("LOCAL_DIARIZATION_CLI", "argmax-cli")
    with tempfile.TemporaryDirectory(prefix="vexa-diarization.") as tmp:
        source, wav, rttm = Path(tmp) / "master", Path(tmp) / "audio.wav", Path(tmp) / "speakers.rttm"
        source.write_bytes(audio)
        _run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", str(wav)])
        _run([cli, "diarize", "--audio-path", str(wav), "--rttm-path", str(rttm),
              "--use-exclusive-reconciliation"])
        try:
            return rttm.read_text()
        except OSError as error:
            raise Transient("diarizer wrote no RTTM") from error


async def _default_diarizer(audio: bytes) -> str:
    return await asyncio.to_thread(diarize_with_cli, audio)


def _finalized_master(recordings: list[dict], recording_id: str, recording_version: Any) -> str:
    """The storage key of the recording's finalized audio master — the ONLY input this job claims to
    diarize. Anything else is structural: retrying cannot conjure a master."""
    # The job carries recording_id as a STRING (the producer stringifies it); meeting.data keeps
    # whatever the upload wrote.
    recording = next((r for r in recordings if str(r.get("id")) == str(recording_id)), None)
    if recording is None:
        raise Permanent("recording is no longer on the meeting")
    audio = next(
        (media for media in recording.get("media_files") or []
         if isinstance(media, dict) and media.get("type") == "audio"
         and media.get("is_final") and media.get("finalized_by") == "recording_finalizer.master"
         and isinstance(media.get("storage_path"), str)),
        None,
    )
    if audio is None:
        raise Permanent("recording has no finalized audio master")
    if audio.get("assembled_chunk_count") != recording_version:
        # A newer master exists; labelling ITS segments under the OLD job identity would be a silent
        # wrong write. The producer enqueues one job per version, so the current version has its own.
        raise Permanent("master version drifted past the claimed recording_version")
    return audio["storage_path"]


async def process_job(
    job: dict[str, Any], lease_token: str, *, client: AdminLeaseClient,
    recording_repo, storage, transcript_store, diarizer: Callable[[bytes], Awaitable[str]],
) -> int:
    """Diarize one claimed job and write its labels. Returns the number of relabelled segments."""
    meeting_id = job["meeting_id"]
    try:
        recordings = await recording_repo.get_recordings(meeting_id)
        user_id = await recording_repo.owner_of(meeting_id)
    except Exception as error:  # noqa: BLE001 — a DB blip is a retry, not a dead job
        raise Transient(f"recording lookup failed: {type(error).__name__}") from error
    if user_id is None:
        raise Permanent("meeting has no owner")
    key = _finalized_master(recordings, job["recording_id"], job.get("recording_version"))

    try:
        audio = await storage.get(key)
    except Exception as error:  # noqa: BLE001 — object storage is the classic transient
        raise Transient(f"master read failed: {type(error).__name__}") from error

    rttm = await diarizer(audio)
    try:
        turns = _rttm_turns(rttm)
    except ValueError as error:
        raise Permanent(f"unparsable RTTM: {error}") from error

    try:
        transcript = await transcript_store.get_transcript_by_id(user_id, meeting_id)
    except Exception as error:  # noqa: BLE001
        raise Transient(f"transcript read failed: {type(error).__name__}") from error
    if transcript is None:
        raise Permanent("transcript is gone")
    changed = _label_segments(transcript.get("segments") or [], turns)

    # The lease gate: diarization is the long leg, so re-assert ownership HERE — after it, before the
    # only write. A 409 raises StaleLease and nothing is ever written.
    await client.renew(job["id"], lease_token)
    if changed:
        try:
            await transcript_store.upsert_segments(transcript["id"], changed)
        except Exception as error:  # noqa: BLE001
            raise Transient(f"segment write failed: {type(error).__name__}") from error
    return len(changed)


async def run_once(
    *, client: AdminLeaseClient, recording_repo, storage, transcript_store,
    diarizer: Callable[[bytes], Awaitable[str]] = _default_diarizer,
) -> int:
    """Claim → diarize → complete. Returns the process exit code (0 = success or nothing to claim)."""
    try:
        claimed = await client.claim()
    except (Transient, StaleLease) as error:
        log_event("local_diarization_claim_failed", audience="operator", level="warning",
                  span=SPAN, fields={"reason": str(error)})
        return 1
    if claimed is None:
        log_event("local_diarization_idle", audience="operator", span=SPAN)
        return 0

    job, lease_token = claimed["job"], claimed["lease_token"]
    meeting_id = str(job.get("meeting_id"))
    try:
        relabelled = await process_job(
            job, lease_token, client=client, recording_repo=recording_repo, storage=storage,
            transcript_store=transcript_store, diarizer=diarizer,
        )
        await client.complete(job["id"], lease_token)
    except StaleLease as error:
        # Another worker owns this job. It will complete or fail it — we touch nothing, not even fail().
        log_event("local_diarization_lease_lost", audience="operator", level="warning",
                  span=SPAN, meeting_id=meeting_id,
                  fields={"job_id": job.get("id"), "reason": str(error)})
        return 1
    except (Permanent, Transient) as error:
        retryable = isinstance(error, Transient)
        log_event("local_diarization_job_failed", audience="operator", level="warning",
                  span=SPAN, meeting_id=meeting_id,
                  fields={"job_id": job.get("id"), "retryable": retryable, "reason": str(error)})
        try:
            await client.fail(job["id"], lease_token, retryable=retryable)
        except (Transient, StaleLease) as report_error:
            log_event("local_diarization_fail_report_failed", audience="operator", level="warning",
                      span=SPAN, meeting_id=meeting_id,
                      fields={"job_id": job.get("id"), "reason": str(report_error)})
        return 1
    log_event("local_diarization_completed", audience="operator", span=SPAN, meeting_id=meeting_id,
              fields={"job_id": job.get("id"), "relabelled_segments": relabelled})
    return 0


async def _main() -> int:
    base_url = (os.getenv("ADMIN_API_URL") or "").rstrip("/")
    worker_token = os.getenv("POST_MEETING_JOBS_WORKER_TOKEN") or ""
    if not (base_url and worker_token):
        log_event("local_diarization_not_configured", audience="operator", level="error", span=SPAN,
                  fields={"missing": [name for name, value in
                                      (("ADMIN_API_URL", base_url),
                                       ("POST_MEETING_JOBS_WORKER_TOKEN", worker_token)) if not value]})
        return 2

    from ..__main__ import build_recordings_storage, build_session_factory
    from ..collector.adapters import SqlAlchemyTranscriptStore
    from ..recordings.adapters import SqlAlchemyRecordingRepo

    session_factory = build_session_factory()
    return await run_once(
        client=AdminLeaseClient(base_url, worker_token),
        recording_repo=SqlAlchemyRecordingRepo(session_factory),
        storage=build_recordings_storage(),
        transcript_store=SqlAlchemyTranscriptStore(session_factory),
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
