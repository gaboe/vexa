- **Retry transcription from the meeting canvas.** When a meeting has a retained recording but the
  transcript pane is empty, the terminal now calls the read-only
  `GET /recordings/{id}/transcription/preflight` and — only when it reports the audio eligible —
  offers a **Retry transcription** button that fires
  `POST /recordings/{id}/transcription/retry` and reloads the transcript when it returns. The call is
  synchronous and can take minutes on a long recording, so the button stays disabled for its whole
  duration; when the recording is not eligible the server's own reason is shown instead. See
  [Meetings API](/api/meetings).
