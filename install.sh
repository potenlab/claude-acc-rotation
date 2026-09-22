#!/bin/sh
# One-command installer: cswap + per-prompt account rotation for Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/install.sh | sh
#
# Works out of the box: every prompt checks the current account and only
# switches when it's at its limit. (Advanced: flags after `sh -s --` go to
# `cswap hook install`.)
#
# Environment overrides:
#   CSWAP_SOURCE  what to install (default: this repo's main branch on GitHub;
#                 a local path works too, e.g. CSWAP_SOURCE=. ./install.sh)
#   CSWAP_NO_ADD  set to 1 to skip adding the currently logged-in account
set -eu

REPO_URL="https://github.com/potenlab/claude-acc-rotation"
SOURCE="${CSWAP_SOURCE:-git+${REPO_URL}@main}"

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Darwin | Linux) ;;
    *) die "this installer supports macOS and Linux (on Windows: uv tool install ${SOURCE}; cswap hook install)" ;;
esac
[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not root"

# 1. uv (Python tool installer; brings its own Python 3.12+)
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; open a new terminal and re-run"
fi

# 2. cswap
say "Installing cswap from ${SOURCE}"
uv tool install --force --python 3.12 "$SOURCE"
BIN_DIR="$(uv tool dir --bin)"
PATH="$BIN_DIR:$PATH"
export PATH
command -v cswap >/dev/null 2>&1 || die "cswap was not found in ${BIN_DIR}"

# 3. per-prompt hook (resolves cswap to its absolute path in settings.json)
say "Enabling account rotation on every Claude Code prompt"
cswap hook install "$@" </dev/null

# 4. the account Claude Code is logged into right now
if [ "${CSWAP_NO_ADD:-0}" != 1 ]; then
    say "Adding the currently logged-in Claude account"
    cswap add </dev/null || warn "no Claude Code login found yet; log in with 'claude', then run 'cswap add'"
fi

cat <<EOF

Done. To add your other accounts, for each one:
  1. In Claude Code run /login and sign in with the next account
     (don't /logout first: that can revoke the saved account's token)
  2. Run: cswap add

Then check everything with:  cswap list
("cswap: command not found"? run 'uv tool update-shell' and open a new terminal)
Uninstall (keeps accounts):  curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/uninstall.sh | sh
EOF
