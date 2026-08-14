"use client";
// The recovery affordance for the one failure a user cannot otherwise act on: the bot recorded the
// meeting (audio is finalized and retained) but never produced a transcript, so the pane is empty and
// the only visible control ("Send bot again") would push a bot into a meeting that already ended.
//
// Rendered next to MeetingHealthBanner — ABOVE the transcript pane — rather than inside it, because
// meetingHealth() reports `ok` for any meeting with no live session: reopening the ended meeting shows
// no banner at all, which is exactly the steady state this failure leaves the user in.
//
// Gating is the SERVER's call: preflight (read-only, owner-scoped) decides eligibility; we only offer
// the button when it says `eligible`. The retry itself is synchronous — minutes on a long recording —
// and has no cross-process single-flight, so the button is disabled for the whole call.
import { useEffect, useState } from "react";
import { presentError } from "../surfaces/apiClient";
import { fetchAudioRecordingId, fetchRetryPreflight, retryTranscription } from "../surfaces/recordingRetry";
import { refreshDurableTranscript, useMeeting } from "./useMeeting";

// Same muted/accent pair the health banner uses — informational until something actually fails.
const TONE = {
  offer: { color: "var(--t2)", bg: "var(--panel2)" },
  failed: { color: "var(--accent)", bg: "var(--accentbg)" },
};

type Phase = { kind: "idle" } | { kind: "running" } | { kind: "failed"; message: string } | { kind: "done" };

export function TranscriptRetryBanner() {
  const meeting = useMeeting();
  const meetingId = Number(meeting.meeting.id);
  const empty = (meeting.transcript.segments ?? []).length === 0;
  // A live meeting is still filling its recording: the audio is not final, so a retry is meaningless
  // and preflight would only echo "not finalized" under the health banner's "Waiting for transcript…".
  const idle = Number.isFinite(meetingId) && empty && !meeting.meeting.live;
  const [recordingId, setRecordingId] = useState<number | undefined>(undefined);

  // undefined = preflight has not ANSWERED (in flight, or the call itself failed). "The server says no"
  // and "the server didn't say" are different states and must never share a headline — a failed
  // preflight makes no claim about the recording at all, so the banner stays hidden.
  const [preflight, setPreflight] = useState<{ eligible: boolean; reason?: string } | undefined>(undefined);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  useEffect(() => {
    setPreflight(undefined);
    setPhase({ kind: "idle" });
    setRecordingId(undefined);
    if (!idle) return;
    let cancelled = false;
    void fetchAudioRecordingId(meetingId)
      .then((id) => {
        if (cancelled || id == null) return;   // no recording → nothing a retry could act on
        setRecordingId(id);
        return fetchRetryPreflight(id).then((p) => {
          if (!cancelled) setPreflight({ eligible: !!p.eligible, reason: p.reason ?? undefined });
        });
      })
      .catch((e) => { presentError(e); /* logged; no answer → no banner */ });
    return () => { cancelled = true; };
  }, [meetingId, idle]);

  if (!idle || recordingId == null || !preflight) return null;
  const { eligible, reason } = preflight;

  const running = phase.kind === "running";

  const run = () => {
    if (running) return;   // the route has no single-flight of its own — one in-flight retry at a time
    setPhase({ kind: "running" });
    void retryTranscription(recordingId)
      .then(() => { setPhase({ kind: "done" }); refreshDurableTranscript(); })
      .catch((e) => setPhase({ kind: "failed", message: presentError(e).headline }));
  };

  const tone = phase.kind === "failed" ? TONE.failed : TONE.offer;
  const headline = eligible
    ? "This meeting was recorded, but no transcript was produced."
    : `Transcription can't be retried for this recording${reason ? ` — ${reason}` : ""}.`;

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        display: "flex", flexDirection: "column", gap: 6,
        margin: "8px 18px 0", padding: "8px 11px", borderRadius: 8,
        background: tone.bg, border: `1px solid ${tone.color}`, color: tone.color,
        fontSize: 12.5, lineHeight: 1.4,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontWeight: 600, flex: 1, minWidth: 0 }}>{headline}</span>
        {eligible && (
          <button
            type="button"
            onClick={run}
            disabled={running}
            title={running ? "Re-transcribing the recording — this can take several minutes" : "Send the retained recording to the transcription service again"}
            style={{
              flex: "none", background: "transparent", border: `1px solid ${tone.color}`, color: tone.color,
              borderRadius: 6, padding: "2px 9px", fontSize: 11.5, fontWeight: 600,
              cursor: running ? "progress" : "pointer", opacity: running ? 0.6 : 1,
            }}
          >
            {running ? "Transcribing…" : "Retry transcription"}
          </button>
        )}
      </div>

      {running && <div style={{ fontSize: 11.5, opacity: 0.92 }}>Re-transcribing the whole recording — this can take several minutes. Keep this tab open.</div>}
      {phase.kind === "failed" && <div style={{ fontSize: 11.5, opacity: 0.92 }}>{phase.message}</div>}
      {phase.kind === "done" && <div style={{ fontSize: 11.5, opacity: 0.92 }}>Transcription finished — reloading the transcript.</div>}
    </div>
  );
}
