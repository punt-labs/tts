#!/usr/bin/env bash
# Format vox MCP tool output for the UI panel.
#
# Two-channel display (see punt-kit/patterns/two-channel-display.md):
#   updatedMCPToolOutput  → compact panel line (♪ prefix, max 80 cols)
#   additionalContext     → full JSON for the model to reference
#
# No `set -euo pipefail` — hooks must degrade gracefully on
# malformed input rather than failing the tool call.

INPUT=$(cat)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name')
TOOL_NAME="${TOOL##*__}"
# The single `music` tool routes on its subcommand argument (on/off/play/next/
# list/status/new/get/remove); read it from the tool input to pick the panel form.
SUBCOMMAND=$(printf '%s' "$INPUT" | jq -r '.tool_input.subcommand // empty')
RESULT=$(printf '%s' "$INPUT" | jq -r '
  def unpack: if type == "string" then (fromjson? // .) else . end;
  if (.tool_response | type) == "array" then
    (.tool_response[0].text // "" | unpack)
  else
    (.tool_response | unpack)
  end
  | if type == "object" and has("result") then (.result | unpack) else . end
')

# Extract voice name from single-result JSON.
# Falls back to "the voice" when missing or empty.
extract_voice() {
  local data="$1"
  VOICE=$(printf '%s' "$data" | jq -r '.[0].voice // .voice // empty' 2>/dev/null)
  [[ -z "$VOICE" ]] && VOICE="the voice"
}

# Infer possessive pronoun for a voice name.
voice_pronoun() {
  local name
  name=$(echo "$1" | tr '[:upper:]' '[:lower:]')
  case "$name" in
    matilda|joanna|jessica|rachel|emily|aria|lily|charlotte|alice|domi|elli|freya|gigi|serena|glinda|mimi|nicole)
      echo "her" ;;
    adam|josh|sam|matthew|daniel|james|clyde|ethan|fin|harry|liam|patrick|thomas|michael|george|callum)
      echo "his" ;;
    *)
      echo "their" ;;
  esac
}

# Pick a random element from positional arguments (Bash 3.2 compatible).
pick_random() {
  local idx=$((RANDOM % $#))
  shift "$idx"
  echo "$1"
}

# First line of a tool's ♪-prefixed "message" field, or empty when absent.
# The music tools author their own concise panel line; the panel shows it
# verbatim (first line only, to stay within the compact channel).
message_line() {
  printf '%s' "$1" | jq -r 'if type == "object" and has("message")
    then (.message | split("\n")[0]) else empty end' 2>/dev/null
}

# $ctx carries the full tool RESULT, which for rec_get includes an arbitrarily
# large base64 recording. Feed it to jq via --rawfile from a process
# substitution, never --arg: an --arg value lands in jq's argv and a payload
# past ARG_MAX would make the whole hook fail exec, delivering neither the panel
# line nor the context. Read from a pipe and no size limit applies.
emit() {
  local summary="$1" ctx="$2"
  jq -n --arg summary "$summary" --rawfile ctx <(printf '%s' "$ctx") '{
    hookSpecificOutput: {
      hookEventName: "PostToolUse",
      updatedMCPToolOutput: $summary,
      additionalContext: $ctx
    }
  }'
}

# Music control (music/music_play/music_next) and vibe are pure
# fire-and-forget: EVERY slash-command flow that drives them documents "no text
# output — the panel confirms," so the panel line above is the entire response.
# Handing the agent the result JSON reads as "here is data, describe it" and
# provokes redundant narration. On success their additionalContext carries this
# terminal stop-narration directive instead.
#
# The other mic tools each back at least one flow that REQUIRES an agent reply
# derived from the payload — record returns saved file paths, unmute drives
# /vox model|provider ("Switched … to X"), speak drives /mute (a phrase reply),
# notify drives /vox c (lists featured voices). Those keep the RESULT, exactly
# like the query tools (status/who/music_list), so the flow has the data.
STOP_NARRATION="The audio panel has already shown this to the user. This tool call is the complete response — reply with no text, no summary, no narration. Stop."

# Error guard: if the result contains an error field, surface it directly.
ERROR_MSG=$(printf '%s' "$RESULT" | jq -r '.error // empty' 2>/dev/null)
if [[ -n "$ERROR_MSG" ]]; then
  emit "♪ error: ${ERROR_MSG}" "$RESULT"
  exit 0
fi

# Every legitimate success payload is a JSON object (control tools) or array
# (record/unmute). Anything else non-empty is a FastMCP uncaught-exception error
# string (e.g. "Error executing tool music: KeyError: 'style'"), which is bare
# text — not our {"error":...} contract. Surface it; never let a success branch
# turn it into a stay-silent directive.
if [[ -n "$RESULT" ]] && ! printf '%s' "$RESULT" | jq -e 'type == "object" or type == "array"' >/dev/null 2>&1; then
  emit "♪ error" "$RESULT"
  exit 0
fi

if [[ "$TOOL_NAME" == "unmute" ]]; then
  extract_voice "$RESULT"
  PRONOUN=$(voice_pronoun "$VOICE")
  PHRASES=(
    "♪ ${VOICE} has spoken"
    "♪ ${VOICE} said ${PRONOUN} piece"
    "♪ ${VOICE} delivered"
    "♪ heard from ${VOICE}"
  )
  emit "$(pick_random "${PHRASES[@]}")" "$RESULT"
  exit 0
fi

# The single `rec` tool routes on its subcommand. Every rec verb keeps its
# RESULT for the agent to act on -- a bare store id, a base64 clip -- so none is
# a silent control action. rec get names the id and size only, NEVER the base64
# blob, so the compact channel is not flooded with an inline recording.
if [[ "$TOOL_NAME" == "rec" ]]; then
  case "$SUBCOMMAND" in
    new)
      # rec new returns a LIST of {id,bytes,cached}, one per synthesized segment.
      COUNT=$(printf '%s' "$RESULT" | jq -r 'length' 2>/dev/null || echo "?")
      extract_voice "$RESULT"
      PHRASES=(
        "♪ ${VOICE} recorded ${COUNT} track(s)"
        "♪ ${COUNT} track(s) saved"
      )
      emit "$(pick_random "${PHRASES[@]}")" "$RESULT"
      ;;
    list)
      COUNT=$(printf '%s' "$RESULT" | jq -r '.recordings | length' 2>/dev/null || echo "?")
      emit "♪ ${COUNT} recording(s) in the store" "$RESULT"
      ;;
    play)
      REF=$(printf '%s' "$RESULT" | jq -r '.played // "the recording"' 2>/dev/null)
      emit "♪ playing ${REF}" "$RESULT"
      ;;
    get)
      ID=$(printf '%s' "$RESULT" | jq -r '.id // "the recording"' 2>/dev/null)
      BYTES=$(printf '%s' "$RESULT" | jq -r '.bytes // "?"' 2>/dev/null)
      emit "♪ fetched ${ID} (${BYTES} bytes)" "$RESULT"
      ;;
    remove)
      REF=$(printf '%s' "$RESULT" | jq -r '.removed // "the recording"' 2>/dev/null)
      emit "♪ removed ${REF}" "$RESULT"
      ;;
    *)
      emit "$(message_line "$RESULT")" "$RESULT"
      ;;
  esac
  exit 0
fi

if [[ "$TOOL_NAME" == "vibe" ]]; then
  VIBE_DATA=$(printf '%s' "$RESULT" | jq -r '.vibe // empty' 2>/dev/null)
  if [[ -n "$VIBE_DATA" ]]; then
    VIBE_TAGS=$(printf '%s' "$VIBE_DATA" | jq -r '.vibe_tags // empty' 2>/dev/null)
    if [[ -n "$VIBE_TAGS" ]]; then
      MSG="♪ vibe shifted to ${VIBE_TAGS}"
    else
      MOOD=$(printf '%s' "$VIBE_DATA" | jq -r '.vibe // empty' 2>/dev/null)
      if [[ -n "$MOOD" ]]; then
        MSG="♪ mood: ${MOOD}"
      else
        MSG="♪ vibe updated"
      fi
    fi
  else
    MSG="♪ vibe updated"
  fi
  # When a Program plays, the vibe reply carries an imperative music_hint: an
  # instruction to author 12 fresh prompts and re-pool the music to the new mood.
  # That directive IS a terminal action, so hand it to the agent as
  # additionalContext in place of the stop-narration directive -- otherwise the
  # STOP_NARRATION silent-set membership swallows it and the re-pool never fires.
  # Music off: no hint, so the vibe change stays silent.
  MUSIC_HINT=$(printf '%s' "$RESULT" | jq -r '.music_hint // empty' 2>/dev/null)
  if [[ -n "$MUSIC_HINT" ]]; then
    emit "$MSG" "$MUSIC_HINT"
  else
    emit "$MSG" "$STOP_NARRATION"
  fi
  exit 0
fi

if [[ "$TOOL_NAME" == "notify" ]]; then
  MODE=$(printf '%s' "$RESULT" | jq -r '.notify.notify // empty' 2>/dev/null)
  case "$MODE" in
    y) MSG="♪ vox enabled" ;;
    n) MSG="♪ vox disabled" ;;
    c) MSG="♪ continuous mode on" ;;
    *) MSG="♪ notify updated" ;;
  esac
  emit "$MSG" "$RESULT"
  exit 0
fi

if [[ "$TOOL_NAME" == "speak" ]]; then
  MODE=$(printf '%s' "$RESULT" | jq -r '.speak // empty' 2>/dev/null)
  VOICE=$(printf '%s' "$RESULT" | jq -r '.voice // empty' 2>/dev/null)
  case "$MODE" in
    y)
      if [[ -n "$VOICE" ]]; then
        MSG="♪ ${VOICE} at the mic"
      else
        MSG="♪ voice on"
      fi
      ;;
    n) MSG="♪ chimes only" ;;
    *) MSG="♪ speak updated" ;;
  esac
  emit "$MSG" "$RESULT"
  exit 0
fi

if [[ "$TOOL_NAME" == "status" ]]; then
  VOICE=$(printf '%s' "$RESULT" | jq -r '.voice // "unknown"' 2>/dev/null)
  NOTIFY=$(printf '%s' "$RESULT" | jq -r '.notify // "?"' 2>/dev/null)
  emit "♪ ${VOICE} · notify=${NOTIFY}" "$RESULT"
  exit 0
fi

# The single `music` tool routes on its subcommand. The playback verbs
# (on/off/play/next) are fire-and-forget: they author their own DJ-flavored ♪
# .message line server-side (see MusicMarquee) and get the terminal
# stop-narration directive, so the panel is the whole response. The query/catalog
# verbs (list/status/new/get/remove) keep the RESULT for the agent to act on -- a
# saved album id, an export path, the program state -- exactly as the old
# separate tools did.
if [[ "$TOOL_NAME" == "music" ]]; then
  case "$SUBCOMMAND" in
    status)
      # A query, not a control action: the agent reports what is playing, so it
      # must get the program block back rather than the stay-silent directive.
      emit "$(message_line "$RESULT")" "$RESULT"
      ;;
    list)
      COUNT=$(printf '%s' "$RESULT" | jq -r '.programs | length' 2>/dev/null || echo "?")
      PHRASES=("♪ ${COUNT} album(s) in the crate" "♪ your crate: ${COUNT} album(s)")
      emit "$(pick_random "${PHRASES[@]}")" "$RESULT"
      ;;
    new)
      ALBUM=$(printf '%s' "$RESULT" | jq -r '.album_id // "a new album"' 2>/dev/null)
      emit "♪ generated album ${ALBUM}" "$RESULT"
      ;;
    get)
      ALBUM=$(printf '%s' "$RESULT" | jq -r '.album_id // "the album"' 2>/dev/null)
      emit "♪ exported ${ALBUM}" "$RESULT"
      ;;
    remove)
      ALBUM=$(printf '%s' "$RESULT" | jq -r '.removed // "the album"' 2>/dev/null)
      emit "♪ removed album ${ALBUM}" "$RESULT"
      ;;
    *)
      # on/off/play/next: control actions produce NO agent text.
      emit "$(message_line "$RESULT")" "$STOP_NARRATION"
      ;;
  esac
  exit 0
fi

# Generic message fallback: if the result has a "message" field, use it.
# Safety net for any tool that returns human-readable text but doesn't
# have a dedicated formatter above.
MSG_FIELD=$(printf '%s' "$RESULT" | jq -r '.message // empty' 2>/dev/null)
if [[ -n "$MSG_FIELD" ]]; then
  emit "$MSG_FIELD" "$RESULT"
  exit 0
fi

# Fallback: full output in panel. --rawfile from a pipe, not --arg, so a large
# unrecognized result cannot overflow ARG_MAX and fail the hook (see emit).
jq -n --rawfile r <(printf '%s' "$RESULT") '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    updatedMCPToolOutput: $r
  }
}'
