#!/bin/sh
# One-command uninstaller for cswap + per-prompt account rotation.
#
#   curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/uninstall.sh | sh
#
# By default this KEEPS your saved accounts, so reinstalling brings rotation
# straight back. To delete them too (after an automatic backup export):
#
#   curl -fsSL .../uninstall.sh | sh -s -- --purge
#
# Options:
#   --purge       also delete saved accounts and credential backups
#   --no-backup   with --purge: skip the backup export first (not recommended)
#
# Never touched: your current Claude Code login, Orca's own accounts, and any
# other hooks in ~/.claude/settings.json.
set -eu

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

PURGE=0
BACKUP=1
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --no-backup) BACKUP=0 ;;
        -h | --help)
            sed -n '2,17p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "unknown option: $arg (see --help)" ;;
    esac
done
[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root"

# Find cswap even when uv's bin dir isn't on PATH yet.
CSWAP=""
if command -v cswap >/dev/null 2>&1; then
    CSWAP="$(command -v cswap)"
elif command -v uv >/dev/null 2>&1 && [ -x "$(uv tool dir --bin 2>/dev/null)/cswap" ]; then
    CSWAP="$(uv tool dir --bin)/cswap"
fi

if [ -z "$CSWAP" ]; then
    warn "cswap is not installed; nothing to uninstall"
    exit 0
fi

# 1. The per-prompt hook (other hooks and settings are left alone)
say "Removing the per-prompt rotation hook"
"$CSWAP" hook uninstall </dev/null || warn "could not remove the hook; run 'cswap hook uninstall' by hand"

# 2. Saved accounts: kept unless --purge
if [ "$PURGE" = 1 ]; then
    if [ "$BACKUP" = 1 ]; then
        BACKUP_FILE="$HOME/cswap-backup-$(date +%Y%m%d-%H%M%S).cswap"
        say "Backing up saved accounts to $BACKUP_FILE"
        if OUT="$("$CSWAP" export "$BACKUP_FILE" </dev/null 2>&1)"; then
            chmod 600 "$BACKUP_FILE" 2>/dev/null || true
        else
            case "$OUT" in
                *"no accounts to export"*) warn "no saved accounts to back up" ;;
                *) die "backup failed, so nothing was deleted: $OUT (use --no-backup to purge anyway)" ;;
            esac
        fi
    fi
    say "Deleting saved accounts and credential backups"
    printf 'y\n' | "$CSWAP" purge
else
    say "Keeping your saved accounts (reinstall restores rotation; use --purge to delete them)"
fi

# 3. The cswap tool itself
if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^claude-swap '; then
    say "Uninstalling cswap"
    uv tool uninstall claude-swap
elif command -v pipx >/dev/null 2>&1 && pipx list 2>/dev/null | grep -q 'claude-swap'; then
    say "Uninstalling cswap (pipx)"
    pipx uninstall claude-swap
else
    warn "cswap wasn't installed with uv or pipx; remove $CSWAP by hand"
fi

echo
echo "Done. Your current Claude Code login is unchanged."
if [ "$PURGE" = 1 ] && [ "${BACKUP_FILE:-}" ] && [ -f "$BACKUP_FILE" ]; then
    echo "Account backup: $BACKUP_FILE  (keep it private: it holds credentials)"
    echo "Restore later:  reinstall, then run  cswap import $BACKUP_FILE"
fi
echo "Reinstall:      curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/install.sh | sh"
