"""``cswap statusline``: show the active account under Claude Code's prompt.

Claude Code runs the ``statusLine`` command from ``~/.claude/settings.json``
after each message and prints its first line under the prompt box. This one
shows which account is live, how much of it is used, which account the hook
would move to when it runs out, and — for a few minutes after a switch — which
account it came from::

    ⇄ Account-1 daehyeonnam@g.postech.edu · 5h 26% · 7d 50% · next Account-3

It must be fast and must never fail: it runs on every message, so it reads
only local files (no network, no Keychain) and prints nothing on any error.

There is only one ``statusLine`` slot. Orca, for one, puts a script there that
forwards Claude Code's rate-limit data to its usage panel. Installing saves
whatever was there and this command **chains** it — same stdin in, its output
kept — so the previous status line keeps working; uninstalling restores it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from claude_swap import paths

PREVIOUS_FILENAME = "statusline_previous.json"
LAST_SWITCH_FILENAME = "last_switch.json"
SWITCH_NOTE_SECONDS = 300  # "↻ from Account-4" shows this long after a switch
RESERVE = 15.0  # matches the hook's default: at or below this, "at its limit"
_CHAINED_TIMEOUT = 1.5

_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_RESET = "\033[0m"


# -- reading local state --------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _live_email() -> str | None:
    oauth = _read_json(paths.get_global_config_path()).get("oauthAccount")
    email = oauth.get("emailAddress") if isinstance(oauth, dict) else None
    return email if isinstance(email, str) else None


def _pct(window: object) -> float | None:
    """Utilization of one window, from either cswap's cache or Claude Code."""
    if not isinstance(window, dict):
        return None
    for key in ("pct", "used_percentage", "utilization", "percent_used"):
        value = window.get(key)
        if isinstance(value, (int, float)):
            # Claude Code may report a 0..1 fraction; cswap stores 0..100.
            return float(value) * 100 if key == "utilization" and value <= 1 else float(value)
    return None


def _windows_from_payload(payload: dict) -> tuple[float | None, float | None]:
    """Live 5h/7d usage Claude Code passes in, if this version sends it."""
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return None, None
    five = limits.get("five_hour") or limits.get("fiveHour")
    seven = limits.get("seven_day") or limits.get("sevenDay")
    return _pct(five), _pct(seven)


def _cached_windows(usage: dict, num: str) -> tuple[float | None, float | None, bool]:
    """(5h, 7d, dead) from cswap's usage cache for one slot."""
    entry = (usage.get("accounts") or {}).get(num)
    if not isinstance(entry, dict):
        return None, None, False
    good = entry.get("lastGood") if isinstance(entry.get("lastGood"), dict) else {}
    dead = bool(entry.get("authDeadStrikes"))
    return _pct(good.get("five_hour")), _pct(good.get("seven_day")), dead


def _headroom(five: float | None, seven: float | None) -> float | None:
    known = [p for p in (five, seven) if p is not None]
    return 100.0 - max(known) if known else None


def _next_account(sequence: dict, usage: dict, current: str | None) -> str | None:
    """The account the hook would move to if the current one ran out."""
    best, best_room = None, None
    for raw in sequence.get("sequence") or []:
        num = str(raw)
        record = (sequence.get("accounts") or {}).get(num) or {}
        if num == current or record.get("disabled"):
            continue
        five, seven, dead = _cached_windows(usage, num)
        room = _headroom(five, seven)
        if dead or room is None or room <= RESERVE:
            continue
        if best_room is None or room > best_room:
            best, best_room = num, room
    return best


def _colour(pct: float | None) -> str:
    if pct is None:
        return _DIM
    if pct >= 100 - RESERVE:
        return _RED
    if pct >= 60:
        return _YELLOW
    return _GREEN


def _fmt(label: str, pct: float | None, colour: bool) -> str:
    text = f"{label} {pct:.0f}%" if pct is not None else f"{label} ?"
    return f"{_colour(pct)}{text}{_RESET}" if colour else text


# -- the line -------------------------------------------------------------------


def render(payload: dict, backup_dir: Path, now: float | None = None, colour: bool = True) -> str:
    """Build the status line from local state. Empty string when nothing to show."""
    now = time.time() if now is None else now
    sequence = _read_json(backup_dir / "sequence.json")
    accounts = sequence.get("accounts") or {}
    if not accounts:
        return ""
    usage = _read_json(backup_dir / "cache" / "usage.json")

    email = _live_email()
    current = next((n for n, a in accounts.items() if a.get("email") == email), None)
    if current is None:
        who = f"{email} (not in cswap)" if email else "no login"
        return f"⇄ {who}"

    five, seven = _windows_from_payload(payload)
    if five is None and seven is None:
        five, seven, _dead = _cached_windows(usage, current)

    name = f"Account-{current}"
    parts = [
        (f"{_BOLD}⇄ {name}{_RESET} {email}" if colour else f"⇄ {name} {email}"),
        _fmt("5h", five, colour),
        _fmt("7d", seven, colour),
    ]
    room = _headroom(five, seven)
    nxt = _next_account(sequence, usage, current)
    if nxt:
        parts.append(f"next Account-{nxt}")
    elif room is not None and room <= RESERVE:
        parts.append(f"{_RED}no spare account{_RESET}" if colour else "no spare account")

    last = _read_json(backup_dir / LAST_SWITCH_FILENAME)
    at, came_from, to = last.get("at"), last.get("from"), last.get("to")
    if (
        isinstance(at, (int, float)) and now - at <= SWITCH_NOTE_SECONDS
        and came_from and str(to) == current
    ):
        ago = max(0, int((now - at) // 60))
        note = f"↻ from Account-{came_from} {'just now' if ago == 0 else f'{ago}m ago'}"
        parts.append(f"{_YELLOW}{note}{_RESET}" if colour else note)

    sep = f" {_DIM}·{_RESET} " if colour else " · "
    return sep.join(parts)


def record_switch(backup_dir: Path, came_from: str | None, to: str | None) -> None:
    """Called by the hook after it moved the login, for the "↻ from" note."""
    if not came_from or not to or came_from == to:
        return
    try:
        (backup_dir / LAST_SWITCH_FILENAME).write_text(
            json.dumps({"from": came_from, "to": to, "at": time.time()}), encoding="utf-8"
        )
    except OSError:
        pass


# -- chaining the previous status line -------------------------------------------


def _run_previous(command: str, stdin: str) -> str:
    """Run the status line that was installed before ours, same input."""
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", command], input=stdin, capture_output=True,
            text=True, timeout=_CHAINED_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "").splitlines()[0].strip() if result.stdout else ""


def run() -> int:
    """Entry point Claude Code calls. Always exits 0."""
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}

    backup_dir = paths.get_backup_root()
    theirs = ""
    previous = _read_json(backup_dir / PREVIOUS_FILENAME).get("statusLine")
    if isinstance(previous, dict) and isinstance(previous.get("command"), str):
        theirs = _run_previous(previous["command"], raw)
    try:
        ours = render(payload, backup_dir, colour=os.environ.get("NO_COLOR") is None)
    except Exception:
        ours = ""
    line = " │ ".join(p for p in (theirs, ours) if p)
    if line:
        print(line)
    return 0


# -- install / uninstall -----------------------------------------------------------


def _settings_path() -> Path:
    return paths.get_claude_config_home() / "settings.json"


def _command() -> str:
    exe = shutil.which("cswap")
    return f'"{exe}" statusline' if exe else f'"{sys.executable}" -m claude_swap statusline'


def is_ours(status_line: object) -> bool:
    command = status_line.get("command", "") if isinstance(status_line, dict) else ""
    return "statusline" in command and ("cswap" in command or "claude_swap" in command)


def install(backup_dir: Path | None = None) -> str:
    """Put cswap in the statusLine slot, keeping what was there to chain."""
    from claude_swap.prompt_hook import _read_claude_settings, _write_claude_settings

    backup_dir = backup_dir or paths.get_backup_root()
    path = _settings_path()
    data = _read_claude_settings(path)
    current = data.get("statusLine")
    if current and not is_ours(current):
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / PREVIOUS_FILENAME).write_text(
            json.dumps({"statusLine": current, "savedAt": datetime.now().isoformat()}),
            encoding="utf-8",
        )
    data["statusLine"] = {"type": "command", "command": _command(), "padding": 0}
    _write_claude_settings(path, data)
    return data["statusLine"]["command"]


def uninstall(backup_dir: Path | None = None) -> bool:
    """Restore the status line that was there before cswap's."""
    from claude_swap.prompt_hook import _read_claude_settings, _write_claude_settings

    backup_dir = backup_dir or paths.get_backup_root()
    path = _settings_path()
    data = _read_claude_settings(path)
    if not is_ours(data.get("statusLine")):
        return False
    previous = _read_json(backup_dir / PREVIOUS_FILENAME).get("statusLine")
    if isinstance(previous, dict):
        data["statusLine"] = previous
    else:
        data.pop("statusLine", None)
    _write_claude_settings(path, data)
    try:
        (backup_dir / PREVIOUS_FILENAME).unlink()
    except OSError:
        pass
    return True


def installed() -> bool:
    try:
        from claude_swap.prompt_hook import _read_claude_settings

        return is_ours(_read_claude_settings(_settings_path()).get("statusLine"))
    except Exception:
        return False


def main(argv: list[str]) -> None:
    """Handle ``cswap statusline [install|uninstall|status]``."""
    action = argv[0] if argv else "run"
    if action == "run":
        sys.exit(run())
    if action == "install":
        command = install()
        print(f"Status line installed: {command}")
        print("The active account now shows under the Claude Code prompt.")
    elif action == "uninstall":
        print("Status line removed; the previous one is restored."
              if uninstall() else "cswap's status line isn't installed.")
    elif action == "status":
        print("installed" if installed() else "not installed")
    else:
        print("usage: cswap statusline [install|uninstall|status]", file=sys.stderr)
        sys.exit(2)
