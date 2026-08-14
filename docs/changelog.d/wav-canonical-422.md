- **Recording uploads accept ordinary WAV files, and unparsable ones fail as 422 not 500.** The WAV
  master codec now walks the RIFF chunk list to find `data` instead of demanding it at offset 36, so
  a file `ffmpeg` writes by default (it adds a `LIST`/`INFO` chunk) uploads and plays back end to
  end. Bytes that genuinely aren't a parsable WAV are rejected at upload with `422 Unprocessable
  Entity` — they never enter the meeting record — and any already-stored chunk that can't be
  assembled now surfaces as 422 on `GET /recordings/{id}/master` instead of a 500.
