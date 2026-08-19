#!/usr/bin/env bash
set -euo pipefail

_stdin=$(cat) || _stdin=""
if command -v jq >/dev/null 2>&1; then
  _cwd=$(printf '%s' "$_stdin" | jq -r '.cwd // empty' 2>/dev/null) || _cwd=""
else
  _cwd=$(printf '%s' "$_stdin" | grep -oE '"cwd"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*:[[:space:]]*"//;s/"//') || _cwd=""
fi
[[ -n "$_cwd" ]] || _cwd="$PWD"
# The parent walk below terminates on "/", which a relative path never reaches:
# `dirname .` is `.`, so a relative cwd spins there forever -- a hang `set -e`
# cannot catch.
_cwd=$(cd "$_cwd" 2>/dev/null && pwd) || _cwd="$PWD"

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$HOME/.claude/settings.json"
COMMANDS_DIR="$HOME/.claude/commands"
PLUGIN_JSON="${PLUGIN_ROOT}/.claude-plugin/plugin.json"

# Detect dev mode: plugin.json name contains "vox-dev"
DEV_MODE=false
if grep -q '"vox-dev"' "$PLUGIN_JSON" 2>/dev/null; then
  DEV_MODE=true
fi

if [[ "$DEV_MODE" == "true" ]]; then
  TOOL_GLOB="mcp__plugin_vox-dev_mic__*"
else
  TOOL_GLOB="mcp__plugin_vox_mic__*"
fi

ACTIONS=()

# ── Clean up retired commands ─────────────────────────────────────────
if [[ "$DEV_MODE" == "false" ]]; then
  RETIRED=(say.md speak.md notify.md vox-on.md vox-off.md enable.md disable.md)
  CLEANED=()
  FAILED_CLEAN=0
  for name in "${RETIRED[@]}"; do
    dest="$COMMANDS_DIR/$name"
    # `-f` passing doesn't guarantee $COMMANDS_DIR is writable -- an rm that
    # fails here must not take the rest of the hook down with it under `set -e`.
    if [[ -f "$dest" ]]; then
      if rm "$dest" 2>/dev/null; then
        CLEANED+=("/${name%.md}")
      else
        FAILED_CLEAN=$((FAILED_CLEAN + 1))
      fi
    fi
  done
  if [[ ${#CLEANED[@]} -gt 0 ]]; then
    ACTIONS+=("Cleaned retired commands: ${CLEANED[*]}")
  fi
  if [[ $FAILED_CLEAN -gt 0 ]]; then
    ACTIONS+=("Failed to remove $FAILED_CLEAN retired command(s) from ~/.claude/commands")
  fi
fi

# ── Deploy top-level commands if missing ──────────────────────────────
# In dev mode, skip command deployment — prod plugin handles top-level commands.
# Skip *-dev.md files — dev commands use plugin namespace (vox-dev:say-dev)
if [[ "$DEV_MODE" == "false" ]]; then
  DEPLOYED=()
  FAILED_DEPLOY=0
  # A bare `mkdir -p` here would abort the whole hook under `set -e` if
  # $HOME is unwritable -- same failure mode the panel log below guards
  # against. `mkdir -p` returns 0 for an ALREADY-existing directory
  # regardless of its write permission, so the guard alone doesn't cover
  # an existing-but-now-unwritable $COMMANDS_DIR -- `cp` below is guarded
  # too. Report every failure via ACTIONS (the settings.json block below
  # does the same on its identical failure class) instead of aborting or
  # skipping silently -- the agent's additionalContext is the only
  # channel this hook has to say why commands never showed up. ACTIONS
  # messages are ASCII literals for the no-jq heredoc fallback below, so
  # this reports the fixed path "~/.claude/commands", never $COMMANDS_DIR
  # (which could carry a quote or backslash from an unusual $HOME).
  if mkdir -p "$COMMANDS_DIR" 2>/dev/null; then
    for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
      name="$(basename "$cmd_file")"
      [[ "$name" == *-dev.md ]] && continue
      dest="$COMMANDS_DIR/$name"
      if [[ ! -f "$dest" ]] || ! diff -q "$cmd_file" "$dest" >/dev/null 2>&1; then
        if cp "$cmd_file" "$dest" 2>/dev/null; then
          DEPLOYED+=("/${name%.md}")
        else
          FAILED_DEPLOY=$((FAILED_DEPLOY + 1))
        fi
      fi
    done
  else
    ACTIONS+=("Failed to create ~/.claude/commands — skipping command deployment")
  fi
  if [[ ${#DEPLOYED[@]} -gt 0 ]]; then
    ACTIONS+=("Deployed commands: ${DEPLOYED[*]}")
  fi
  if [[ $FAILED_DEPLOY -gt 0 ]]; then
    ACTIONS+=("Failed to deploy $FAILED_DEPLOY command(s) to ~/.claude/commands")
  fi
fi

# ── Auto-allow MCP tools and skills ───────────────────────────────────
# Every MCP tool and every skill must be auto-approved so users never see
# a permission prompt after enabling the plugin. Uses the PLUGIN_RULES
# array pattern from punt-kit/standards/permissions.md § 6.
#
# Skill names must match deployed commands. When a command is added
# or renamed, update the Skill() list below; scripts/check-skill-permissions.sh
# (wired into `make lint`) enforces parity and catches any drift that
# would otherwise surface as unexplained permission prompts.
if ! command -v jq >/dev/null 2>&1; then
  ACTIONS+=("jq not found, skipping permission setup")
else
  # Remove legacy mcp__plugin_tts_* and mcp__plugin_vox*_vox__* patterns
  if jq -e '.permissions.allow // [] | map(select(test("mcp__plugin_(tts[_-]|vox[^_]*_vox__)"))) | length > 0' "$SETTINGS" >/dev/null 2>&1; then
    TMPFILE="$(mktemp "$SETTINGS.XXXXXX" 2>/dev/null || printf '')"
    if [[ -n "$TMPFILE" ]] && jq '.permissions.allow = [.permissions.allow[] | select(test("mcp__plugin_(tts[_-]|vox[^_]*_vox__)") | not)]' "$SETTINGS" > "$TMPFILE" && mv "$TMPFILE" "$SETTINGS"; then
      ACTIONS+=("Removed legacy MCP permission patterns")
    else
      [[ -n "$TMPFILE" ]] && rm -f "$TMPFILE"
      ACTIONS+=("Failed to remove legacy MCP permission patterns")
    fi
  fi

  # Build PLUGIN_RULES via jq to avoid JSON injection from $TOOL_GLOB
  PLUGIN_RULES=$(jq -n --arg glob "$TOOL_GLOB" \
    '[$glob, "Skill(unmute)", "Skill(mute)", "Skill(recap)", "Skill(vibe)", "Skill(vox)", "Skill(music)", "Skill(model)", "Skill(provider)", "Skill(voice)"]' 2>/dev/null) || {
    ACTIONS+=("jq failed to build permission rules — skipping permission setup")
    PLUGIN_RULES=""
  }

  if [[ -z "$PLUGIN_RULES" ]]; then
    : # jq failed above, already logged
  else
    if [[ ! -f "$SETTINGS" ]]; then
      if mkdir -p "$(dirname "$SETTINGS")" && printf '{}' > "$SETTINGS"; then
        ACTIONS+=("Created ~/.claude/settings.json")
      else
        ACTIONS+=("Failed to create ~/.claude/settings.json — skipping permission setup")
      fi
    fi
  fi

  if [[ -n "$PLUGIN_RULES" ]] && [[ -f "$SETTINGS" ]]; then
    ADDED=$(jq -r --argjson new "$PLUGIN_RULES" '
      (.permissions.allow // []) as $orig
      | [$new[] | select(. as $r | $orig | index($r) | not)] | length
    ' "$SETTINGS" 2>/dev/null) || ADDED=""

    if [[ -z "$ADDED" ]]; then
      ACTIONS+=("Failed to read permissions from settings.json (file may be corrupt)")
    elif [[ "$ADDED" =~ ^[0-9]+$ ]] && [[ "$ADDED" -gt 0 ]]; then
      TMP=$(mktemp "$SETTINGS.XXXXXX" 2>/dev/null) || {
        ACTIONS+=("mktemp failed — skipped permission update")
        TMP=""
      }
      if [[ -n "$TMP" ]] && jq --argjson new "$PLUGIN_RULES" '
        (.permissions.allow // []) as $orig
        | .permissions.allow = $orig + [$new[] | select(. as $r | $orig | index($r) | not)]
      ' "$SETTINGS" > "$TMP" && mv "$TMP" "$SETTINGS"; then
        ACTIONS+=("Auto-allowed $ADDED permission rule(s) in settings.json")
      else
        if [[ -n "$TMP" ]]; then
          rm -f "$TMP"
          ACTIONS+=("Failed to update permissions in settings.json")
        fi
      fi
    fi
  fi
fi

# ── Notify Claude if anything was set up ─────────────────────────────
if [[ ${#ACTIONS[@]} -gt 0 ]]; then
  MSG="Vox plugin first-run setup complete."
  for action in "${ACTIONS[@]}"; do
    MSG="$MSG $action."
  done
  if command -v jq >/dev/null 2>&1; then
    jq -n --arg msg "$MSG" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $msg}}'
  else
    # Fallback: ACTIONS messages are ASCII literals, safe for heredoc
    cat <<ENDJSON
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"$MSG"}}
ENDJSON
  fi
fi

# ── Launch this session's Vox control panel applet ────────────────────
# The panel is a session-bound program that owns the "Vox" entry in the Lux
# menu and services its clicks directly, the same way lux's own lux-beads
# applet does -- voxd cannot do this itself: launchd starts it with no
# repository working directory. Modeled directly on lux-beads' own
# session-start block (a pgrep-guard-then-nohup spawn under a session-pid
# lock), gated additionally on vox's own per-repo enablement marker so an
# unrelated repo never gets a floating "Vox" menu entry.
#
# $PPID is the Claude Code process that ran this hook. The applet watches it
# and exits when it goes, so it never outlives its session. SessionStart
# fires more than once for one session -- /resume and /clear both fire it
# again against the same process -- and the applet refuses a second start
# itself under its own session-pid lock, so the guard below only saves the
# pointless respawn.
_repo_root=$(git -C "$_cwd" rev-parse --show-toplevel 2>/dev/null) || _repo_root=""
if [ -z "$_repo_root" ]; then
  _dir="$_cwd"
  while [ ! -f "$_dir/.punt-labs/vox/enabled" ] && [ "$_dir" != "/" ]; do
    _dir=$(dirname "$_dir")
  done
  _repo_root="$_dir"
fi
if [ -f "$_repo_root/.punt-labs/vox/enabled" ]; then
  # ~/.punt-labs/vox/logs is where every other vox process already logs, and
  # unlike a shared tmp dir it is not writable by other local users -- so a
  # predictable per-session filename cannot be pre-empted by a symlink planted
  # at that path. Creating the directory 0700 matches paths.log_dir().
  PANEL_LOG_DIR="$HOME/.punt-labs/vox/logs"
  mkdir -p "$PANEL_LOG_DIR" 2>/dev/null || true
  # Tighten the whole chain, not just the leaf: `mkdir -m` sets the mode only
  # on the deepest directory and only when it creates it, so a state root left
  # traversable by a permissive umask stays that way. This mirrors
  # private_state.ensure_private_tree() on the Python side.
  chmod 700 "$HOME/.punt-labs" "$HOME/.punt-labs/vox" "$PANEL_LOG_DIR" 2>/dev/null || true
  PANEL_LOG="$PANEL_LOG_DIR/vox-panel-$PPID.log"
  # An unwritable log path must cost this session its log, not its panel. The
  # `>>` redirects below are unguarded, and a failed redirect on a synchronous
  # command aborts under `set -e` -- so without this fallback an unwritable
  # $HOME killed the hook on the very line explaining why the panel was absent.
  touch "$PANEL_LOG" 2>/dev/null || PANEL_LOG=/dev/null
  if ! command -v vox-panel >/dev/null 2>&1; then
    # A log line is the only surface reachable here, deliberately: nothing has
    # connected to voxd or luxd yet at this point in the hook, so there is no
    # daemon to carry this reason into `vox status`/`vox doctor`.
    echo "$(date '+%Y-%m-%d %H:%M:%S') session-start: vox-panel not found on PATH; the Vox control panel will not be available this session" >>"$PANEL_LOG"
  elif pgrep -f "vox-panel --session-pid ${PPID}\$" >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') session-start: this session is already served; not spawning another panel" >>"$PANEL_LOG"
  else
    nohup vox-panel --session-pid "$PPID" >>"$PANEL_LOG" 2>&1 &
    disown
  fi
fi

exit 0
