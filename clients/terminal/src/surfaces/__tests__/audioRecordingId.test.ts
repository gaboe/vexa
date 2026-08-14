/** Which recording a transcription retry acts on. `data.recordings[]` can hold more than one entry
 *  (ids are minted per media type), and only an AUDIO-bearing one can be re-transcribed — the server's
 *  preflight is still the authority on eligibility, this only picks which id to ask about. */
import { describe, expect, it } from "vitest";
import { audioRecordingId } from "../liveMeetings";

describe("audioRecordingId", () => {
  it("returns undefined with no recordings", () => {
    expect(audioRecordingId(undefined)).toBeUndefined();
    expect(audioRecordingId([])).toBeUndefined();
  });

  it("prefers the newest recording that carries audio", () => {
    expect(audioRecordingId([
      { id: 1, media_files: [{ type: "audio" }] },
      { id: 2, media_files: [{ type: "video" }] },
    ])).toBe(1);
    expect(audioRecordingId([
      { id: 1, media_files: [{ type: "audio" }] },
      { id: 2, media_files: [{ type: "audio" }, { type: "video" }] },
    ])).toBe(2);
  });

  it("falls back to the newest recording when no media_files are projected", () => {
    expect(audioRecordingId([{ id: 1 }, { id: 2 }])).toBe(2);
  });

  it("ignores a non-numeric id", () => {
    expect(audioRecordingId([{ id: undefined }])).toBeUndefined();
  });
});
