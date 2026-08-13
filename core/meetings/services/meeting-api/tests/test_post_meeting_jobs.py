import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from meeting_api.post_meeting import InMemoryPostMeetingJobRepository, JobStatus


@pytest.mark.asyncio
async def test_insert_or_get_is_idempotent_on_job_identity():
    repo = InMemoryPostMeetingJobRepository()

    first = await repo.insert_or_get(kind="diarization", meeting_id=7, recording_id="rec-1", recording_version=2)
    second = await repo.insert_or_get(kind="diarization", meeting_id=7, recording_id="rec-1", recording_version=2)

    assert second == first
    assert first.status is JobStatus.PENDING
    assert first.attempts == 0


@pytest.mark.asyncio
async def test_one_atomic_claim_winner():
    repo = InMemoryPostMeetingJobRepository()
    job = await repo.insert_or_get(kind="diarization", meeting_id=7, recording_id="rec-1", recording_version=2)
    now = datetime.now(timezone.utc)

    claims = await asyncio.gather(*(repo.claim(job_id=job.id, lease_owner=f"worker-{i}", lease_for=timedelta(minutes=1), now=now) for i in range(2)))

    winners = [claim for claim in claims if claim]
    assert len(winners) == 1
    assert winners[0].status is JobStatus.LEASED
    assert winners[0].attempts == 1


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_counts_attempt():
    repo = InMemoryPostMeetingJobRepository()
    job = await repo.insert_or_get(kind="diarization", meeting_id=7, recording_id="rec-1", recording_version=2)
    now = datetime.now(timezone.utc)
    first = await repo.claim(job_id=job.id, lease_owner="lost-worker", lease_for=timedelta(seconds=1), now=now)

    reclaimed = await repo.claim(job_id=job.id, lease_owner="new-worker", lease_for=timedelta(minutes=1), now=now + timedelta(seconds=2))

    assert first is not None
    assert reclaimed is not None
    assert reclaimed.lease_owner == "new-worker"
    assert reclaimed.attempts == first.attempts + 1


@pytest.mark.asyncio
async def test_valid_state_transitions_and_terminal_rejection():
    repo = InMemoryPostMeetingJobRepository()
    job = await repo.insert_or_get(kind="diarization", meeting_id=7, recording_id="rec-1", recording_version=2)
    claimed = await repo.claim(job_id=job.id, lease_owner="worker", lease_for=timedelta(minutes=1), now=datetime.now(timezone.utc))

    retryable = await repo.transition(job_id=job.id, status=JobStatus.RETRYABLE_FAILED)
    pending = await repo.transition(job_id=job.id, status=JobStatus.PENDING)
    reclaimed = await repo.claim(job_id=job.id, lease_owner="worker", lease_for=timedelta(minutes=1), now=datetime.now(timezone.utc))
    succeeded = await repo.transition(job_id=job.id, status=JobStatus.SUCCEEDED)

    assert claimed is not None
    assert retryable is not None
    assert pending is not None
    assert reclaimed is not None
    assert succeeded is not None
    assert claimed.status is JobStatus.LEASED
    assert retryable.status is JobStatus.RETRYABLE_FAILED
    assert pending.status is JobStatus.PENDING
    assert reclaimed.status is JobStatus.LEASED
    assert succeeded.status is JobStatus.SUCCEEDED
    assert await repo.transition(job_id=job.id, status=JobStatus.RETRYABLE_FAILED) is None
