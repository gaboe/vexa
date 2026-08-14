"""The phase-2 CONSUMER (``meeting_api.post_meeting.diarize_once``) driven fully offline: a
MockTransport standing in for admin-api's lease API, the in-memory recordings + transcript fakes,
and an injected diarizer so no test ever shells out to ffmpeg/argmax-cli."""
import json

import httpx
import pytest

from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.post_meeting.diarize_once import AdminLeaseClient, run_once
from meeting_api.recordings.fakes import InMemoryRecordingRepo, InMemoryStorage

RTTM = "SPEAKER local 1 0 1 <NA> <NA> a <NA> <NA>\nSPEAKER local 1 1 1 <NA> <NA> b <NA> <NA>\n"
JOB = {"id": 5, "kind": "local_diarization", "meeting_id": 7, "recording_id": "42",
       "recording_version": 3, "status": "running", "attempts": 1, "max_attempts": 5}


class LeaseTransport:
    """Records every lease call; answers claim/renew/complete/fail with scripted status codes."""

    def __init__(self, *, claim=200, renew=200, complete=200, fail=200, job=None):
        self.calls: list[tuple[str, dict]] = []
        self.codes = {"claim": claim, "renew": renew, "complete": complete, "fail": fail}
        self.job = JOB if job is None else job

    def paths(self):
        return [path for path, _ in self.calls]

    async def __call__(self, request):
        leg = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.url.path, body))
        assert request.headers["x-post-meeting-worker-key"] == "worker-secret"
        code = self.codes[leg]
        if code == 204:
            return httpx.Response(204, request=request)
        if code >= 300:
            return httpx.Response(code, request=request, json={"detail": "nope"})
        if leg == "claim":
            return httpx.Response(200, request=request, json={"job": self.job, "lease_token": "tok"})
        return httpx.Response(200, request=request, json={"job": self.job})


def _client(transport):
    return AdminLeaseClient("http://admin.test", "worker-secret",
                            transport=httpx.MockTransport(transport))


def _deps(*, master=True, version=3, blob=True, segments=None):
    repo = InMemoryRecordingRepo()
    repo.seed(meeting_id=7, user_id=7, session_uid="session", status="completed")
    media = [{
        "type": "audio", "is_final": True, "storage_path": "recordings/42/master.webm",
        "finalized_by": "recording_finalizer.master" if master else None,
        "assembled_chunk_count": version,
    }]
    repo._meetings[7]["recordings"] = [{"id": 42, "media_files": media}]
    storage = InMemoryStorage()
    if blob:
        storage.blobs["recordings/42/master.webm"] = b"audio"
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=7, platform="google_meet", native_meeting_id="abc-def", meeting_id=7,
        segments=segments if segments is not None else [
            {"segment_id": "s1", "start": 0.0, "end": 0.9, "text": "hi"},
            {"segment_id": "s2", "start": 1.1, "end": 1.9, "text": "yo"},
        ],
    )
    return repo, storage, store


async def _run(transport, *, diarizer=None, **kwargs):
    repo, storage, store = _deps(**kwargs)

    async def fake_diarizer(audio):
        assert audio == b"audio"
        return RTTM

    code = await run_once(client=_client(transport), recording_repo=repo, storage=storage,
                          transcript_store=store, diarizer=diarizer or fake_diarizer)
    return code, store


async def test_claim_run_complete_labels_segments():
    transport = LeaseTransport()

    code, store = await _run(transport)

    assert code == 0
    assert transport.paths() == [
        "/internal/post-meeting-jobs/claim",
        "/internal/post-meeting-jobs/5/renew",
        "/internal/post-meeting-jobs/5/complete",
    ]
    assert json.loads(json.dumps(transport.calls[0][1])) == {
        "kind": "local_diarization", "lease_seconds": 900,
        "worker_id": transport.calls[0][1]["worker_id"],
    }
    assert transport.calls[1][1]["lease_token"] == "tok"
    transcript = await store.get_transcript_by_id(7, 7)
    assert [s.get("speaker") for s in transcript["segments"]] == ["Speaker A", "Speaker B"]


async def test_nothing_to_claim_is_a_clean_no_op():
    transport = LeaseTransport(claim=204)

    code, store = await _run(transport)

    assert code == 0
    assert transport.paths() == ["/internal/post-meeting-jobs/claim"]
    transcript = await store.get_transcript_by_id(7, 7)
    assert [s.get("speaker") for s in transcript["segments"]] == [None, None]


async def test_storage_read_failure_is_retryable():
    transport = LeaseTransport()

    code, _ = await _run(transport, blob=False)

    assert code == 1
    assert transport.paths()[-1] == "/internal/post-meeting-jobs/5/fail"
    assert transport.calls[-1][1] == {"lease_token": "tok", "retryable": True}


async def test_diarizer_crash_is_retryable():
    transport = LeaseTransport()

    async def boom(audio):
        from meeting_api.post_meeting.diarize_once import Transient
        raise Transient("argmax-cli exited 1")

    code, _ = await _run(transport, diarizer=boom)

    assert code == 1
    assert transport.calls[-1][1] == {"lease_token": "tok", "retryable": True}


@pytest.mark.parametrize("kwargs", [
    {"master": False},   # never finalized by the master finalizer
    {"version": 4},      # a newer master exists — this job's identity is stale
])
async def test_structural_input_is_permanent(kwargs):
    transport = LeaseTransport()

    code, _ = await _run(transport, **kwargs)

    assert code == 1
    assert transport.paths()[-1] == "/internal/post-meeting-jobs/5/fail"
    assert transport.calls[-1][1] == {"lease_token": "tok", "retryable": False}


async def test_unparsable_rttm_is_permanent():
    transport = LeaseTransport()

    async def garbage(audio):
        return "not an rttm line at all\n"

    code, _ = await _run(transport, diarizer=garbage)

    assert code == 1
    assert transport.calls[-1][1] == {"lease_token": "tok", "retryable": False}


async def test_stale_lease_writes_nothing_and_never_reports_failure():
    transport = LeaseTransport(renew=409)

    code, store = await _run(transport)

    assert code == 1
    # Renewal is the gate before the only write: 409 ⇒ abandon, no upsert, no fail() call.
    assert transport.paths() == [
        "/internal/post-meeting-jobs/claim", "/internal/post-meeting-jobs/5/renew",
    ]
    transcript = await store.get_transcript_by_id(7, 7)
    assert [s.get("speaker") for s in transcript["segments"]] == [None, None]


async def test_admin_unreachable_on_claim_exits_nonzero(capsys):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    repo, storage, store = _deps()

    async def unused(audio):  # pragma: no cover — never reached
        raise AssertionError("must not diarize")

    code = await run_once(client=_client(refuse), recording_repo=repo, storage=storage,
                          transcript_store=store, diarizer=unused)

    assert code == 1
    assert "worker-secret" not in capsys.readouterr().out
