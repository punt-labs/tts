#!/bin/sh
# Install punt-vox — voice for your AI coding assistant.
# Usage: curl -fsSL https://raw.githubusercontent.com/punt-labs/vox/<SHA>/install.sh | sh
set -eu

# --- Colors (disabled when not a terminal) ---
if [ -t 1 ]; then
  BOLD='\033[1m' GREEN='\033[32m' YELLOW='\033[33m' NC='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' NC=''
fi

info() { printf '%b▶%b %s\n' "$BOLD" "$NC" "$1"; }
ok()   { printf '  %b✓%b %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '  %b!%b %s\n' "$YELLOW" "$NC" "$1"; }
fail() { printf '  %b✗%b %s\n' "$YELLOW" "$NC" "$1"; exit 1; }

MARKETPLACE_REPO="punt-labs/claude-plugins"
MARKETPLACE_NAME="punt-labs"
PLUGIN_NAME="vox"
PACKAGE="punt-vox"
VERSION="4.16.0"
BINARY="vox"

# --- Argument parsing: --no-plugin / VOX_NO_PLUGIN (install-cli-only.md) ---
#
# `--no-plugin` (or VOX_NO_PLUGIN=1) installs the harness-neutral CLI and skips
# ONLY the Claude Code marketplace-register + plugin-install steps. Every other
# step -- binary, PATH, daemon, tool dirs, seed, and the user-scope CLAUDE.md
# @-import -- runs unchanged. Both forms work over `curl ... | sh`:
#   curl ... | sh -s -- --no-plugin      (flag)
#   curl ... | VOX_NO_PLUGIN=1 sh        (env)
usage() {
  printf 'Usage: install.sh [--no-plugin]\n'
  printf '  --no-plugin   Install the vox CLI without the Claude Code plugin\n'
  printf '                (equivalent to VOX_NO_PLUGIN=1).\n'
}

# Skip resolution is a single boolean, OR-combined from the flag, the env var,
# and capability-absence (Step 1). VOX_NO_PLUGIN skips only when set to exactly
# "1"; any other value -- empty, "0", "true", "yes" -- is ignored, matching the
# installer's internal 0/1 convention.
SKIP_PLUGIN=0
if [ "${VOX_NO_PLUGIN:-}" = "1" ]; then
  SKIP_PLUGIN=1
fi

for arg in "$@"; do
  case "$arg" in
    --no-plugin) SKIP_PLUGIN=1 ;;
    -h|--help)   usage; exit 0 ;;
    *)           printf 'install.sh: unknown option: %s\n' "$arg" >&2; usage >&2; exit 2 ;;
  esac
done

# --- Step 1: Prerequisites ---

info "Checking prerequisites..."

# Capability auto-skip: `claude` and `git` are needed ONLY to register the
# marketplace and clone/install the plugin. When either is absent there is no
# plugin to install into, so the plugin steps auto-skip and the CLI installs
# regardless -- the non-Claude-harness path (Codex, Cursor, a plain terminal).
# This OR-combines with the explicit --no-plugin / VOX_NO_PLUGIN request.
if command -v claude >/dev/null 2>&1; then
  ok "claude CLI found"
else
  SKIP_PLUGIN=1
  warn "'claude' CLI not found -- installing the vox CLI only (no Claude Code plugin)"
fi

if command -v git >/dev/null 2>&1; then
  ok "git found"
else
  SKIP_PLUGIN=1
  warn "'git' not found -- installing the vox CLI only (no Claude Code plugin)"
fi

# --- Step 2: uv ---

info "Checking uv..."

if command -v uv >/dev/null 2>&1; then
  ok "uv already installed"
else
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [ -f "$HOME/.local/bin/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.local/bin/env"
  elif [ -f "$HOME/.cargo/env" ]; then
    # shellcheck source=/dev/null
    . "$HOME/.cargo/env"
  fi
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv install succeeded but 'uv' not found on PATH. Restart your shell and re-run."
  fi
  ok "uv installed"
fi

# --- Step 3: Python 3.13+ ---

info "Checking Python..."

PYTHON_FLAG=""
HAVE_PYTHON=0
if command -v python3 >/dev/null 2>&1; then
  PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
  PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')
  if [ "$PY_MAJOR" -gt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -ge 13 ]; }; then
    ok "Python ${PY_MAJOR}.${PY_MINOR}"
    HAVE_PYTHON=1
  fi
fi

if [ "$HAVE_PYTHON" = "0" ]; then
  info "Installing Python 3.13 via uv..."
  uv python install 3.13 || fail "Failed to install Python 3.13"
  ok "Python 3.13 (uv-managed)"
  PYTHON_FLAG="--python 3.13"
fi

# --- Step 3b: System TTS (Linux only) ---
if [ "$(uname -s)" = "Linux" ]; then
  info "Checking system TTS..."
  if command -v espeak-ng >/dev/null 2>&1; then
    ok "espeak-ng found (offline fallback)"
  elif command -v espeak >/dev/null 2>&1; then
    ok "espeak found (offline fallback)"
  else
    warn "espeak-ng not found — install for offline TTS: sudo apt-get install espeak-ng"
    warn "Without it, you'll need a cloud TTS provider (e.g. API key for ElevenLabs/OpenAI or AWS credentials for Polly)"
  fi
fi

# --- Step 3c: Program audio player (mpv) ---
#
# mpv drives the daemon's PROGRAM audio tier (music, and later audiobooks and
# podcasts) over its JSON IPC socket. It is a HARD dependency with no fallback:
# the notification tier keeps afplay/say/espeak, but program audio needs mpv
# (docs/mpv-program-player.md). Install it the same tier as any other required
# tool -- Homebrew on macOS, the system package manager on Linux -- and fail
# the install if it cannot be made present, so a box always satisfies
# `vox doctor`.

# Runs inside an `if` condition, so `set -e` is suspended: a failed package
# manager returns non-zero to the caller instead of aborting the script.
_install_mpv() {
  case "$(uname -s)" in
    Darwin)
      command -v brew >/dev/null 2>&1 || return 1
      brew install mpv
      ;;
    *)
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y mpv
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y mpv
      elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm mpv
      else
        return 1
      fi
      ;;
  esac
}

info "Checking mpv (program audio player)..."
if command -v mpv >/dev/null 2>&1; then
  ok "mpv found"
else
  info "Installing mpv..."
  if _install_mpv && command -v mpv >/dev/null 2>&1; then
    ok "mpv installed"
  else
    fail "Could not install mpv. Install it manually (macOS: brew install mpv; Linux: apt/dnf/pacman install mpv), then re-run. mpv is required for program audio."
  fi
fi

# --- Step 4: Install vox CLI ---

info "Installing $PACKAGE..."

# Clean up root-owned __pycache__ left by older ``sudo vox daemon install``
# flows (pre-sudo-scoping refactor). Without this, uv tool install fails
# with Permission denied on Linux upgrades from those older versions.
_uv_tools="${XDG_DATA_HOME:-$HOME/.local/share}/uv/tools/$PACKAGE"
if [ -d "$_uv_tools" ] && [ -n "$(find "$_uv_tools" -name __pycache__ -user root -print -quit 2>/dev/null)" ]; then
  sudo find "$_uv_tools" -name __pycache__ -user root -exec rm -rf {} + 2>/dev/null || true
fi

# shellcheck disable=SC2086
uv tool install --force $PYTHON_FLAG "$PACKAGE==$VERSION" || fail "Failed to install $PACKAGE==$VERSION"
ok "$PACKAGE installed"

if ! command -v "$BINARY" >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v "$BINARY" >/dev/null 2>&1; then
    fail "$PACKAGE installed but '$BINARY' not found on PATH"
  fi
fi

ok "$BINARY $(command -v "$BINARY")"

# --- Step 4b: Migrate legacy .vox/ config ---
_migrate_legacy_config() {
  _mlc_repo_root="$1"
  _mlc_legacy_dir="${_mlc_repo_root}/.vox"
  _mlc_legacy_config="${_mlc_legacy_dir}/config.md"
  _mlc_new_dir="${_mlc_repo_root}/.punt-labs/vox"
  _mlc_new_config="${_mlc_new_dir}/config.md"

  [ -f "$_mlc_legacy_config" ] || return 0
  [ -L "$_mlc_legacy_dir" ] && { warn ".vox/ is a symlink, skipping migration"; return 0; }
  [ -L "$_mlc_legacy_config" ] && { warn ".vox/config.md is a symlink, skipping migration"; return 0; }

  if [ -e "${_mlc_repo_root}/.punt-labs" ] && [ ! -d "${_mlc_repo_root}/.punt-labs" ]; then
    warn ".punt-labs exists but is not a directory, skipping migration"
    return 0
  fi

  [ -f "$_mlc_new_config" ] && { ok "config already at .punt-labs/vox/"; return 0; }

  mkdir -p "$_mlc_new_dir" || { warn "could not create $_mlc_new_dir"; return 0; }
  mv "$_mlc_legacy_config" "$_mlc_new_config" || { warn "could not move config.md"; return 0; }
  ok "migrated .vox/config.md -> .punt-labs/vox/config.md"

  # Clean up ephemeral audio
  rm -f "${_mlc_legacy_dir}"/*.mp3 2>/dev/null

  # Remove .vox/ if empty
  rmdir "$_mlc_legacy_dir" 2>/dev/null || warn ".vox/ not empty after migration (user files detected)"
}

if [ -f "${PWD}/.vox/config.md" ]; then
  info "Checking for legacy .vox/ config..."
  _migrate_legacy_config "$PWD"
fi

# --- Step 5: Install daemon ---

if [ -f /Library/LaunchDaemons/com.punt-labs.voxd.plist ]; then
  info "Migrating vox daemon (one sudo prompt to remove old system service)..."
else
  info "Installing vox daemon..."
fi
_vox_path="$(command -v "$BINARY")"
if "$_vox_path" daemon install; then
  ok "vox daemon installed"
else
  warn "Could not install vox daemon (run '$_vox_path daemon install' manually)"
fi

# --- Steps 6-8: Claude Code plugin ---
#
# Skipped as a unit when SKIP_PLUGIN=1 (--no-plugin, VOX_NO_PLUGIN=1, or the
# claude/git capability auto-skip from Step 1). The scope is exactly the
# marketplace-register + plugin-install steps; the user-scope guide import
# below runs in both modes.

if [ "$SKIP_PLUGIN" = "0" ]; then
  # --- Step 6: Register marketplace ---

  info "Registering Punt Labs marketplace..."

  if claude plugin marketplace list < /dev/null 2>/dev/null | grep -q "$MARKETPLACE_NAME"; then
    ok "marketplace already registered"
    claude plugin marketplace update "$MARKETPLACE_NAME" < /dev/null 2>/dev/null || true
  else
    claude plugin marketplace add "$MARKETPLACE_REPO" < /dev/null || fail "Failed to register marketplace"
    ok "marketplace registered"
  fi

  # --- Step 7: SSH fallback for plugin install ---

  # claude plugin install clones via SSH (git@github.com:...).
  # Users without SSH keys need an HTTPS fallback.
  NEED_HTTPS_REWRITE=0
  cleanup_https_rewrite() {
    if [ "$NEED_HTTPS_REWRITE" = "1" ]; then
      git config --global --unset url."https://github.com/".insteadOf 2>/dev/null || true
      NEED_HTTPS_REWRITE=0
    fi
  }
  trap cleanup_https_rewrite EXIT INT TERM

  if ! ssh -n -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    warn "SSH auth to GitHub unavailable, using HTTPS fallback"
    git config --global url."https://github.com/".insteadOf "git@github.com:"
    NEED_HTTPS_REWRITE=1
  fi

  # --- Step 8: Install or upgrade plugin ---

  info "Installing $PLUGIN_NAME plugin..."

  claude plugin uninstall "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null 2>/dev/null || true
  if ! claude plugin install "${PLUGIN_NAME}@${MARKETPLACE_NAME}" < /dev/null; then
    cleanup_https_rewrite
    fail "Failed to install $PLUGIN_NAME"
  fi
  if ! claude plugin list < /dev/null 2>/dev/null | grep -q "$PLUGIN_NAME@$MARKETPLACE_NAME"; then
    cleanup_https_rewrite
    fail "$PLUGIN_NAME install reported success but plugin not found"
  fi
  ok "$PLUGIN_NAME plugin installed"

  cleanup_https_rewrite
else
  info "Skipping Claude Code plugin (CLI-only install)"
fi

# --- Register the agent usage guide (user-scope CLAUDE.md @-import) ---
#
# Runs in BOTH plugin and CLI-only modes. The plugin path installs directly
# (never via `vox install`), so the import must be registered here; and a
# --no-plugin box still needs the ~/.punt-labs/vox/CLAUDE.md import so an agent
# driving vox through the `vox` CLI gets the guidance. Idempotent + best-effort.
#
# `register-guidance` is unreleased plumbing: a punt-vox pinned to an older
# VERSION won't have the subcommand, so probe with `--help` (typer exits
# non-zero on an unknown command) before invoking. An older install no-ops
# cleanly instead of emitting a failure every run; a new enough install
# registers. Do not couple this to a lockstep version bump.
if "$BINARY" register-guidance --help < /dev/null >/dev/null 2>&1; then
  if "$BINARY" register-guidance < /dev/null 2>/dev/null; then
    ok "usage guide registered"
  else
    warn "Could not register usage guide (run '$BINARY register-guidance' manually)"
  fi
else
  info "usage guide registration not supported by $PACKAGE $VERSION (skipped)"
fi

# --- Step 9: Verify ---

info "Verifying installation..."
printf '\n'
"$BINARY" doctor || true
printf '\n'

# --- Done ---
#
# The final message is gated on the skip boolean, not on the reason for it, so
# the capability auto-skip and the explicit --no-plugin print the same
# CLI-only block -- and neither prints "Restart Claude Code" when no plugin was
# installed.

if [ "$SKIP_PLUGIN" = "0" ]; then
  printf '%b%b%s is ready!%b\n\n' "$GREEN" "$BOLD" "$PLUGIN_NAME" "$NC"
  printf 'Restart Claude Code, then:\n'
  printf '  /enable       # turn vox on for this repo\n'
  printf '  /recap        # spoken summary of what just happened\n\n'
else
  printf '%b%bvox CLI is ready!%b\n\n' "$GREEN" "$BOLD" "$NC"
  printf 'The vox CLI is installed (no Claude Code plugin). Try:\n'
  printf '  vox say "Build finished"   # speak text through the daemon\n'
  printf '  vox enable                 # turn vox on for a repo\n'
  printf '  vox doctor                 # check providers and daemon\n\n'
  printf 'To add the Claude Code plugin later, re-run this installer without --no-plugin.\n\n'
fi
