from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .schema.models import PostMeetingJob

PENDING = "pending"
LEASED = "leased"
RETRYABLE_FAILED = "retryable_failed"
SUCCEEDED = "succeeded"
PERMANENT_FAILED = "permanent_failed"


class StaleLeaseError(Exception):
    pass


@dataclass(frozen=True)
class ClaimedJob:
    job: PostMeetingJob
    lease_token: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PostMeetingJobRepository:
    async def insert_or_get(
        self,
        session: AsyncSession,
        *,
        kind: str,
        meeting_id: int,
        recording_id: str,
        recording_version: int,
    ) -> PostMeetingJob:
        values = {
            "kind": kind,
            "meeting_id": meeting_id,
            "recording_id": recording_id,
            "recording_version": recording_version,
            "status": PENDING,
        }
        created = await session.scalar(
            insert(PostMeetingJob)
            .values(**values)
            .on_conflict_do_nothing(
                constraint="uq_post_meeting_job_identity"
            )
            .returning(PostMeetingJob)
        )
        if created is not None:
            await session.commit()
            return created
        return (await session.execute(
            select(PostMeetingJob).filter_by(**{key: values[key] for key in (
                "kind", "meeting_id", "recording_id", "recording_version",
            )})
        )).scalar_one()

    async def claim(
        self,
        session: AsyncSession,
        *,
        kind: str,
        lease_seconds: int,
    ) -> ClaimedJob | None:
        now = _now()
        token = secrets.token_urlsafe(32)
        candidate = (
            select(PostMeetingJob.id)
            .where(
                PostMeetingJob.kind == kind,
                or_(
                    PostMeetingJob.status.in_((PENDING, RETRYABLE_FAILED)),
                    and_(
                        PostMeetingJob.status == LEASED,
                        PostMeetingJob.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(PostMeetingJob.created_at, PostMeetingJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
            .cte("claim_candidate")
        )
        job = await session.scalar(
            update(PostMeetingJob)
            .where(PostMeetingJob.id == candidate.c.id)
            .values(
                status=LEASED,
                attempts=PostMeetingJob.attempts + 1,
                lease_owner=kind,
                lease_token_hash=_token_hash(token),
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .returning(PostMeetingJob)
        )
        await session.commit()
        return ClaimedJob(job, token) if job is not None else None

    async def renew(
        self, session: AsyncSession, *, job_id: int, lease_token: str, lease_seconds: int
    ) -> PostMeetingJob:
        now = _now()
        job = await session.scalar(
            update(PostMeetingJob)
            .where(
                PostMeetingJob.id == job_id,
                PostMeetingJob.status == LEASED,
                PostMeetingJob.lease_token_hash == _token_hash(lease_token),
                PostMeetingJob.lease_expires_at > now,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            .returning(PostMeetingJob)
        )
        if job is None:
            await session.rollback()
            raise StaleLeaseError
        await session.commit()
        return job

    async def complete(
        self, session: AsyncSession, *, job_id: int, lease_token: str
    ) -> tuple[PostMeetingJob, bool]:
        now = _now()
        digest = _token_hash(lease_token)
        job = await session.scalar(
            update(PostMeetingJob)
            .where(
                PostMeetingJob.id == job_id,
                PostMeetingJob.status == LEASED,
                PostMeetingJob.lease_token_hash == digest,
                PostMeetingJob.lease_expires_at > now,
            )
            .values(status=SUCCEEDED, lease_owner=None, lease_expires_at=None)
            .returning(PostMeetingJob)
        )
        if job is not None:
            await session.commit()
            return job, False
        job = await session.get(PostMeetingJob, job_id)
        if job is not None and job.status == SUCCEEDED and job.lease_token_hash == digest:
            return job, True
        await session.rollback()
        raise StaleLeaseError

    async def fail(
        self, session: AsyncSession, *, job_id: int, lease_token: str, retryable: bool
    ) -> PostMeetingJob:
        now = _now()
        job = await session.scalar(
            update(PostMeetingJob)
            .where(
                PostMeetingJob.id == job_id,
                PostMeetingJob.status == LEASED,
                PostMeetingJob.lease_token_hash == _token_hash(lease_token),
                PostMeetingJob.lease_expires_at > now,
            )
            .values(
                status=RETRYABLE_FAILED if retryable else PERMANENT_FAILED,
                lease_owner=None,
                lease_expires_at=None,
            )
            .returning(PostMeetingJob)
        )
        if job is None:
            await session.rollback()
            raise StaleLeaseError
        await session.commit()
        return job
