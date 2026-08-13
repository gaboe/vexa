from admin_api.schema.models import PostMeetingJob


def test_post_meeting_job_model_constructs_identity_and_claim_indexes():
    table = PostMeetingJob.__table__

    assert table.name == "post_meeting_jobs"
    assert {column.name for column in table.columns} >= {
        "id", "kind", "meeting_id", "recording_id", "recording_version",
        "status", "attempts", "lease_owner", "lease_expires_at",
    }
    assert {constraint.name for constraint in table.constraints} >= {"uq_post_meeting_job_identity"}
    assert {index.name for index in table.indexes} >= {"ix_post_meeting_jobs_claim"}
