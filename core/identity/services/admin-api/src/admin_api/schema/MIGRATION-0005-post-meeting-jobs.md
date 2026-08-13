# MIGRATION-0005 — add `post_meeting_jobs`

`ensure_schema()` creates `post_meeting_jobs` additively. Its unique identity is
`(kind, meeting_id, recording_id, recording_version)`. The table stores post-meeting job
status, attempts, and a bounded lease. Lease tokens are stored only as SHA-256 hashes; the active
hash remains with a successful job so matching duplicate completion is a no-op. No existing data
requires backfill.
