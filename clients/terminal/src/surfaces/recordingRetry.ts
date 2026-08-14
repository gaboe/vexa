/** Transcription retry — the two owner-scoped recordings routes the terminal calls when a meeting
 *  has a recording but produced no transcript (the bot recorded, but never reached the STT service).
 *
 *  Both go through the ONE catch-all proxy (`/api/recordings/...` → gateway ROOT → meeting-api), so
 *  they carry the same per-user X-API-Key every other meeting call carries; the gateway resolves it
 *  to the X-User-Id the routes scope by. No new auth path.
 *
 *  `preflight` is read-only and cheap — always call it first and only offer the retry when it says
 *  `eligible`. `retry` is SYNCHRONOUS and rebuilds + re-transcribes the whole master, so it can take
 *  minutes on a long recording, and it has no cross-process single-flight: the caller MUST keep the
 *  control disabled for the whole call so one user can't stack overlapping retries. */
import { getJson } from "./apiClient";

export interface RetryPreflight {
  recording_id: number;
  meeting_id?: number;
  eligible: boolean;
  reason?: string | null;
  audio?: { is_final?: boolean; chunk_count?: number; storage_path?: string | null };
}

export function fetchRetryPreflight(recordingId: number): Promise<RetryPreflight> {
  return getJson<RetryPreflight>(`/api/recordings/${recordingId}/transcription/preflight`);
}

interface RecordingRow {
  id: number;
  meeting_id?: number;
  created_at?: string;
  media_files?: { type?: string }[];
}

/** The audio-bearing recording of one meeting, or undefined when the meeting has none.
 *
 *  Sourced from `GET /recordings` rather than the meeting row: the meetings projection carries the
 *  meeting's `data` WITHOUT its `recordings` array, so a meeting payload can never answer this. The
 *  newest audio-bearing recording wins — a re-sent bot appends another recording to the same meeting,
 *  and the last one is the one a user looking at an empty transcript means. */
export async function fetchAudioRecordingId(meetingId: number): Promise<number | undefined> {
  const { recordings } = await getJson<{ recordings?: RecordingRow[] }>("/api/recordings");
  const mine = (recordings ?? []).filter(
    (r) => r.meeting_id === meetingId && (r.media_files ?? []).some((f) => f.type === "audio"),
  );
  if (mine.length === 0) return undefined;
  return mine.sort((a, b) => String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")))[mine.length - 1].id;
}

/** Fire the retry. Resolves when the transcription finished and the segments were upserted
 *  (deterministic `recording-retry:{id}:{index}` ids — repeating updates the same rows). */
export function retryTranscription(recordingId: number): Promise<unknown> {
  return getJson(`/api/recordings/${recordingId}/transcription/retry`, { method: "POST" });
}
