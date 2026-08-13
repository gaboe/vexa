import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from admin_api.app import db as app_db
from admin_api.app.main import create_app
from admin_api.post_meeting_jobs import PostMeetingJobRepository, StaleLeaseError
from admin_api.schema.models import Base, Meeting, PostMeetingJob
from admin_api.schema.sync import ensure_schema_sync
from test_stack_admin_api import _dispose_async_engine

from conftest import requires_docker

pytestmark = requires_docker

INTERNAL_SECRET = "test-internal-secret"


@pytest.fixture()
def client(pg_url, pg_async_url, monkeypatch):
    engine = create_engine(pg_url)
    Base.metadata.drop_all(engine)
    ensure_schema_sync(engine, Base)
    engine.dispose()
    monkeypatch.setenv("POST_MEETING_JOBS_WORKER_TOKEN", "test-worker-key")
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)
    app_db.configure(pg_async_url)
    with TestClient(create_app()) as test_client:
        yield test_client
    _dispose_async_engine()


def _worker():
    return {"X-Post-Meeting-Worker-Key": "test-worker-key"}


def _internal():
    return {"X-Internal-Secret": INTERNAL_SECRET}


def _seed_meeting(pg_url):
    engine = create_engine(pg_url)
    with Session(engine) as session:
        meeting = Meeting(user_id=1, platform="test", status="completed")
        session.add(meeting)
        session.commit()
        meeting_id = meeting.id
    engine.dispose()
    return meeting_id


def _job_count(pg_url):
    engine = create_engine(pg_url)
    with Session(engine) as session:
        count = session.scalar(select(func.count()).select_from(PostMeetingJob))
    engine.dispose()
    return count


def _seed_job(pg_url, *, kind="diarization"):
    engine = create_engine(pg_url)
    with Session(engine) as session:
        meeting = Meeting(user_id=1, platform="test", status="completed")
        session.add(meeting)
        session.flush()
        job = PostMeetingJob(
            kind=kind,
            meeting_id=meeting.id,
            recording_id="recording-1",
            recording_version=1,
            status="pending",
        )
        session.add(job)
        session.commit()
        job_id = job.id
    engine.dispose()
    return job_id


def test_sql_repository_persists_identity_leases_and_terminal_idempotency(pg_url, pg_async_url):
    engine = create_engine(pg_url)
    Base.metadata.drop_all(engine)
    ensure_schema_sync(engine, Base)
    with Session(engine) as session:
        meeting = Meeting(user_id=1, platform="test", status="completed")
        session.add(meeting)
        session.commit()
        meeting_id = meeting.id
    engine.dispose()
    app_db.configure(pg_async_url)

    async def exercise():
        sessions = async_sessionmaker(app_db.get_engine(), expire_on_commit=False)
        repo = PostMeetingJobRepository()
        async with sessions() as session:
            first = await repo.insert_or_get(
                session, kind="diarization", meeting_id=meeting_id,
                recording_id="recording-1", recording_version=1,
            )
            duplicate = await repo.insert_or_get(
                session, kind="diarization", meeting_id=meeting_id,
                recording_id="recording-1", recording_version=1,
            )
            assert duplicate.id == first.id
            job_id = first.id
            claimed = await repo.claim(session, kind="diarization", lease_seconds=60)
            assert claimed is not None and claimed.job.attempts == 1
            assert await repo.claim(session, kind="diarization", lease_seconds=60) is None
            await session.execute(update(PostMeetingJob).where(PostMeetingJob.id == job_id).values(
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            ))
            await session.commit()
            reclaimed = await repo.claim(session, kind="diarization", lease_seconds=60)
            assert reclaimed is not None and reclaimed.job.attempts == 2
            with pytest.raises(StaleLeaseError):
                await repo.complete(session, job_id=job_id, lease_token=claimed.lease_token)
            completed, idempotent = await repo.complete(
                session, job_id=job_id, lease_token=reclaimed.lease_token
            )
            assert completed.status == "succeeded" and idempotent is False
            repeated, idempotent = await repo.complete(
                session, job_id=job_id, lease_token=reclaimed.lease_token
            )
            assert repeated.id == job_id and idempotent is True
        await app_db.get_engine().dispose()

    asyncio.run(exercise())


def test_enqueue_requires_internal_secret_and_valid_identity(client, pg_url):
    payload = {
        "kind": "diarization",
        "meeting_id": _seed_meeting(pg_url),
        "recording_id": "recording-1",
        "recording_version": 1,
    }
    assert client.post("/internal/post-meeting-jobs", json=payload).status_code == 403
    assert client.post(
        "/internal/post-meeting-jobs", headers={"X-Internal-Secret": "wrong"}, json=payload
    ).status_code == 403
    assert client.post(
        "/internal/post-meeting-jobs", headers=_internal(), json={"kind": ""}
    ).status_code == 422
    assert _job_count(pg_url) == 0


def test_enqueue_is_idempotent_and_redacts_worker_credentials(client, pg_url):
    payload = {
        "kind": "diarization",
        "meeting_id": _seed_meeting(pg_url),
        "recording_id": "recording-1",
        "recording_version": 1,
    }
    first = client.post("/internal/post-meeting-jobs", headers=_internal(), json=payload)
    second = client.post("/internal/post-meeting-jobs", headers=_internal(), json=payload)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["job"].items() >= payload.items()
    assert set(first.json()) == {"job"}
    assert not ({"lease_token", "lease_token_hash", "lease_owner", "worker_key"} & first.json()["job"].keys())
    assert _job_count(pg_url) == 1


def test_enqueue_stays_disabled_without_worker_credential_and_creates_no_job(client, pg_url, monkeypatch):
    monkeypatch.delenv("POST_MEETING_JOBS_WORKER_TOKEN")
    response = client.post(
        "/internal/post-meeting-jobs", headers=_internal(), json={
            "kind": "diarization", "meeting_id": _seed_meeting(pg_url),
            "recording_id": "recording-1", "recording_version": 1,
        }
    )
    assert response.status_code == 503
    assert _job_count(pg_url) == 0


def test_worker_routes_claim_once_reclaim_expired_and_reject_stale_lease(client, pg_url):
    job_id = _seed_job(pg_url)
    assert client.post("/internal/post-meeting-jobs/claim", json={"kind": "diarization"}).status_code == 403

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: client.post("/internal/post-meeting-jobs/claim", headers=_worker(), json={"kind": "diarization"}),
            range(2),
        ))
    claims = [result for result in results if result.status_code == 200]
    assert len(claims) == 1
    first = claims[0].json()
    assert first["job"]["id"] == job_id and first["job"]["attempts"] == 1
    assert any(result.status_code == 204 for result in results)
    renewed = client.post(
        f"/internal/post-meeting-jobs/{job_id}/renew", headers=_worker(),
        json={"lease_token": first["lease_token"], "lease_seconds": 60},
    )
    assert renewed.status_code == 200 and renewed.json()["job"]["status"] == "leased"

    engine = create_engine(pg_url)
    with Session(engine) as session:
        session.execute(update(PostMeetingJob).where(PostMeetingJob.id == job_id).values(
            lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        ))
        session.commit()
    engine.dispose()

    second = client.post("/internal/post-meeting-jobs/claim", headers=_worker(), json={"kind": "diarization"})
    assert second.status_code == 200, second.text
    reclaimed = second.json()
    assert reclaimed["job"]["attempts"] == 2

    stale = client.post(
        f"/internal/post-meeting-jobs/{job_id}/complete", headers=_worker(),
        json={"lease_token": first["lease_token"]},
    )
    assert stale.status_code == 409
    complete = client.post(
        f"/internal/post-meeting-jobs/{job_id}/complete", headers=_worker(),
        json={"lease_token": reclaimed["lease_token"]},
    )
    assert complete.status_code == 200 and complete.json()["idempotent"] is False
    duplicate = client.post(
        f"/internal/post-meeting-jobs/{job_id}/complete", headers=_worker(),
        json={"lease_token": reclaimed["lease_token"]},
    )
    assert duplicate.status_code == 200 and duplicate.json()["idempotent"] is True

    retry_job_id = _seed_job(pg_url)
    retry_claim = client.post(
        "/internal/post-meeting-jobs/claim", headers=_worker(), json={"kind": "diarization"}
    ).json()
    assert retry_claim["job"]["id"] == retry_job_id
    failed = client.post(
        f"/internal/post-meeting-jobs/{retry_job_id}/fail", headers=_worker(),
        json={"lease_token": retry_claim["lease_token"], "retryable": True},
    )
    assert failed.status_code == 200 and failed.json()["job"]["status"] == "retryable_failed"


def test_worker_routes_stay_disabled_without_worker_credential(client, monkeypatch):
    monkeypatch.delenv("POST_MEETING_JOBS_WORKER_TOKEN")
    response = client.post("/internal/post-meeting-jobs/claim", json={"kind": "diarization"})
    assert response.status_code == 503
