import httpx
import pytest

from meeting_api.app import create_app
from meeting_api.collector.fakes import InMemoryTranscriptStore


@pytest.fixture
def store():
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=7, platform="google_meet", native_meeting_id="local",
        segments=[
            {"segment_id": "one", "start": 0, "end": 2, "speaker": "Old"},
            {"segment_id": "two", "start": 2, "end": 4, "speaker": "Old"},
        ],
    )
    return store


async def request(app, *, user="7", rttm="SPEAKER local 1 0 2 <NA> <NA> beta <NA> <NA>\nSPEAKER local 1 2 2 <NA> <NA> alpha <NA> <NA>"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/meetings/google_meet/local/diarization/rttm", headers={"X-User-Id": user}, json={"rttm": rttm})


@pytest.mark.asyncio
async def test_import_is_owner_scoped_deterministic_and_idempotent(store):
    app = create_app(transcript_store=store)
    denied = await request(app, user="8")
    assert denied.status_code == 404

    first = await request(app)
    assert first.json() == {"updated_segments": 2}
    transcript = await store.get_transcript(7, "google_meet", "local")
    assert [segment["speaker"] for segment in transcript["segments"]] == ["Speaker B", "Speaker A"]

    second = await request(app)
    assert second.json() == {"updated_segments": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("rttm, detail", [
    ("SPEAKER broken", "RTTM must contain SPEAKER records with 10 fields"),
    ("SPEAKER local 1 0 nan <NA> <NA> spk0 <NA> <NA>", "RTTM duration must be positive and start non-negative"),
    ("", "RTTM must contain at least one speaker"),
])
async def test_import_rejects_invalid_rttm(store, rttm, detail):
    response = await request(create_app(transcript_store=store), rttm=rttm)
    assert response.status_code == 422
    assert response.json()["detail"] == detail


@pytest.mark.asyncio
async def test_import_labels_more_than_two_speakers(store):
    store.seed_meeting(
        user_id=7, platform="google_meet", native_meeting_id="crowd",
        segments=[{"segment_id": f"s{i}", "start": i, "end": i + 1} for i in range(3)],
    )
    rttm = "\n".join(
        f"SPEAKER local 1 {i} 1 <NA> <NA> spk{i} <NA> <NA>" for i in range(3)
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(transcript_store=store)), base_url="http://test") as client:
        response = await client.post(
            "/meetings/google_meet/crowd/diarization/rttm", headers={"X-User-Id": "7"},
            json={"rttm": rttm},
        )
    assert response.json() == {"updated_segments": 3}
    transcript = await store.get_transcript(7, "google_meet", "crowd")
    assert [s["speaker"] for s in transcript["segments"]] == ["Speaker A", "Speaker B", "Speaker C"]


def test_speaker_labels_extend_past_z():
    from meeting_api.diarization import _speaker_label

    assert [_speaker_label(i) for i in (0, 1, 25, 26, 27, 51, 52)] == [
        "Speaker A", "Speaker B", "Speaker Z", "Speaker AA", "Speaker AB", "Speaker AZ", "Speaker BA",
    ]


@pytest.mark.asyncio
async def test_import_breaks_equal_overlap_ties_by_rttm_speaker_id(store):
    store.seed_meeting(
        user_id=7, platform="google_meet", native_meeting_id="tie",
        segments=[{"segment_id": "tie", "start": 0, "end": 2, "speaker": None}],
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(transcript_store=store)), base_url="http://test") as client:
        response = await client.post(
            "/meetings/google_meet/tie/diarization/rttm", headers={"X-User-Id": "7"},
            json={"rttm": "SPEAKER local 1 0 1 <NA> <NA> beta <NA> <NA>\nSPEAKER local 1 1 1 <NA> <NA> alpha <NA> <NA>"},
        )
        assert response.json() == {"updated_segments": 1}
        repeat = await client.post(
            "/meetings/google_meet/tie/diarization/rttm", headers={"X-User-Id": "7"},
            json={"rttm": "SPEAKER local 1 0 1 <NA> <NA> beta <NA> <NA>\nSPEAKER local 1 1 1 <NA> <NA> alpha <NA> <NA>"},
        )
        assert repeat.json() == {"updated_segments": 0}
    transcript = await store.get_transcript(7, "google_meet", "tie")
    assert transcript["segments"][0]["speaker"] == "Speaker A"
