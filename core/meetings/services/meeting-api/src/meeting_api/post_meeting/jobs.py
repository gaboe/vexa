"""Durable storage for post-meeting jobs; execution is intentionally outside this module."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Protocol


class JobStatus(str, Enum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"


_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.PERMANENT_FAILED})
_TRANSITIONS = {
    JobStatus.LEASED: frozenset({JobStatus.SUCCEEDED, JobStatus.RETRYABLE_FAILED, JobStatus.PERMANENT_FAILED}),
    JobStatus.RETRYABLE_FAILED: frozenset({JobStatus.PENDING}),
}


@dataclass(frozen=True)
class PostMeetingJob:
    id: int
    kind: str
    meeting_id: int
    recording_id: str
    recording_version: int
    status: JobStatus
    attempts: int = 0
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[datetime] = None


class PostMeetingJobRepository(Protocol):
    async def insert_or_get(self, *, kind: str, meeting_id: int, recording_id: str, recording_version: int) -> PostMeetingJob: ...
    async def claim(self, *, job_id: int, lease_owner: str, lease_for: timedelta, now: datetime) -> Optional[PostMeetingJob]: ...
    async def transition(self, *, job_id: int, status: JobStatus) -> Optional[PostMeetingJob]: ...


class InMemoryPostMeetingJobRepository:
    """Test adapter matching production claim and state semantics."""

    def __init__(self) -> None:
        self._jobs: dict[int, PostMeetingJob] = {}
        self._identities: dict[tuple[str, int, str, int], int] = {}

    async def insert_or_get(self, *, kind: str, meeting_id: int, recording_id: str, recording_version: int) -> PostMeetingJob:
        identity = (kind, meeting_id, recording_id, recording_version)
        if job_id := self._identities.get(identity):
            return self._jobs[job_id]
        job = PostMeetingJob(len(self._jobs) + 1, *identity, status=JobStatus.PENDING)
        self._jobs[job.id] = job
        self._identities[identity] = job.id
        return job

    async def claim(self, *, job_id: int, lease_owner: str, lease_for: timedelta, now: datetime) -> Optional[PostMeetingJob]:
        job = self._jobs.get(job_id)
        if not job or job.status in _TERMINAL or (
            job.status is JobStatus.LEASED and job.lease_expires_at is not None and job.lease_expires_at > now
        ):
            return None
        claimed = replace(job, status=JobStatus.LEASED, attempts=job.attempts + 1, lease_owner=lease_owner, lease_expires_at=now + lease_for)
        self._jobs[job_id] = claimed
        return claimed

    async def transition(self, *, job_id: int, status: JobStatus) -> Optional[PostMeetingJob]:
        job = self._jobs.get(job_id)
        if not job or status not in _TRANSITIONS.get(job.status, frozenset()):
            return None
        updated = replace(job, status=status, lease_owner=None, lease_expires_at=None)
        self._jobs[job_id] = updated
        return updated

