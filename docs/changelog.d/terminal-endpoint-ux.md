- Terminal: custom endpoint base URLs now accept a trailing `/v1` (the shape vendor docs give you)
  — the wizard and Settings → Models normalize it before the backend appends its own API path, so
  both shapes reach the same endpoint instead of 404ing. Settings → Models also stops echoing the
  transcription token and model API key back in plaintext; both render masked, as the wizard does.
