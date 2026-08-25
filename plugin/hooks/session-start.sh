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

ACTIONS=()

# Dev vs prod comes from the manifest: a "vox-dev" name means this is a working
# tree loaded with `claude --plugin-dir plugin`, and the permission glob, the
# retired-command cleanup, and the command deployment all branch on it.
#
# There are THREE answers, not two. An absent or unreadable manifest used to
# fall through to prod, which is the worst available guess: prod deploys
# commands out of $PLUGIN_ROOT and writes the prod tool glob, so a PLUGIN_ROOT
# that is not actually the plugin failed OPEN -- taking the more invasive
# branch on the strength of a file it never managed to read. `unknown` refuses
# to guess: it reports the reason through ACTIONS (this hook's only channel to
# the agent) and skips every branch that needs the answer. The panel block at
# the end reads no mode, so an unreadable manifest costs the session its
# commands and permissions, not its panel. Exit stays 0 either way -- a
# SessionStart hook must not take the session down with it.
PLUGIN_MODE=unknown
if [[ ! -f "$PLUGIN_JSON" ]]; then
  ACTIONS+=("No plugin manifest at .claude-plugin/plugin.json under the plugin root - cannot tell dev from prod, so skipped command deployment and permission setup")
else
  # No 2>/dev/null here, deliberately. grep exits 1 for "no match" -- a genuine
  # prod manifest -- and 2 for a read error, and collapsing those two into
  # "false" is exactly what let an unreadable-but-present manifest read as
  # prod. Its stderr is also the only place the underlying reason surfaces.
  _grep_status=0
  grep -q '"vox-dev"' "$PLUGIN_JSON" || _grep_status=$?
  case "$_grep_status" in
    0) PLUGIN_MODE=dev ;;
    1) PLUGIN_MODE=prod ;;
    *) ACTIONS+=("Could not read the plugin manifest at .claude-plugin/plugin.json (grep exit $_grep_status) - cannot tell dev from prod, so skipped command deployment and permission setup") ;;
  esac
fi

if [[ "$PLUGIN_MODE" == "dev" ]]; then
  TOOL_GLOB="mcp__plugin_vox-dev_mic__*"
elif [[ "$PLUGIN_MODE" == "prod" ]]; then
  TOOL_GLOB="mcp__plugin_vox_mic__*"
fi

# NAMESPACED_ONLY files — model/provider/voice/recap are deliberately
# namespaced-only (/vox:model, not /model), because a bare top-level command
# claims a name Claude Code itself may use (model already collides; provider
# and voice are the same class of risk). recap has no known collision but is
# namespaced by operator ruling for consistency with the other three. See
# docs/vox-ovz3-command-namespace.md. Declared once, above both the RETIRED
# cleanup and the deploy loop below, so the two cannot drift apart.
NAMESPACED_ONLY=(model.md provider.md voice.md recap.md)

# CLEANED names the retired commands this run's rm actually removed. Declared
# here, above the prod-only block that populates it, so the permission
# section below (which reads it to decide which Skill() grants to prune) can
# reference it safely under `set -u` even when PLUGIN_MODE never reaches the
# prod branch that fills it in.
CLEANED=()

# ── Clean up retired commands ─────────────────────────────────────────
if [[ "$PLUGIN_MODE" == "prod" ]]; then
  RETIRED=(say.md speak.md notify.md vox-on.md vox-off.md enable.md disable.md \
    model.md provider.md voice.md recap.md)
  FAILED_CLEAN=0
  for name in "${RETIRED[@]}"; do
    dest="$COMMANDS_DIR/$name"
    [[ -f "$dest" ]] || continue
    # The four NAMESPACED_ONLY names are generic enough (model, provider,
    # voice, recap) that a user could plausibly have hand-authored their own
    # command of the same name -- unlike the other seven retired names
    # (say.md, vox-on.md, ...), which are vox-specific with no realistic
    # collision. Only retire a NAMESPACED_ONLY file when it is genuinely
    # vox's own deployment.
    #
    # This is a FINGERPRINT check, not exact content equality: every vox
    # command file's frontmatter references an `mcp__plugin_vox_mic__*` tool
    # (the tool namespace vox's own MCP server registers under, not
    # something a user's hand-authored command would plausibly contain), and
    # that string is stable across edits to a command's prose. Exact-content
    # equality was tried first and is wrong -- recap.md's own H1 and Usage
    # text changed in this same PR (bare /recap -> /vox:recap), so a stale
    # pre-PR recap.md deployed by an earlier session would never byte-match
    # the current shipped file and would never be retired, leaving /recap
    # bare forever on every upgrading install. A fingerprint that only cares
    # "is this vox's own file" survives that class of future edit too.
    _namespaced=0
    for skip in "${NAMESPACED_ONLY[@]}"; do
      [[ "$name" == "$skip" ]] && { _namespaced=1; break; }
    done
    if [[ "$_namespaced" -eq 1 ]] \
      && ! grep -q 'mcp__plugin_vox_mic__' "$dest" 2>/dev/null; then
      continue
    fi
    # `-f` passing doesn't guarantee $COMMANDS_DIR is writable -- an rm that
    # fails here must not take the rest of the hook down with it under `set -e`.
    # The captured stderr is sanitized before it reaches ACTIONS -- see the
    # sanitization-invariant comment on the no-jq heredoc fallback below.
    if _rm_err=$(rm "$dest" 2>&1); then
      CLEANED+=("/${name%.md}")
    else
      FAILED_CLEAN=$((FAILED_CLEAN + 1))
      _rm_err="${_rm_err//\\/}"
      _rm_err="${_rm_err//\"/\'}"
      _rm_err="${_rm_err//[[:cntrl:]]/ }"
      ACTIONS+=("Failed to remove retired command ~/.claude/commands/$name: $_rm_err")
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
# Skip *-dev.md files — dev commands use plugin namespace (vox-dev:say-dev).
# Skip NAMESPACED_ONLY files (declared above) — model/provider/voice/recap are
# deliberately namespaced-only.
if [[ "$PLUGIN_MODE" == "prod" ]]; then
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
  # channel this hook has to say why commands never showed up. This static
  # summary line reports the fixed path "~/.claude/commands", never
  # $COMMANDS_DIR (which could carry a quote or backslash from an unusual
  # $HOME); the per-file failure message added below the loop DOES carry
  # variable content (the OS error text), so it is sanitized before joining
  # ACTIONS -- see the comment at its cp/rm call site.
  if mkdir -p "$COMMANDS_DIR" 2>/dev/null; then
    for cmd_file in "$PLUGIN_ROOT/commands/"*.md; do
      name="$(basename "$cmd_file")"
      [[ "$name" == *-dev.md ]] && continue
      _skip=0
      for skip in "${NAMESPACED_ONLY[@]}"; do
        [[ "$name" == "$skip" ]] && { _skip=1; break; }
      done
      [[ "$_skip" -eq 1 ]] && continue
      dest="$COMMANDS_DIR/$name"
      if [[ ! -f "$dest" ]] || ! diff -q "$cmd_file" "$dest" >/dev/null 2>&1; then
        # Sanitized the same way the retired-command rm error is -- see the
        # sanitization-invariant comment on the no-jq heredoc fallback below.
        if _cp_err=$(cp "$cmd_file" "$dest" 2>&1); then
          DEPLOYED+=("/${name%.md}")
        else
          FAILED_DEPLOY=$((FAILED_DEPLOY + 1))
          _cp_err="${_cp_err//\\/}"
          _cp_err="${_cp_err//\"/\'}"
          _cp_err="${_cp_err//[[:cntrl:]]/ }"
          ACTIONS+=("Failed to deploy ~/.claude/commands/$name: $_cp_err")
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
#
# Skipped entirely when the mode is unknown: TOOL_GLOB is the dev/prod-specific
# half of every rule written here, so there is nothing correct to write without
# it, and `set -u` would abort on the unset variable rather than guess.
if [[ "$PLUGIN_MODE" == "unknown" ]]; then
  : # already reported above; the tool glob is unknowable without the manifest
elif ! command -v jq >/dev/null 2>&1; then
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

  # Remove a stale bare Skill(<name>) grant for each of model/provider/
  # voice/recap that this run's retired-command cleanup ACTUALLY removed
  # (CLEANED, populated above -- prod mode only, and only for a file that
  # passed the vox-fingerprint check). Forward-integration cleanup of
  # superseded state, the same class of thing the RETIRED command cleanup
  # already does for files above -- an upgrading user would otherwise keep
  # a now-meaningless grant forever, since nothing else in this hook ever
  # removes a permission rule once added.
  #
  # Deliberately NOT unconditional the way the legacy-MCP-pattern cleanup
  # above is: a bare Skill(model) grant is exactly as plausibly the user's
  # own permission for their own hand-authored command as
  # ~/.claude/commands/model.md itself is (see the fingerprint-check
  # comment on the file retirement above) -- stripping it independent of
  # whether the file was actually vox's would silently revoke a grant that
  # was never vox's to touch. Scoped to prod mode only, matching where
  # file retirement runs, so a vox-dev session never strips grants the
  # sibling prod plugin wrote (that would just have prod re-add them next
  # start, churning settings.json every dev/prod session flip).
  STALE_SKILL_ARGS=()
  # Stock macOS ships bash 3.2 as /bin/bash, and "${empty_array[@]}" on that
  # version raises "unbound variable" under `set -u` even after `arr=()` --
  # a real bug in bash itself, not a `set -u` false alarm, fixed in bash 4.4.
  # CLEANED is empty on every fresh install and on every steady-state session
  # after the one-time retirement, so this is the common case, not an edge
  # case -- every other array expansion in this file is already guarded with
  # a `${#arr[@]} -gt 0` check; this one needs the same guard.
  if [[ "$PLUGIN_MODE" == "prod" ]] && [[ ${#CLEANED[@]} -gt 0 ]]; then
    for cleaned in "${CLEANED[@]}"; do
      bare="${cleaned#/}"
      for skip in "${NAMESPACED_ONLY[@]}"; do
        [[ "${bare}.md" == "$skip" ]] && { STALE_SKILL_ARGS+=("Skill($bare)"); break; }
      done
    done
  fi
  if [[ ${#STALE_SKILL_ARGS[@]} -gt 0 ]]; then
    # Guarded the same way every other command substitution that can fail in
    # this file is (_rm_err, _cp_err, PLUGIN_RULES, ADDED, TMP, _repo_root) --
    # a bare assignment under `set -euo pipefail` aborts the whole script on
    # failure, with no ACTIONS message and no exit-0 guarantee, violating this
    # file's own "a SessionStart hook must not take the session down with it"
    # invariant (see the PLUGIN_MODE comment above). Reproduced directly: a
    # failing `jq -cn --args` here kills the hook before it reaches the Lux
    # panel launch block, with nothing on stdout.
    STALE_SKILLS=$(jq -cn --args '$ARGS.positional' -- "${STALE_SKILL_ARGS[@]}") || {
      ACTIONS+=("jq failed to build stale-grant list — skipping stale grant cleanup")
      STALE_SKILLS=""
    }
    if [[ -n "$STALE_SKILLS" ]] && jq -e --argjson stale "$STALE_SKILLS" \
      '.permissions.allow // [] | map(select(. as $r | $stale | index($r))) | length > 0' \
      "$SETTINGS" >/dev/null 2>&1; then
      TMPFILE="$(mktemp "$SETTINGS.XXXXXX" 2>/dev/null || printf '')"
      if [[ -n "$TMPFILE" ]] && jq --argjson stale "$STALE_SKILLS" \
        '.permissions.allow = [.permissions.allow[] | select(. as $r | $stale | index($r) | not)]' \
        "$SETTINGS" > "$TMPFILE" && mv "$TMPFILE" "$SETTINGS"; then
        ACTIONS+=("Removed stale bare Skill() grants for: ${STALE_SKILL_ARGS[*]} (now namespaced-only)")
      else
        [[ -n "$TMPFILE" ]] && rm -f "$TMPFILE"
        ACTIONS+=("Failed to remove stale bare Skill() grants for: ${STALE_SKILL_ARGS[*]}")
      fi
    fi
  fi

  # Build PLUGIN_RULES via jq to avoid JSON injection from $TOOL_GLOB
  #
  # model/provider/voice/recap are namespaced-only commands (NAMESPACED_ONLY
  # above), so their skill grant is qualified with the plugin namespace below,
  # not the un-namespaced form -- a grant on the bare command name would
  # pre-approve a command that no longer deploys and never matches the actual
  # namespaced invocation.
  PLUGIN_RULES=$(jq -n --arg glob "$TOOL_GLOB" \
    '[$glob, "Skill(unmute)", "Skill(mute)", "Skill(vibe)", "Skill(vox)", "Skill(music)", "Skill(call)", "Skill(vox:model)", "Skill(vox:provider)", "Skill(vox:voice)", "Skill(vox:recap)"]' 2>/dev/null) || {
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
    # Fallback, no jq: this heredoc has no JSON escaping, so it embeds $MSG
    # into the string literal verbatim. ACTIONS messages may carry sanitized
    # dynamic content (the OS error text from a failed rm/cp) -- backslash
    # and double-quote are stripped, and every control character (newline,
    # tab, carriage return, ...) is replaced with a space, at the point each
    # message is built (see the sanitization at the rm/cp call sites above).
    # A literal-text-only ACTIONS message is inherently safe; a message
    # carrying variable content is safe only because it was sanitized before
    # ever reaching this array.
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
