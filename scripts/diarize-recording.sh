#!/usr/bin/env bash
set -euo pipefail

: "${VEXA_API_BASE:?Set VEXA_API_BASE, for example http://localhost:3951}"
: "${VEXA_API_KEY:?Set VEXA_API_KEY to your Vexa API key}"
: "${VEXA_RECORDING_ID:?Set VEXA_RECORDING_ID to the finalized recording ID}"
: "${VEXA_USER_ID:?Set VEXA_USER_ID to the Vexa user ID that owns the recording}"
: "${VEXA_PLATFORM:?Set VEXA_PLATFORM, for example google_meet}"
: "${VEXA_NATIVE_MEETING_ID:?Set VEXA_NATIVE_MEETING_ID}"
: "${ARGMAX_CLI:=argmax-cli}"

for command in curl ffmpeg jq "$ARGMAX_CLI"; do
	command -v "$command" >/dev/null || {
		echo "Missing required command: $command" >&2
		exit 1
	}
done

workdir=$(mktemp -d -t vexa-diarization.XXXXXX)
trap 'rm -rf "$workdir"' EXIT
headers=(-H "X-API-Key: $VEXA_API_KEY")

recording=$(curl --fail --silent --show-error "${headers[@]}" \
	"$VEXA_API_BASE/recordings/$VEXA_RECORDING_ID")
jq -e --arg user_id "$VEXA_USER_ID" '.user_id == ($user_id | tonumber)' <<<"$recording" >/dev/null || {
	echo "Recording is not owned by VEXA_USER_ID." >&2
	exit 1
}
jq -er '
  .media_files[]
  | select(.type == "audio" and .is_final == true and (.storage_path | type == "string"))
  | .storage_path
' <<<"$recording" >/dev/null || {
	echo "Recording has no finalized audio master." >&2
	exit 1
}

curl --fail --silent --show-error "${headers[@]}" \
	"$VEXA_API_BASE/recordings/$VEXA_RECORDING_ID/master?type=audio" >"$workdir/recording.webm"
ffmpeg -loglevel error -y -i "$workdir/recording.webm" -ac 1 -ar 16000 "$workdir/recording.wav"
"$ARGMAX_CLI" diarize \
	--audio-path "$workdir/recording.wav" \
	--rttm-path "$workdir/speakers.rttm" \
	--use-exclusive-reconciliation

payload=$(jq --rawfile rttm "$workdir/speakers.rttm" -n '{rttm: $rttm}')
curl --fail --silent --show-error --request POST "${headers[@]}" \
	-H 'Content-Type: application/json' \
	--data "$payload" \
	"$VEXA_API_BASE/meetings/$VEXA_PLATFORM/$VEXA_NATIVE_MEETING_ID/diarization/rttm"
printf '\nImported local anonymous speaker labels.\n'
