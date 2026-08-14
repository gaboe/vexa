# MIGRATION-0005 — add `post_meeting_jobs`

`ensure_schema()` creates `post_meeting_jobs` additively. Its unique identity is
`(kind, meeting_id, recording_id, recording_version)`. The table stores post-meeting job
status, attempts, and a bounded lease. Lease tokens are stored only as SHA-256 hashes; the active
hash remains with a successful job so matching duplicate completion is a no-op. No existing data
requires backfill.

Retry budget and backoff live on the row: `max_attempts` (`integer NOT NULL DEFAULT 5`) bounds how
many times a job may be claimed, and `next_attempt_at` (`timestamptz NULL`) holds the earliest time
a retryable failure may be claimed again — exponential in the attempts already spent (30s base,
doubling, capped at 3600s). A retryable failure whose `attempts` has reached `max_attempts` becomes
`permanent_failed` instead. Claiming requires `attempts < max_attempts` and
`next_attempt_at IS NULL OR next_attempt_at <= now`, so an always-failing job backs off instead of
starving its queue, and it clears `next_attempt_at` as it takes the lease. The ceiling also bounds
crash-loop reclaim: a job whose worker died on its last attempt keeps `status = 'leased'` with an
expired `lease_expires_at` and is never claimed again — a sweeper flipping those rows to
`permanent_failed` is the upgrade path.

The claim index is `ix_post_meeting_jobs_claim (kind, status, lease_expires_at)` — `kind` leads
because the claim query filters it first.
