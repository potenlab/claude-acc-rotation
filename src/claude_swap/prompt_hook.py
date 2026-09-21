"""Per-prompt auto-switching via a Claude Code ``UserPromptSubmit`` hook.

``cswap hook`` is what the hook runs: every time a prompt is submitted it
performs one :class:`~claude_swap.autoswitch.AutoSwitchEngine` tick — the same
decision ``cswap auto --once`` makes — so the active account rotates off one
nearing its 5h/7d limit without a background loop. ``cswap hook install``
registers it in Claude Code's user ``settings.json``.

The hook must never get in the way of the prompt it fires for:

- it always exits 0 and never writes plain stdout (for ``UserPromptSubmit``
  plain stdout is injected into the model's context); a switch is reported as
  a ``systemMessage``, which Claude Code shows to the user only;
- it is throttled (``--min-interval``) so rapid prompts, or several sessions
  on one machine, don't each pay for a tick;
- it does nothing inside ``cswap run`` sessions, which are pinned to one
  account by design.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from claude_swap import paths

HOOK_EVENT = "UserPromptSubmit"
HOOK_TIMEOUT_SECONDS = 30
DEFAULT_MIN_INTERVAL = 20.0
STAMP_FILENAME = "prompt_hook_last_run"
_MANAGEMENT_ACTIONS = {"install", "uninstall", "status"}
# Identifies a hook entry we installed, in either launcher form:
# `"/path/to/cswap" hook ...` or `"/path/to/python" -m claude_swap hook ...`.
_OUR_COMMAND = re.compile(
    r"""(?:\bcswap(?:\.exe)?["']?|-m\s+claude_swap)\s+hook\b""", re.IGNORECASE
)


# -- hook entry point ---------------------------------------------------------


def _drain_stdin() -> None:
    """Consume the hook's JSON payload so Claude Code never blocks writing it."""
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except (OSError, ValueError):
        pass


def _in_session_profile(backup_dir: Path) -> bool:
    """True when running under a ``cswap run`` session profile."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if not config_dir:
        return False
    try:
        Path(config_dir).resolve().relative_to((backup_dir / "sessions").resolve())
    except ValueError:
        return False
    return True


def _throttled(stamp: Path, min_interval: float, now: float) -> bool:
    """Return True if the last tick was under ``min_interval`` ago; else stamp."""
    try:
        if now - stamp.stat().st_mtime < min_interval:
            return True
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp, (now, now))
    except OSError:
        pass
    return False


def _system_message(events: list) -> str | None:
    """Summarize the tick's events into one user-facing line, or None."""
    for event in events:
        if event.kind == "switch":
            return f"cswap: {event.human()}"
    for event in events:
        if event.kind == "all-exhausted":
            return f"cswap: {event.human()}"
    return None


def run_hook(args: argparse.Namespace) -> int:
    """Run one throttled auto-switch tick for a submitted prompt."""
    _drain_stdin()
    try:
        from claude_swap.autoswitch import AutoSwitchEngine
        from claude_swap.settings import load_settings, merged_with_cli
        from claude_swap.switcher import ClaudeAccountSwitcher

        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if _in_session_profile(switcher.backup_dir):
            return 0
        if _throttled(
            switcher.backup_dir / STAMP_FILENAME, args.min_interval, time.time()
        ):
            return 0

        events: list = []
        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        engine = AutoSwitchEngine(
            switcher, settings, events.append, dry_run=args.dry_run
        )
        engine.tick()
        message = _system_message(events)
        if message and not args.quiet:
            print(json.dumps({"systemMessage": message}), flush=True)
    except Exception as e:  # never break the user's prompt
        if args.debug:
            print(f"cswap hook: {e}", file=sys.stderr)
    return 0


# -- install / uninstall ------------------------------------------------------


def claude_settings_path() -> Path:
    return paths.get_claude_config_home() / "settings.json"


def hook_command(extra: list[str]) -> str:
    """The command line Claude Code should run, resolved to an absolute path."""
    exe = shutil.which("cswap")
    base = f'"{exe}" hook' if exe else f'"{sys.executable}" -m claude_swap hook'
    return " ".join([base, *extra])


def _is_ours(hook: object) -> bool:
    command = hook.get("command", "") if isinstance(hook, dict) else ""
    return bool(_OUR_COMMAND.search(command))


def _read_claude_settings(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_claude_settings(path: Path, data: dict) -> None:
    # Write through a symlinked settings.json (dotfiles) instead of replacing it.
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".cswap-tmp")
    try:
        mode = target.stat().st_mode & 0o777  # settings.json may hold secrets
    except FileNotFoundError:
        mode = 0o600
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, target)


def _strip_ours(groups: list) -> list:
    """Remove our hook entries, dropping matcher groups left empty."""
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        remaining = [h for h in group["hooks"] if not _is_ours(h)]
        if remaining or not group["hooks"]:
            kept.append({**group, "hooks": remaining})
    return kept


def install(path: Path, command: str) -> None:
    data = _read_claude_settings(path)
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ValueError(f'"hooks" in {path} is not a JSON object')
    groups = hooks.get(HOOK_EVENT) or []
    if not isinstance(groups, list):
        raise ValueError(f'"hooks.{HOOK_EVENT}" in {path} is not a list')
    groups = _strip_ours(groups)
    groups.append(
        {
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                    "timeout": HOOK_TIMEOUT_SECONDS,
                }
            ]
        }
    )
    hooks[HOOK_EVENT] = groups
    _write_claude_settings(path, data)


def uninstall(path: Path) -> bool:
    data = _read_claude_settings(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict) or HOOK_EVENT not in hooks:
        return False
    before = hooks[HOOK_EVENT]
    if not isinstance(before, list):
        return False
    after = _strip_ours(before)
    if after == before:
        return False
    if after:
        hooks[HOOK_EVENT] = after
    else:
        del hooks[HOOK_EVENT]
        if not hooks:
            del data["hooks"]
    _write_claude_settings(path, data)
    return True


def installed_command(path: Path) -> str | None:
    try:
        data = _read_claude_settings(path)
    except (OSError, ValueError):
        return None
    for group in (data.get("hooks") or {}).get(HOOK_EVENT, []):
        for hook in (group or {}).get("hooks", []) if isinstance(group, dict) else []:
            if _is_ours(hook):
                return hook.get("command")
    return None


# -- CLI ----------------------------------------------------------------------


def _add_tick_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help="Switch when the active account reaches this utilization (default: autoswitch.threshold)",
    )
    parser.add_argument(
        "--strategy",
        choices=("best", "consume-first"),
        default=None,
        help="Target selection (default: autoswitch.strategy)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help="Also switch on these per-model weekly limits (e.g. Fable, 'Fable,Opus', all)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default: autoswitch.cooldown)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL,
        metavar="SECONDS",
        help=f"Skip the check if one ran less than this long ago (default {DEFAULT_MIN_INTERVAL:.0f})",
    )


def _format_value(value: object) -> str:
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _forwarded_options(args: argparse.Namespace) -> list[str]:
    """Re-serialize tick options so the installed command carries them."""
    out: list[str] = []
    for flag, value in (
        ("--threshold", args.threshold),
        ("--strategy", args.strategy),
        ("--model", args.model),
        ("--cooldown", args.cooldown),
    ):
        if value is not None:
            # `--flag=value` so a value starting with "-" can't be misparsed.
            out.append(shlex.quote(f"{flag}={_format_value(value)}"))
    if args.min_interval != DEFAULT_MIN_INTERVAL:
        out.append(f"--min-interval={_format_value(args.min_interval)}")
    if args.quiet:
        out.append("--quiet")
    return out


def hook_command_main(argv: list[str]) -> None:
    """Handle ``cswap hook [install|uninstall|status] [options]``."""
    parser = argparse.ArgumentParser(
        prog="cswap hook",
        description=(
            "Check usage and maybe switch accounts on every prompt, via a "
            "Claude Code UserPromptSubmit hook. With no action, runs the check "
            "(this is what the installed hook calls)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cswap hook install                   # check on every prompt, switch at 90%%
  cswap hook install --threshold 80    # switch earlier
  cswap hook install --strategy consume-first
  cswap hook status
  cswap hook uninstall
  cswap hook --dry-run --min-interval 0 < /dev/null   # try one check by hand
        """,
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("run", "install", "uninstall", "status"),
        default="run",
    )
    _add_tick_options(parser)
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Don't show a message in Claude Code when a switch happens",
    )
    parser.add_argument("--dry-run", action="store_true", help="Decide but never switch")
    parser.add_argument("--debug", action="store_true", help="Print errors to stderr")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # Exit 2 from a UserPromptSubmit hook blocks the prompt. A stale or bad
        # flag in the installed command must only skip the check, never that.
        if e.code not in (0, None) and not _MANAGEMENT_ACTIONS & set(argv):
            sys.exit(0)
        raise

    if args.action == "run":
        sys.exit(run_hook(args))

    path = claude_settings_path()
    try:
        if args.action == "install":
            command = hook_command(_forwarded_options(args))
            install(path, command)
            print(f"Installed {HOOK_EVENT} hook in {path}")
            print(f"  {command}")
            print("New prompts in Claude Code now check usage and switch accounts when needed.")
        elif args.action == "uninstall":
            if uninstall(path):
                print(f"Removed the cswap hook from {path}")
            else:
                print(f"No cswap hook found in {path}")
        else:
            command = installed_command(path)
            if command:
                print(f"Installed ({path}):\n  {command}")
            else:
                print(f"Not installed ({path}). Run: cswap hook install")
    except (OSError, ValueError) as e:
        print(f"Error: could not update {path}: {e}", file=sys.stderr)
        sys.exit(1)
