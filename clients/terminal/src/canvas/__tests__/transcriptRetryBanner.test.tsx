import { describe, expect, it, vi, beforeEach } from "vitest";
import { act } from "react";
import { createRoot } from "react-dom/client";

const lookup = vi.fn();
const preflight = vi.fn();
const retry = vi.fn();
const refresh = vi.fn();
let state: any;

vi.mock("../../surfaces/recordingRetry", () => ({
  fetchAudioRecordingId: (meetingId: number) => lookup(meetingId),
  fetchRetryPreflight: (id: number) => preflight(id),
  retryTranscription: (id: number) => retry(id),
}));
vi.mock("../useMeeting", () => ({
  useMeeting: () => state,
  refreshDurableTranscript: () => refresh(),
}));

import { TranscriptRetryBanner } from "../TranscriptRetryBanner";

function meetingState(recordingId: number | undefined, segments: unknown[] = [], live = false) {
  // The recording id no longer rides the meeting row — the meetings projection omits `recordings`,
  // so the banner looks it up by meeting id. The parameter drives that lookup instead.
  lookup.mockResolvedValue(recordingId);
  return { meeting: { id: "4", title: "m", live }, transcript: { segments } };
}

async function render() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(<TranscriptRetryBanner />); });
  return { container, unmount: () => act(() => root.unmount()) };
}

const button = (c: HTMLElement) => c.querySelector("button") as HTMLButtonElement | null;

describe("TranscriptRetryBanner", () => {
  beforeEach(() => { lookup.mockReset(); preflight.mockReset(); retry.mockReset(); refresh.mockReset(); });

  it("renders nothing when the meeting already has transcript segments", async () => {
    state = meetingState(489531790697, [{ text: "hi" }]);
    preflight.mockResolvedValue({ recording_id: 1, eligible: true });
    const { container, unmount } = await render();
    expect(container.textContent).toBe("");
    expect(preflight).not.toHaveBeenCalled();
    unmount();
  });

  it("renders nothing when the meeting has no recording", async () => {
    state = meetingState(undefined);
    const { container, unmount } = await render();
    expect(container.textContent).toBe("");
    expect(preflight).not.toHaveBeenCalled();
    unmount();
  });

  it("renders nothing while the meeting is still live (the recording isn't final yet)", async () => {
    state = meetingState(77, [], true);
    const { container, unmount } = await render();
    expect(container.textContent).toBe("");
    expect(preflight).not.toHaveBeenCalled();
    unmount();
  });

  it("renders nothing when preflight itself fails — no answer means no claim", async () => {
    state = meetingState(77);
    preflight.mockRejectedValue(new Error("Not Found"));
    const { container, unmount } = await render();
    expect(container.textContent).toBe("");
    unmount();
  });

  it("NOT eligible → the server's reason, no retry button", async () => {
    state = meetingState(77);
    preflight.mockResolvedValue({ recording_id: 77, eligible: false, reason: "Recording audio is not finalized" });
    const { container, unmount } = await render();
    expect(container.textContent).toContain("Recording audio is not finalized");
    expect(button(container)).toBeNull();
    unmount();
  });

  it("eligible → offers the button; click disables it for the whole call, then refreshes", async () => {
    state = meetingState(489531790697);
    preflight.mockResolvedValue({ recording_id: 489531790697, eligible: true, reason: null });
    let finish: (v: unknown) => void = () => {};
    retry.mockReturnValue(new Promise((res) => { finish = res; }));

    const { container, unmount } = await render();
    const b = button(container)!;
    expect(b.textContent).toBe("Retry transcription");
    expect(b.disabled).toBe(false);

    await act(async () => { b.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(retry).toHaveBeenCalledWith(489531790697);
    expect(button(container)!.disabled).toBe(true);

    // A second click while in flight must not fire a second (unguarded, synchronous) retry.
    await act(async () => { button(container)!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(retry).toHaveBeenCalledTimes(1);

    await act(async () => { finish({}); });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(button(container)!.disabled).toBe(false);
    unmount();
  });

  it("failure → shows the presented server message and re-enables the button", async () => {
    state = meetingState(77);
    preflight.mockResolvedValue({ recording_id: 77, eligible: true });
    retry.mockRejectedValue(new Error("Transcription service is unavailable right now."));
    const { container, unmount } = await render();
    await act(async () => { button(container)!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(container.textContent).toContain("Transcription service is unavailable right now.");
    expect(button(container)!.disabled).toBe(false);
    unmount();
  });
});
