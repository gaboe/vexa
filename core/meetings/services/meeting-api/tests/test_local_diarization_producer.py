import json

import httpx
import pytest
from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.post_meeting import AdminPostMeetingJobClient, LocalDiarizationProducer
from meeting_api.recordings import upload_chunk
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage


class CapturingTransport:
    def __init__(self, status_code=200):
        self.requests = []
        self.status_code = status_code

    async def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(self.status_code, request=request, json={"job": {}})


def _wav():
    return (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x04\x00\x00\x00\x00\x00\x00\x00"
    )


def _meeting(*, status="completed", finalized=True, recording=True):
    media_files = []
    if recording:
        media_files = [{
            "type": "audio", "is_final": finalized,
            "finalized_by": "recording_finalizer.master" if finalized else None,
            "assembled_chunk_count": 3 if finalized else None,
        }]
    return {"id": 7, "status": status, "data": {"recordings": [{"id": 42, "media_files": media_files}] if recording else []}}


def _producer(transport):
    client = AdminPostMeetingJobClient(
        "http://admin.test", "super-secret", transport=httpx.MockTransport(transport)
    )
    return LocalDiarizationProducer(client)


async def test_disabled_makes_no_admin_request(monkeypatch):
    transport = CapturingTransport()
    monkeypatch.delenv("LOCAL_DIARIZATION_ENABLED", raising=False)

    await _producer(transport).enqueue_completed_recordings(_meeting())

    assert transport.requests == []


async def test_completed_finalized_enqueues_local_diarization_identity_once(monkeypatch):
    transport = CapturingTransport()
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")
    producer = _producer(transport)

    await producer.enqueue_completed_recordings(_meeting())
    await producer.enqueue_completed_recordings(_meeting())

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.url == "http://admin.test/internal/post-meeting-jobs"
    assert request.headers["x-internal-secret"] == "super-secret"
    assert json.loads(request.content) == {
        "kind": "local_diarization", "meeting_id": 7,
        "recording_id": "42", "recording_version": 3,
    }


@pytest.mark.parametrize("meeting", [
    _meeting(status="active"), _meeting(finalized=False), _meeting(recording=False),
])
async def test_missing_or_unfinalized_or_incomplete_never_enqueues(monkeypatch, meeting):
    transport = CapturingTransport()
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")

    await _producer(transport).enqueue_completed_recordings(meeting)

    assert transport.requests == []


async def test_completion_replay_enqueues_finalized_audio_once(monkeypatch):
    transport = CapturingTransport()
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")
    repo = InMemoryMeetingRepo()
    meeting = await repo.create_meeting(
        user_id=7, platform="google_meet", native_meeting_id="meeting", data=_meeting()["data"],
    )
    await repo.create_session(meeting_id=meeting["id"], session_uid="session")
    client = TestClient(create_app(
        meeting_repo=repo, runtime=FakeRuntimeClient(), post_meeting_producer=_producer(transport),
    ))
    for status in ("joining", "active", "completed", "completed"):
        response = client.post(
            "/bots/internal/callback/lifecycle",
            json={"connection_id": "session", "status": status},
        )
        assert response.status_code == 200

    assert len(transport.requests) == 1


async def test_finalization_after_completion_enqueues(monkeypatch):
    transport = CapturingTransport()
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")
    producer = _producer(transport)
    meeting = _meeting(finalized=False)

    await producer.enqueue_completed_recordings(meeting)
    meeting["data"]["recordings"][0]["media_files"][0].update(
        is_final=True, finalized_by="recording_finalizer.master", assembled_chunk_count=3
    )
    await producer.enqueue_completed_recordings(meeting)

    assert len(transport.requests) == 1


async def test_finalization_hook_enqueues_after_completion(monkeypatch):
    transport = CapturingTransport()
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")
    repo = InMemoryRecordingRepo()
    storage = InMemoryStorage()
    repo.seed(meeting_id=7, user_id=7, session_uid="session", status="completed")
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=7, session_uid="session", data=_wav(),
        media_format="wav", is_final=True,
    )
    app = create_app(
        recording_repo=repo, storage=storage, post_meeting_producer=_producer(transport),
    )

    response = TestClient(app).get(
        f"/recordings/{receipt['recording_id']}/master", headers={"x-user-id": "7"},
    )

    assert response.status_code == 200
    assert len(transport.requests) == 1


async def test_finalization_hook_survives_producer_failure(monkeypatch):
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")
    repo = InMemoryRecordingRepo()
    storage = InMemoryStorage()
    repo.seed(meeting_id=7, user_id=7, session_uid="session", status="completed")
    receipt = await upload_chunk(
        repo, storage, token_meeting_id=7, session_uid="session", data=_wav(),
        media_format="wav", is_final=True,
    )

    class Boom:
        async def enqueue_completed_recordings(self, meeting):
            raise RuntimeError("admin down")

    app = create_app(recording_repo=repo, storage=storage, post_meeting_producer=Boom())

    response = TestClient(app).get(
        f"/recordings/{receipt['recording_id']}/master", headers={"x-user-id": "7"},
    )

    assert response.status_code == 200


async def test_admin_failure_is_nonfatal_and_redacts_secret(monkeypatch, capsys):
    transport = CapturingTransport(status_code=503)
    monkeypatch.setenv("LOCAL_DIARIZATION_ENABLED", "true")

    await _producer(transport).enqueue_completed_recordings(_meeting())

    assert len(transport.requests) == 1
    assert "super-secret" not in capsys.readouterr().out
