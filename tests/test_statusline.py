"""Tests for ``cswap statusline`` (the active account under the prompt)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from claude_swap import statusline


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A backup dir with 3 accounts, and a Claude config dir logged in as #1."""
    config = tmp_path / "claude"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    backup = tmp_path / "backup"
    (backup / "cache").mkdir(parents=True)
    (backup / "sequence.json").write_text(json.dumps({
        "sequence": [1, 2, 3],
        "accounts": {
            "1": {"email": "a@example.com"},
            "2": {"email": "b@example.com"},
            "3": {"email": "c@example.com"},
        },
    }))
    login("a@example.com", config)
    usage(backup, {"1": (26, 50), "2": (90, 10), "3": (5, 20)})
    return backup, config


def login(email: str, config: Path) -> None:
    (config / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))


def usage(backup: Path, pcts: dict, dead: tuple = ()) -> None:
    accounts = {
        n: {
            "lastGood": {"five_hour": {"pct": f}, "seven_day": {"pct": s}},
            "authDeadStrikes": 1 if n in dead else 0,
        }
        for n, (f, s) in pcts.items()
    }
    (backup / "cache" / "usage.json").write_text(json.dumps({"schemaVersion": 2, "accounts": accounts}))


class TestRender:
    def test_shows_account_usage_and_next(self, env):
        backup, _ = env
        line = statusline.render({}, backup, colour=False)
        assert line == "⇄ Account-1 a@example.com · 5h 26% · 7d 50% · next Account-3"

    def test_next_skips_near_limit_dead_and_disabled(self, env):
        backup, _ = env
        usage(backup, {"1": (26, 50), "2": (90, 10), "3": (5, 20)}, dead=("3",))
        assert "next" not in statusline.render({}, backup, colour=False)

    def test_disabled_account_is_not_next(self, env):
        backup, _ = env
        seq = json.loads((backup / "sequence.json").read_text())
        seq["accounts"]["3"]["disabled"] = True
        (backup / "sequence.json").write_text(json.dumps(seq))
        assert "next Account-3" not in statusline.render({}, backup, colour=False)

    def test_at_limit_with_no_spare_says_so(self, env):
        backup, _ = env
        usage(backup, {"1": (95, 50), "2": (90, 10), "3": (99, 0)})
        assert statusline.render({}, backup, colour=False).endswith("no spare account")

    def test_live_rate_limits_win_over_the_cache(self, env):
        backup, _ = env
        payload = {"rate_limits": {"five_hour": {"used_percentage": 71}, "seven_day": {"used_percentage": 52}}}
        line = statusline.render(payload, backup, colour=False)
        assert "5h 71%" in line and "7d 52%" in line

    def test_fractional_utilization_is_scaled(self, env):
        backup, _ = env
        payload = {"rate_limits": {"five_hour": {"utilization": 0.4}}}
        assert "5h 40%" in statusline.render(payload, backup, colour=False)

    def test_recent_switch_shows_where_it_came_from(self, env):
        backup, _ = env
        statusline.record_switch(backup, "4", "1")
        line = statusline.render({}, backup, now=time.time() + 90, colour=False)
        assert line.endswith("↻ from Account-4 1m ago")

    def test_old_switch_note_expires(self, env):
        backup, _ = env
        statusline.record_switch(backup, "4", "1")
        line = statusline.render({}, backup, now=time.time() + 3600, colour=False)
        assert "↻" not in line

    def test_switch_note_only_for_the_account_switched_to(self, env):
        backup, _ = env
        statusline.record_switch(backup, "1", "2")
        assert "↻" not in statusline.render({}, backup, colour=False)

    def test_login_not_managed_by_cswap(self, env):
        backup, config = env
        login("stranger@example.com", config)
        assert statusline.render({}, backup, colour=False) == "⇄ stranger@example.com (not in cswap)"

    def test_no_accounts_prints_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
        assert statusline.render({}, tmp_path / "empty", colour=False) == ""

    def test_unknown_usage_shows_question_mark(self, env):
        backup, _ = env
        (backup / "cache" / "usage.json").write_text("{not json")
        assert "5h ?" in statusline.render({}, backup, colour=False)

    def test_record_switch_ignores_no_change(self, env):
        backup, _ = env
        statusline.record_switch(backup, "1", "1")
        statusline.record_switch(backup, None, "1")
        assert not (backup / statusline.LAST_SWITCH_FILENAME).exists()


class TestNextFollowsTheMode:
    def _seq(self):
        return {"sequence": [1, 2, 3, 4], "accounts": {n: {"email": f"{n}@e"} for n in "1234"}}

    def _usage(self):
        # 2: 60% left, 3: 5% left (held out), 4: 90% left
        return {"accounts": {
            "2": {"lastGood": {"five_hour": {"pct": 40}, "seven_day": {"pct": 0}}},
            "3": {"lastGood": {"five_hour": {"pct": 95}, "seven_day": {"pct": 0}}},
            "4": {"lastGood": {"five_hour": {"pct": 10}, "seven_day": {"pct": 0}}},
        }}

    def test_next_available_walks_the_rotation_order(self):
        assert statusline._next_account(self._seq(), self._usage(), "1", "next-available") == "2"

    def test_rotation_order_wraps_and_skips_held_out(self):
        assert statusline._next_account(self._seq(), self._usage(), "2", "next-available") == "4"

    def test_on_limit_picks_the_most_room(self):
        assert statusline._next_account(self._seq(), self._usage(), "1", "on-limit") == "4"


class TestInstall:
    def _settings(self, config: Path) -> dict:
        return json.loads((config / "settings.json").read_text())

    def test_install_keeps_previous_and_uninstall_restores_it(self, env):
        backup, config = env
        orca = {"type": "command", "command": "sh ~/.orca/agent-hooks/claude-statusline.sh"}
        (config / "settings.json").write_text(json.dumps({"model": "opus", "statusLine": orca}))

        statusline.install(backup)
        data = self._settings(config)
        assert statusline.is_ours(data["statusLine"])
        assert data["model"] == "opus"
        assert json.loads((backup / statusline.PREVIOUS_FILENAME).read_text())["statusLine"] == orca

        assert statusline.uninstall(backup) is True
        assert self._settings(config)["statusLine"] == orca
        assert not (backup / statusline.PREVIOUS_FILENAME).exists()

    def test_reinstall_never_saves_itself_as_previous(self, env):
        backup, config = env
        orca = {"type": "command", "command": "orca-statusline"}
        (config / "settings.json").write_text(json.dumps({"statusLine": orca}))
        statusline.install(backup)
        statusline.install(backup)
        assert json.loads((backup / statusline.PREVIOUS_FILENAME).read_text())["statusLine"] == orca

    def test_uninstall_without_previous_removes_the_key(self, env):
        backup, config = env
        (config / "settings.json").write_text("{}")
        statusline.install(backup)
        statusline.uninstall(backup)
        assert "statusLine" not in self._settings(config)

    def test_uninstall_leaves_someone_elses_status_line(self, env):
        backup, config = env
        theirs = {"type": "command", "command": "my-line"}
        (config / "settings.json").write_text(json.dumps({"statusLine": theirs}))
        assert statusline.uninstall(backup) is False
        assert self._settings(config)["statusLine"] == theirs


class TestRun:
    def test_chains_the_previous_line_with_the_same_input(self, env, monkeypatch, capsys):
        backup, _ = env
        monkeypatch.setattr(statusline.paths, "get_backup_root", lambda: backup)
        (backup / statusline.PREVIOUS_FILENAME).write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "read x; echo \"prev:$x\""}}
        ))
        monkeypatch.setattr("sys.stdin", _Stdin('{"session_id":"s"}'))
        monkeypatch.setenv("NO_COLOR", "1")
        assert statusline.run() == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith('prev:{"session_id":"s"} │ ⇄ Account-1')

    def test_silent_previous_line_leaves_only_ours(self, env, monkeypatch, capsys):
        backup, _ = env
        monkeypatch.setattr(statusline.paths, "get_backup_root", lambda: backup)
        (backup / statusline.PREVIOUS_FILENAME).write_text(json.dumps(
            {"statusLine": {"type": "command", "command": "cat >/dev/null"}}
        ))
        monkeypatch.setattr("sys.stdin", _Stdin("{}"))
        monkeypatch.setenv("NO_COLOR", "1")
        statusline.run()
        assert capsys.readouterr().out.startswith("⇄ Account-1")

    def test_broken_input_still_exits_zero(self, env, monkeypatch, capsys):
        backup, _ = env
        monkeypatch.setattr(statusline.paths, "get_backup_root", lambda: backup)
        monkeypatch.setattr("sys.stdin", _Stdin("{not json"))
        assert statusline.run() == 0


class _Stdin:
    def __init__(self, text: str):
        self._text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._text
