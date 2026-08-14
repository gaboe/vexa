/**
 * recording-codec — the recording.v1 MASTER codec (Node): build a final media
 * file from recording.v1 chunks. Parallels @vexa/capture-codec (the capture.v1
 * wire codec); this is the recording.v1 master codec.
 *
 * PARALLEL PATH (keep in sync). The Python twin is the pure-core module
 * `services/meeting-api/meeting_api/recording_codec.py` (NOT the finalizer — that's
 * the IO/orchestration around this). Same scope, two languages, names aligned and
 * pinned to the shared golden vectors (src/contracts/golden/):
 *      buildRecordingMaster ↔  _build_recording_master   (format dispatch)
 *      buildWebmMaster      ↔  _build_webm_master         (WebM Cluster byte-concat)
 *      buildWavMaster       ↔  _build_wav_master          (WAV RIFF header-merge)
 *      parseWavHeader       ↔  _parse_wav_header
 * Change the {webm,wav}-master logic here → mirror it there; the golden tests
 * (golden.test.ts here, test_recording_golden.py there) fail on any drift.
 * (Used by the all-Node desktop, which has no meeting-api; prod assembles via the
 * Python twin — same pattern as the desktop's node:sqlite store.)
 *
 * Two strategies, dispatched on format (the wire's recording.v1 `format`):
 *  - webm: BYTE-CONCAT in seq order. The MediaRecorder stream emits a self-
 *    describing chunk 0 (EBML + Segment + first Cluster) then Cluster-only
 *    chunks; stacking the Clusters inside the Segment yields a valid container.
 *    (ffmpeg's concat demuxer would drop the Cluster-only inputs — so NOT ffmpeg.)
 *  - wav: RIFF-aware merge — strip each chunk's 44-byte header, sum the PCM
 *    payloads, prepend one corrected master header (fmt copied from chunk 0).
 */

const WAV_HEADER_BYTES = 44;

/** Walk the RIFF chunk list (id(4) + size(4) + body, odd bodies padded to even) for `fmt ` and
 *  `data`. A standard WAV carries chunks between them — ffmpeg writes LIST/INFO by default — so
 *  `data` is located, never assumed at offset 36. `fmtChunk` is the 16-byte PCM fmt body copied
 *  verbatim into the master; a longer body (WAVE_FORMAT_EXTENSIBLE) is truncated to that prefix.
 *  The payload is the data body by its declared size, falling back to the rest of the buffer when
 *  that size is 0 or overruns — a streaming writer stamps a placeholder and appends. */
function parseWavHeader(buf: Buffer): { fmtChunk: Buffer; payload: Buffer } {
  if (buf.length < WAV_HEADER_BYTES) throw new Error("WAV chunk shorter than the 44-byte header");
  if (buf.toString("ascii", 0, 4) !== "RIFF" || buf.toString("ascii", 8, 12) !== "WAVE")
    throw new Error("WAV chunk missing RIFF/WAVE magic");

  let fmtChunk: Buffer | null = null;
  let pos = 12;
  while (pos + 8 <= buf.length) {
    const id = buf.toString("ascii", pos, pos + 4);
    const size = buf.readUInt32LE(pos + 4);
    const bodyAt = pos + 8;
    if (id === "fmt ") {
      if (bodyAt + 16 > buf.length) throw new Error("WAV 'fmt ' chunk runs past the end of the buffer");
      fmtChunk = buf.subarray(bodyAt, bodyAt + 16);
    } else if (id === "data") {
      if (fmtChunk === null) throw new Error("WAV chunk has 'data' before any 'fmt ' chunk");
      const end = size > 0 && size <= buf.length - bodyAt ? bodyAt + size : buf.length;
      return { fmtChunk, payload: buf.subarray(bodyAt, end) };
    }
    if (size > buf.length - bodyAt) break; // a size running past the buffer — nothing further parses
    pos = bodyAt + size + (size & 1);
  }
  throw new Error(`WAV chunk has no parsable 'data' chunk${fmtChunk === null ? " and no 'fmt ' chunk" : ""}`);
}

/** RIFF-aware merge (mirrors PulseAudioCapture._wrapWav). fmt is copied from the
 *  first chunk; all chunks must declare the same fmt (mismatch → throw). */
function buildWavMaster(chunks: Buffer[]): Buffer {
  const real = chunks.filter((c) => c.length >= WAV_HEADER_BYTES); // skip the empty final chunk
  if (real.length === 0) throw new Error("buildWavMaster requires at least one non-empty chunk");
  const { fmtChunk } = parseWavHeader(real[0]);
  const payloads: Buffer[] = [];
  real.forEach((c, i) => {
    const { fmtChunk: f, payload } = parseWavHeader(c);
    if (!f.equals(fmtChunk)) throw new Error(`WAV fmt chunk mismatch at chunk index ${i}`);
    payloads.push(payload);
  });
  const totalData = payloads.reduce((n, p) => n + p.length, 0);
  const header = Buffer.alloc(WAV_HEADER_BYTES);
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + totalData, 4);
  header.write("WAVE", 8, "ascii");
  header.write("fmt ", 12, "ascii");
  header.writeUInt32LE(16, 16);
  fmtChunk.copy(header, 20);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(totalData, 40);
  return Buffer.concat([header, ...payloads]);
}

/** Byte-concat in seq order. The empty final chunk concatenates as a no-op. */
function buildWebmMaster(chunks: Buffer[]): Buffer {
  if (chunks.length === 0) throw new Error("buildWebmMaster requires at least one chunk");
  return Buffer.concat(chunks);
}

/**
 * Assemble `chunks` (ALREADY ordered by chunk_seq) into a single master media
 * buffer. The host writes the result to `master.<format>`.
 *
 * Note: webm output is a plain byte-concat — playable, but carries no top-level
 * duration metadata (meeting-api optionally injects it via ffmpeg; the desktop
 * keeps it dependency-free). Seeking is approximate; playback is correct.
 */
export function buildRecordingMaster(format: "webm" | "wav", chunks: Buffer[]): Buffer {
  return format === "wav" ? buildWavMaster(chunks) : buildWebmMaster(chunks);
}
