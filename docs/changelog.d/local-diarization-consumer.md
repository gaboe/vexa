- Local diarization phase 2: a one-shot consumer (`python -m meeting_api.post_meeting.diarize_once`)
  claims a `local_diarization` job from admin-api's lease API, diarizes the recording's finalized
  audio master with the external CLI, and writes anonymous speaker labels onto the transcript —
  the manual `scripts/diarize-recording.sh` pipeline, unattended. Cron or a k8s Job drives cadence;
  the lease provides mutual exclusion. Transient failures (storage, diarizer, admin-api) retry with
  backoff, structural ones (no finalized master, unparsable RTTM) fail permanently, and a lost lease
  abandons the job without writing.
- RTTM import now labels any number of speakers (Speaker A…Z, then AA, AB, …); two-speaker output is
  unchanged.
