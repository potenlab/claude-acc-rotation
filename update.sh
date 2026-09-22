#!/bin/sh
# One-command updater for cswap + per-prompt account rotation.
#
#   curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/update.sh | sh
#
# Reinstalls the latest cswap from this repo's main branch. Kept as they are:
# your saved accounts, your settings, and the rotation hook — a hook you turned
# off stays off, and one installed with custom flags keeps them. Open Claude
# Code sessions use the new version from their next prompt; no restart needed.
#
# Environment overrides:
#   CSWAP_SOURCE  what to install (default: this repo's main branch on GitHub;
#                 a local path works too, e.g. CSWAP_SOURCE=. ./update.sh)
set -eu

REPO_URL="https://github.com/potenlab/claude-acc-rotation"
SOURCE="${CSWAP_SOURCE:-git+${REPO_URL}@main}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root"

# Find the installed cswap even when uv's bin dir isn't on PATH yet.
CSWAP=""
if command -v cswap >/dev/null 2>&1; then
    CSWAP="$(command -v cswap)"
elif command -v uv >/dev/null 2>&1 && [ -x "$(uv tool dir --bin 2>/dev/null)/cswap" ]; then
    CSWAP="$(uv tool dir --bin)/cswap"
fi
[ -n "$CSWAP" ] || die "cswap isn't installed yet. Install it with:
  curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/install.sh | sh"

BEFORE="$("$CSWAP" --version 2>/dev/null | awk '{print $NF}')"

if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^claude-swap '; then
    say "Updating cswap from ${SOURCE}"
    uv tool install --force --python 3.12 "$SOURCE"
elif command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q 'claude-swap'; then
    say "Updating cswap from ${SOURCE} (pipx)"
    pipx install --force "$SOURCE"
else
    die "cswap at $CSWAP wasn't installed with uv or pipx; reinstall it with install.sh"
fi

AFTER="$("$CSWAP" --version 2>/dev/null | awk '{print $NF}')"
if [ "$BEFORE" = "$AFTER" ]; then
    say "cswap $AFTER (already the latest version; reinstalled anyway)"
else
    say "cswap $BEFORE -> $AFTER"
fi

# The hook is left alone: same command, same flags, same on/off state.
HOOK="$("$CSWAP" hook status </dev/null 2>/dev/null || true)"
case "$HOOK" in
    Installed*) say "Rotation hook: on (unchanged)" ;;
    *) warn "Rotation hook is off. Turn it on with: cswap hook install" ;;
esac

echo
echo "Done. Your saved accounts and settings are unchanged."
echo "Open Claude Code sessions use the new version from their next prompt."
