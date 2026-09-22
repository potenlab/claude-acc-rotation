# claude-swap

Multi-account switcher for Claude Code. Easily switch between multiple Claude accounts without logging out, or let it switch for you before you hit a rate limit. Track usage for every account in a live dashboard, and run accounts in parallel. Works with both the Claude Code CLI and the VS Code extension.

## Quick install (account rotation on every prompt)

One command installs cswap, turns on the [per-prompt hook](#check-on-every-prompt-claude-code-hook), and adds the Claude account you're logged into (macOS / Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/install.sh | sh
```

Pass hook options after `sh -s --`, e.g. `... | sh -s -- --threshold 80`. Then add each of your other accounts: `/login` with it in Claude Code, and run `cswap add`.

To uninstall, which **keeps your saved accounts** so a reinstall brings rotation straight back:

```bash
curl -fsSL https://raw.githubusercontent.com/potenlab/claude-acc-rotation/main/uninstall.sh | sh
```

Add `sh -s -- --purge` to delete the saved accounts too. It exports a backup to `~/cswap-backup-<date>.cswap` first and refuses to delete anything if that backup fails. Your current Claude Code login, Orca's accounts and your other hooks are never touched.

**Back up your accounts.** A deleted account store means logging in to every account again. One file avoids that:

```bash
cswap export ~/cswap-backup.cswap      # keep it private: it holds credentials
cswap import ~/cswap-backup.cswap      # restore every account, no logins
```

## Test results (real accounts, 2026-09-21)

Everything below was measured on a real setup — 4 Claude Max accounts on macOS (Darwin 25.6, Python 3.12) — not simulated.

**Rotation across accounts.** Three prompts in a row, each line a real credential swap:

| Prompt | Active account before | After | Note |
|---|---|---|---|
| 1 | 4 · treesoop.dion@ | **1** · daehyeonnam@ | |
| 2 | 1 · daehyeonnam@ | **2** · dev@potenlab.dev | |
| 3 | 2 · dev@potenlab.dev | **4** · treesoop.dion@ | account 3 skipped: 7d window at 100% |

Account 3 was at its weekly limit and was skipped on every pass, so no prompt ever landed on an exhausted account.

**Threshold mode picks the right target.** With the active account at 77% of its 7-day window and a threshold of 70%, the engine reported:

```
Account-4 (treesoop.dion@): 77% used (switch at 70%) | others: #1: 5h 26% · 7d 11%,
#2: 5h 37% · 7d 57%, #3: 5h 0% · 7d 100%
→ would switch Account-4 -> Account-1 (daehyeonnam@)
```

It chose account 1 (the most quota left) and rejected account 3 (exhausted).

**Speed.** Time added to a prompt, measured with `/usr/bin/time`:

| Hook mode | Wall time |
|---|---|
| `--rotate` (a real account switch) | 0.43 – 0.45 s |
| threshold check, no switch needed | 0.55 s (1.11 s on the first, cold run) |

In threshold mode the check is also throttled to once per 20 s, so most prompts pay nothing at all.

**Safety.** Verified by hand and covered by tests:

- `settings.json` keeps its other keys and its `0600` permissions when the hook is installed or removed; other hooks are left untouched.
- Re-running `hook install` replaces the entry instead of stacking duplicates.
- The hook exits 0 on a crash, a network failure, or a stale flag — a `UserPromptSubmit` hook that exits 2 would erase your prompt, so this path is tested explicitly.
- Nothing the hook prints reaches the model; a switch is reported as a `systemMessage` that only you see.
- `cswap run` sessions are left alone, since they're pinned to one account.

**Test suite:** 2309 passed, 4 skipped (`uv run pytest`), including 42 tests for the hook itself.

**Known limits, measured too:** on macOS, Claude Code caches Keychain credentials for ~30 s, so a prompt sent within that window may still run on the previous account — the rotation stays correct, it just lags. And every switch makes the next message rebuild its conversation cache, which costs extra tokens; on long conversations, threshold mode is the cheaper choice.

## Installation

### Using uv (recommended)

```bash
uv tool install claude-swap
```

### Using pipx

```bash
pipx install claude-swap
```

### From source

```bash
git clone https://github.com/realiti4/claude-swap.git
cd claude-swap
uv sync
uv run cswap help
```

### Updating

```bash
cswap upgrade          # uv/pipx installs on macOS/Linux: auto-detects and upgrades
# or run your installer directly:
uv tool upgrade claude-swap
pipx upgrade claude-swap
```

## Usage

### Add your first account

Log into Claude Code with your first account, then:

```bash
cswap add
```

### Add more accounts

Log in with another account, then:

```bash
cswap add
```

Do not run `/logout` first: current Claude Code may revoke the refresh token stored for the account you are leaving.

### Switch accounts

Rotate to the next account:

```bash
cswap switch
```

Or switch to a specific account:

```bash
cswap switch 2
cswap switch user@example.com
cswap switch dev                # or by alias, once set with `cswap alias 2 dev`
```

Not sure which one? `cswap list` is the dashboard — every account's 5-hour and 7-day usage and reset times at a glance:

```bash
cswap list
```

Or let claude-swap auto-pick by remaining quota — `cswap switch --strategy best` (most quota left) or `--strategy next-available` (skip rate-limited accounts).

**Note:** You usually don't need to restart — on Linux/Windows the new account is picked up automatically, and on macOS after the Keychain cache expires. To apply it instantly, restart Claude Code or reopen the VS Code extension tab. See [Tips](#tips) for the per-platform details.

### Automatic switching

Let claude-swap watch your usage and switch for you. When the active account's 5-hour or 7-day window reaches the threshold (default 90%), it switches to the account with the most quota left — before you hit the limit, and safe to run while Claude Code is working:

```bash
cswap auto                     # foreground loop, polls every 60s
cswap auto --threshold 80      # switch earlier
cswap auto --model Fable       # also switch when the Fable weekly limit is hit
cswap auto --once              # single check-and-switch, for cron/scripts
cswap auto --dry-run           # log what it would do, never switch
cswap auto --strategy consume-first   # burn the soonest-resetting account first
```

<details>
<summary>How it behaves & advanced usage</summary>

- Runs safely alongside Claude Code: switches take the same credential locks Claude Code uses, so a swap never collides with a token refresh.
- A cooldown (default 5 min) and a hysteresis margin stop it flip-flopping near the threshold: a proactive switch only lands on an account that's below the threshold *and* better than the current one by the margin — a candidate that clears the margin is always taken, but two accounts hovering at the line never ping-pong. When every account is exhausted it keeps checking on a bounded slow cadence, waking sooner for an imminent reset.
- **Strategies** (`--strategy`, or `cswap config set autoswitch.strategy`): `best` (default) stays put until the active account nears its limit, then moves to the account with the most quota left. `consume-first` proactively keeps you on the account whose **weekly window resets soonest** — use-it-or-lose-it — switching to a sooner-resetting account (with room to spare) even below the threshold, so perishable weekly quota isn't wasted.
- Usage polling is adaptive — a couple of accounts per check, busy alternates watched more closely, and exhausted ones checked about every ten minutes (or slower after 429s) — so API traffic stays flat no matter how many accounts you manage.
- It fails safe: if a usage check errors it keeps trusting the last-known numbers while retries back off, and an expired token on an idle machine makes it hold rather than fail over (Claude Code refreshes the token on your next message).
- An account whose refresh token has died is quarantined and reported until you either log in with it and re-run `cswap add --slot N`, or replace its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots on its own (`--force` is still required to replace other existing accounts; note a stale export can carry an already-superseded token). API-key accounts are never rotated onto unless you pass `--include-api-key-accounts`.
- To hold an account out of rotation yourself — a work account you don't want touched, one you're resting — run `cswap disable <num|email>`; `cswap enable <num|email>` puts it back. Disabled accounts are skipped by auto-switch, bare `cswap switch`, and the `best` / `next-available` strategies, but stay fully managed and remain a valid explicit `cswap switch <num|email>` target. They show a `(disabled)` marker in `cswap list`, in the [TUI](#interactive-dashboard-tui), and in the [menu bar](#menu-bar-macos) — both of which also let you toggle the state in place (TUI: menu → *Disable / enable account…*; menu bar: *Disable / enable account*).
- By default only the account-wide 5h/7d windows drive switching. If you work on one model and hit its **weekly per-model limit** first (e.g. Fable), add `--model Fable` (or `cswap config set autoswitch.model Fable`) to fold that model's window into the decision, so it switches off an account whose model quota is spent even while its 5h/7d windows still have room.
  - **Model names** are Anthropic's own per-model `display_name`s, matched case-insensitively. The exact strings for your accounts are the per-model rows in `cswap list` (e.g. a line reading `Fable: 100%`).

For cron/systemd timers, `--once` reports the outcome in its exit code (`0` switched, `1` error, `2` nothing to do, `3` blocked — no viable target), and `--json` emits one JSON event per line:

```bash
*/5 * * * * cswap auto --once --json >> ~/.cswap-auto.log 2>&1
```

Defaults like the threshold and cooldown are configurable with `cswap config set autoswitch.threshold 80` — flags override them (see [Configuration](#configuration)).

</details>

### Check on every prompt (Claude Code hook)

Instead of keeping `cswap auto` running, let Claude Code trigger the check itself: every prompt you submit runs one auto-switch tick, the same decision as `cswap auto --once`. This works well for a pool of accounts shared by a team, since each machine rotates on its own whenever someone sends a prompt.

```bash
cswap hook install                      # add a UserPromptSubmit hook to ~/.claude/settings.json
cswap hook install --threshold 80       # switch earlier (other flags: --strategy, --model, --cooldown)
cswap hook status                       # show the installed command
cswap hook uninstall                    # remove it (other hooks are left alone)
```

- The hook never blocks or breaks a prompt: it always exits 0, and nothing it prints reaches the model. When it does switch, Claude Code shows a one-line `cswap: Switched Account-1 -> Account-2 ...` notice.
- It runs at most once every 20 seconds (`--min-interval`), however many sessions are open. Usage is still read on the adaptive schedule described in [How it works](#how-it-works), so most prompts cost no API call.
- It skips `cswap run` sessions, because those are pinned to one account.
- The switch applies to your next request. On macOS, Claude Code caches Keychain credentials for about 30 seconds (see [Tips](#tips)).
#### Check on every prompt

`--rotate` checks the current account on every prompt, and **only switches when it has to**:

```bash
cswap hook install --rotate --detach-orca    # recommended
```

- Current account has more than 15% left (`hook.reserve`) → stay. No switch, no message.
- Current account is at its limit, or its login is dead → move to the account with the **most room left**. Accounts near their own limit, dead, disabled or with unreadable usage are never picked.
- No account has room → stay, and say so.

Staying put matters: every switch makes the next message rebuild its conversation cache (extra quota), and on macOS a switch takes ~30 s to reach running sessions. The other modes switch far more often:

```bash
cswap hook install --rotate=next-available   # move to the next account on EVERY prompt
cswap hook install --rotate=best             # jump whenever another account has more room
cswap hook install --rotate=plain            # rotate blindly, ignoring usage
```

Accounts near their limit are **held out until they reset**, then used again. The margin is `hook.reserve` (default 5; set it once and every open session follows):

```bash
cswap config set hook.reserve 15      # "at its limit" = less than 15% left
```

Or per install with `--reserve`:

```bash
cswap hook install --rotate --reserve 10     # hold accounts back at 90% used
cswap hook install --rotate --reserve 0      # only skip accounts fully at 100%
```

**The first prompt of every session checks your login first.** Before it goes out, the hook asks Anthropic's server whether the account you're logged into still works. An account whose token was revoked still looks valid locally, and every request on it fails with `401 OAuth access token has been revoked` — so if the check fails, the hook moves to the next usable account before your prompt is sent (`cswap: Account-2's login was revoked — Switched to Account-3 ...`). This runs in every mode, once per session, and never acts on an expired-but-refreshable token or a network error.

Accounts whose saved refresh token is dead are skipped too — switching onto one would only produce a 401 — and the hook names them so you know which to log in again. After you `/login` with such an account, the next prompt saves the new login automatically; no `cswap add` needed.

If every other account is held out, the hook stays on the current one and says so (`cswap: All other accounts are at their 5h/7d limit (keeping 5% in reserve) — staying on Account-1.`) rather than failing silently.

This spreads a shared pool of accounts evenly and never lets one account carry a whole session. The cost is that each switch rebuilds the conversation cache on the next message, which uses extra quota — with long conversations, threshold mode (the default) is cheaper.

- In threshold mode the hook deliberately does **not** rotate on every single prompt. Each switch rebuilds the conversation cache, which uses extra quota, so it only moves when the active account nears its limit (or, with `--strategy consume-first`, when a sooner-resetting account is available).

#### Turning rotation off for some sessions (Orca, CI, a pinned terminal)

The hook lives in your user `settings.json`, so it applies to every Claude Code session on the machine. Three ways to exempt some of them, from narrowest to widest:

```bash
# 1. By directory — prompts from here (and below) never switch accounts
cswap hook install --rotate --skip-path ~/orca          # repeatable

# 2. By terminal — set this in the environment that launches Claude Code
CSWAP_HOOK_DISABLE=1 claude

# 3. By session — pin one account; the hook always skips `cswap run` sessions
cswap run 2
```

**With [Orca](https://orca.computer):** Orca's worktrees and embedded terminals run ordinary Claude Code sessions against your default `~/.claude` (verified: Orca doesn't set `CLAUDE_CONFIG_DIR` for terminal panes), so they read the same `settings.json` and **rotate automatically along with everything else** — no extra setup.

**Keep Orca off the login — `--detach-orca`.** Orca is an account switcher too: it writes the same login cswap does, and re-asserts its own pick on every Claude pane launch, on window focus, and on a usage poll every ~15 minutes. With two tools writing one login, each restores refresh tokens the other has already rotated, the server revokes them, and the next request fails with `401 OAuth access token has been revoked` until that account is logged in again. The fix is a single writer:

```bash
cswap orca status       # is Orca managing the Claude login?
cswap orca detach       # stop it; Orca's terminals keep working
cswap hook install --rotate --detach-orca   # and re-detach it on every prompt
```

Detaching is Orca's own "system default" setting: it keeps its account list, but stops overwriting the login. With `--detach-orca` the hook re-detaches it on the next prompt if someone picks an account in Orca's menu. This uses Orca's local runtime socket (`accounts.selectClaude`), which is **undocumented** — an Orca update could change it, and every failure is a silent skip. (`--sync-orca`, the earlier flag that pointed Orca at cswap's account, turned out to keep both tools writing; it now behaves as `--detach-orca`.)

Orca-launched sessions rotate by default. If you'd rather keep them on one account — useful when several worktrees share one long-running task — install the hook with `--skip-path ~/orca` (or wherever your Orca worktrees live), or export `CSWAP_HOOK_DISABLE=1` in the environment Orca launches terminals from. Either way the accounts stay managed: `cswap list`, `cswap switch` and the dashboard keep working, only the automatic per-prompt switching is off there.

To turn rotation off everywhere, remove the hook entirely:

```bash
cswap hook uninstall        # accounts and usage tracking stay; only the hook goes
```

And to hold one account out of rotation without touching the hook, use `cswap disable <num|email>`.

### Run multiple accounts at the same time (session mode)

Launch Claude Code as a specific account in the current terminal only — every other terminal and the VS Code extension stay on your default account, so two accounts can work in parallel.

```bash
cswap run 2                     # launch Claude Code as account 2, here only
cswap run user@example.com      # by email
cswap run 2 -- --resume         # everything after '--' is forwarded to claude
cswap run 2 --share-history     # share your chat history with this account too
cswap run 2 --require-session   # refuse rather than run plain claude if 2 is the default login
```

Sessions use your normal `~/.claude` setup (settings, CLAUDE.md, skills, MCP servers, etc.), but each account keeps its own chat history — pass `--share-history` if you want your accounts to continue the same conversations.

Running the account that is already your default login launches plain `claude` on that login instead of a session (a second copy of the active credential would go stale). Scripts that need the isolation guaranteed can pass `--require-session`, which refuses in that case instead.
  
A session refreshes its own copy of the account's token, so once it exits, the credential it rotated is captured back into the account's stored backup before a switch or usage check uses that backup. While a session is still running, `cswap switch` refuses to move the default login onto its account if the stored backup has already fallen behind (activating it could only fail); exit the session first, or pick another account. While a session runs, its account's usage is read with the session's own credential and never refreshed by cswap; a read the server refuses shows as token expired, and is not requested again, until the session renews the credential on its next call.

<details>
<summary>Sharing details — MCP servers & chat history</summary>

- With `--share-history`, a session started under one account shows up in `--resume` under the others, and nothing already saved is lost.
- User-scope MCP servers (`claude mcp add -s user`) are mirrored from your default profile on every launch — manage them there; changes made inside a session don't persist. Definitions are copied as-is (including inline `env`/`headers` values), but MCP OAuth logins are not — HTTP servers may ask you to authenticate once per profile via `/mcp`.
- `--no-share` turns sharing off and removes the mirrored MCP config (profiles that never mirrored are left alone).

</details>

<details>
<summary>Map accounts to directories — auto-pick per repo</summary>

Bind a directory to an account, and a bare `cswap run` there launches that account in session mode — e.g. work account in work repos, personal elsewhere:

```bash
cswap map 2 ~/work/client-app   # map a directory to account 2
cswap map user@example.com      # map the current directory
cswap map                       # list mappings
cswap unmap ~/work/client-app   # remove one (defaults to current directory)

cd ~/work/client-app/src
cswap run                       # → account 2, session mode
```

Subfolders inherit the nearest mapped ancestor. In an unmapped directory, `cswap run` just launches plain `claude` with your default login. Mappings are per-machine (not part of `cswap export`) and are cleaned up when their account is removed.

</details>

### Interactive dashboard (TUI)

Run `cswap` on its own (or `cswap tui`) for the full-screen dashboard: live usage for every account, switching, and the auto-switcher, all keyboard-driven. `cswap watch` opens it straight to the live monitor. Works on macOS, Linux, and Windows.

<img src="assets/tui-watch.png" width="760" alt="cswap watch — live 5h/7d usage bars for every account, with reset times and the active account marked">

### Refresh expired tokens

If an account's token expires, log back into Claude Code with that account and re-run:

```bash
cswap add
```

This will update the stored credentials without creating a duplicate.

### Other commands

```bash
cswap run 2                     # Run an account in this terminal only (session mode)
cswap auto                      # Auto-switch when nearing rate limits (see above)
cswap config                    # Show or edit settings (see Configuration below)
cswap list                      # Show all accounts with 5h/7d usage and reset times
cswap list --token-status       # Add source-labelled OAuth token diagnostics
cswap status                    # Show current account
cswap add --slot 3              # Add account to a specific slot (prompts before overwrite)
cswap add --alias dev           # Add account and give it a short alias
cswap remove 2                  # Remove an account
cswap disable 2                 # Hold an account out of auto-rotation (keeps its login)
cswap enable 2                  # Return a disabled account to rotation
cswap alias 2 dev               # Give an account a short alias (usable anywhere NUM|EMAIL is)
cswap alias 2 --unset           # Remove an account's alias
cswap alias                     # List all aliases
cswap move 2 1                  # Assign an account to a slot (relocates to an empty slot, swaps if taken)
cswap unclaimed                 # List stashed credential entries (slot + why they were stashed)
cswap unclaimed --purge ID      # Drop one (deletes its bytes; recover with /login + `cswap add`)
cswap tui                       # Interactive dashboard (also: bare `cswap`)
cswap watch                     # Dashboard, opened on the live watch page
cswap upgrade                   # Upgrade claude-swap to the latest version
cswap purge                     # Remove all claude-swap data
```

The original flag spellings (`cswap --switch`, `cswap --list`, ...) keep working.

## Tips

- **Do you need to restart after switching?** Usually not. On **Linux and Windows**, credentials are stored in a file and Claude Code re-reads them whenever that file changes, so the new account takes effect on your next message — no restart needed. On **macOS**, credentials live in the Keychain, which Claude Code caches for about 30 seconds; a running session picks up the switch once that cache expires. Restart Claude Code (or close and reopen the VS Code extension tab) only if you want the change to apply instantly.
- **Continuing sessions after switching:** You can keep using the same Claude Code session after switching — run `cswap switch` in any terminal and carry on. If you'd prefer a clean start, close and reopen Claude Code (or the VS Code extension tab) and use `--resume` to pick your previous session. Either way, the first message on the new account may use extra usage as its conversation cache rebuilds.

## How it works

- Backs up OAuth tokens and config when you add an account
- Swaps only the account-specific Claude login when you switch accounts;
  live account-independent OAuth state (such as MCP server logins) is
  preserved instead of being overwritten by a slot's older snapshot
- Account credentials stored securely using platform-appropriate methods
- Switches (manual and automatic) hold Claude Code's own credential locks while writing, so a swap never interleaves with a token refresh
- Auto-switch freshens a target's token before activating it, and quarantines accounts whose refresh token has died (recover by re-adding it with `cswap add --slot N`, or by replacing its stored credentials from a known-good export — a plain `cswap import backup.cswap` replaces dead-token slots automatically)
- Usage numbers refresh every few minutes — faster for an account being used or close to switching, slower for idle ones — keeping cswap comfortably inside Anthropic's rate limits however many dashboards you keep open on a machine. An age note like `· 6m ago` just means the next scheduled check hasn't come yet, not that something is stuck.

## Data locations

| Platform | Credentials | Config backups |
|----------|-------------|----------------|
| Windows | File-based (inside the backup directory, under `credentials/`) | `~/.claude-swap-backup/` |
| macOS | macOS Keychain | `~/.claude-swap-backup/` |
| Linux / WSL | File-based (inside the backup directory, under `credentials/`) | `${XDG_DATA_HOME:-~/.local/share}/claude-swap/` |

Session-mode profiles (`cswap run`) live under the backup directory in `sessions/`. Tool preferences (`settings.json`) and auto-switch state (`autoswitch_state.json` — cooldown and quarantined accounts; delete it to reset) live in the backup directory root.

On Linux/WSL, set `XDG_DATA_HOME` to override the default location.

## Menu bar (macOS)

<details>
<summary>Optional macOS menu bar app — usage at a glance, click to switch</summary>

Needs the `menubar` extra (macOS only):

```bash
uv tool install 'claude-swap[menubar]'   # or: pipx install 'claude-swap[menubar]'
cswap menubar
```

Shows every account's 5h / 7d / spend usage and switches with a click (specific / rotate / best / next-available), plus the TUI's add / disable-enable / remove / refresh actions. Enable *Settings → Auto-switch accounts* to run the same engine as [`cswap auto`](#automatic-switching) in the background; it shares the `autoswitch.*` settings, so the menu bar and CLI stay in sync. Off until you turn it on.

**Keep it running without a terminal.** `cswap menubar` runs in the foreground, so the status item dies with the terminal that started it and does not come back after a reboot. `--install-service` hands it to launchd instead — starts at login, restarts on crash, no `.app` bundle:

```bash
cswap menubar --install-service     # start now, and at every login
cswap menubar --service-status      # installed? loaded? pid?
cswap menubar --uninstall-service   # stop it and remove the plist
```

The agent lives at `~/Library/LaunchAgents/com.cswap.menubar.plist` and logs to `~/Library/Logs/com.cswap.menubar.{log,err}`. It pins the `cswap` console script, whose path survives an upgrade — but the running process keeps the old build until it restarts, so after `cswap upgrade` either re-run `--install-service` or `launchctl kickstart -k gui/$(id -u)/com.cswap.menubar`.

</details>

## Advanced

### Configuration

Tool preferences live in `settings.json` in the backup root; `cswap config` reads and edits it with validation, so you never have to find the file or guess valid ranges.

<details>
<summary>Commands & usage</summary>

```bash
cswap config                              # list effective settings ("(default)" = not set)
cswap config get autoswitch.threshold
cswap config set autoswitch.threshold 80  # validated: rejects out-of-range values loudly
cswap config set autoswitch.model Fable   # per-model switching (see "auto"); Fable,Opus for several
cswap config unset autoswitch.threshold   # back to the default
cswap config path                         # where settings.json lives
```

`cswap config --help` lists every key with its valid range and default. Hand-editing the file still works — `cswap config` is just a safer front door. `list` and `get` take `--json` for scripting.

</details>

### Backup and migration

Move account data between machines or back it up:

```bash
cswap export backup.cswap                    # All accounts to a file
cswap export backup.cswap --account 2        # One account
cswap export backup.cswap --full             # Include full ~/.claude.json and credential object (same-PC backup)
cswap import backup.cswap                    # Skips accounts that already exist
cswap import backup.cswap --force            # Overwrite existing
```

The export file is plaintext JSON and, by default, carries only each account's own login — machine-shared MCP/plugin OAuth tokens and the device token stay on the source machine (`--full` keeps everything, for same-PC backups). If you need encryption, pipe through your tool of choice (e.g. `cswap export - | gpg -c > backup.gpg`).

If an imported account is the one you're currently logged in as, activate the imported credentials with `cswap switch N --force` (a plain `switch` to the current account is a safe no-op and won't touch the import).

### JSON output for scripting

Add `--json` to `list`, `status`, or `switch` to emit a single machine-readable JSON object on stdout (human-readable notices go to stderr). Useful for scripting auto-swap and quota tracking.

```bash
cswap list --json                   # all accounts with usage/quota
cswap status --json                 # current active account
cswap switch --strategy best --json # switch, then report the result
cswap switch 2 --json
```

<details>
<summary>Example output & schema notes</summary>

```json
{
  "schemaVersion": 1,
  "activeAccountNumber": 2,
  "accounts": [
    { "number": 2, "email": "you@example.com", "active": true, "usageStatus": "ok",
      "usage": { "fiveHour": { "pct": 25.0, "resetsAt": "2026-06-22T23:29:59Z" },
                 "sevenDay": { "pct": 16.0, "resetsAt": "2026-06-26T17:59:59Z" } } }
  ]
}
```

Every payload carries a `schemaVersion` (currently `1`); on a handled error stdout is `{"schemaVersion":1,"error":{...}}` with a non-zero exit code. `--switch`/`--switch-to` report `{"switched": true|false, "from": …, "to": …, "reason": …}`.

Usage is served from a per-account cache: when the usage API is briefly unreachable, the last-known numbers are shown instead of nothing (the human view marks them with their age, e.g. `· 2m ago`). Rows with decision-trusted usage carry additive `usageFetchedAt`/`usageAgeSeconds` fields telling you how old the measurement is. Whenever `usage` is null but a last-known measurement exists — data too old to drive a decision (`usageStatus` stays `unavailable`), or a row in a non-`ok` state such as `token_expired` — additive `lastGoodUsage`/`lastGoodFetchedAt`/`lastGoodAgeSeconds` fields preserve the human display without making the account actionable. When `usage` is null and nothing else explains it (`usageStatus` is `unavailable`), an additive `usageError` names the last fetch failure by kind (e.g. `http-429`, `timeout`) and, while the cache is backing off from it, `usageRetryAt` gives the time of the next attempt. These fields apply to list rows and the managed active row from `status --json`. An account held out of rotation with `cswap disable` carries an additive `"disabled": true` on its row (absent otherwise).

A row carries an additive `loginExpiresAt` (ISO-8601 UTC) when the stored login records when its refresh token expires, which is the moment the slot will need a fresh `/login` and `cswap add --slot N`; a script can warn a few days ahead instead of discovering `relogin_required`. Absent when Claude Code recorded no such date for that login.

An account row also carries an additive `alias` field once one is set with `cswap alias` (e.g. `"alias": "dev"`); accounts without one simply omit the key.

Weekly windows (`sevenDay` and per-model `scoped` entries — never `fiveHour`) additively carry pace fields once the week is ~a day old: `expectedPct` (where usage would sit if spread evenly across the week) and `aheadOfPace` (`true` when meaningfully above that — the same signal the human views show as an `(ahead)`/`(ahead of pace)` marker). `projectedExhaustionAt`/`willLastToReset` extrapolate the current rate into an ETA to 100% and a yes/no "will it last to the reset"; they stay `--json`-only since a linear projection is too rough to present as fact in the UI.

</details>

`cswap auto --json` emits an event *stream* instead — one JSON object per line (`{"schemaVersion":1,"event":"switch","ts":…, …}` with kinds like `poll`, `switch`, `no-switch`, `account-quarantined`, `all-exhausted`, `error`). The contract is additive: new kinds and fields may appear, so scripts should ignore unknown ones.

### Add an account from a raw token or API key

If you only have a long-lived setup-token (e.g., produced by `claude setup-token`)
or a managed API key (`sk-ant-api...`) and you don't want to log in via the browser
flow first — useful on headless servers or when receiving a token from another
machine — register it directly. The token type is auto-detected:

```bash
cswap add-token sk-ant-oat01-...             # OAuth setup-token
cswap add-token sk-ant-api03-...             # managed API key
cswap add-token sk-ant-oat01-... --slot 3
cswap add-token - --slot 3                   # read token from stdin
cswap add-token --email user@example.com     # optional label override
```

`--email` is optional; omitted values use `setup-token-{slot}@token.local`
(or `api-key-{slot}@token.local` for API keys). No Anthropic API calls are made.

**API-key accounts.** An `sk-ant-api...` value registers a managed API-key account
(the kind Claude Code uses after `/login` with a key) rather than an OAuth
setup-token. It switches like any other account; since API keys have no subscription
quota, they show no usage and the usage-aware `switch` strategies never skip them as
rate-limited.

## Uninstall

Remove all data:

```bash
cswap purge
```

Then uninstall the tool:

```bash
uv tool uninstall claude-swap
# or
pipx uninstall claude-swap
```

## Requirements

- Python 3.12+
- Claude Code installed and logged in

## License

MIT
