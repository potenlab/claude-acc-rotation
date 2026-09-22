"""Tests for the per-prompt UserPromptSubmit hook (``cswap hook``)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude_swap import prompt_hook
from claude_swap.autoswitch import AllExhaustedEvent, NoSwitchEvent, SwitchEvent


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        threshold=None,
        strategy=None,
        model=None,
        cooldown=None,
        rotate="threshold",
        skip_path=[],
        sync_orca=False,
        detach_orca=False,
        no_detach_orca=True,
        reserve=None,
        force=True,
        min_interval=prompt_hook.DEFAULT_MIN_INTERVAL,
        quiet=False,
        dry_run=False,
        debug=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _switch_event() -> SwitchEvent:
    return SwitchEvent(
        trigger="proactive",
        from_ref={"number": "1", "email": "a@example.com"},
        to_ref={"number": "2", "email": "b@example.com"},
    )


# -- install / uninstall --------------------------------------------------------


class TestInstall:
    def test_install_into_missing_file(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        prompt_hook.install(path, '"/bin/cswap" hook')
        data = json.loads(path.read_text())
        [group] = data["hooks"]["UserPromptSubmit"]
        assert group["hooks"][0]["command"] == '"/bin/cswap" hook'
        assert group["hooks"][0]["type"] == "command"

    def test_install_preserves_other_settings_and_hooks(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        other = {"type": "command", "command": "echo hi"}
        path.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {
                        "UserPromptSubmit": [{"hooks": [other]}],
                        "Stop": [{"hooks": [{"type": "command", "command": "x"}]}],
                    },
                }
            )
        )
        prompt_hook.install(path, '"/bin/cswap" hook')
        data = json.loads(path.read_text())
        assert data["model"] == "opus"
        assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "x"
        groups = data["hooks"]["UserPromptSubmit"]
        assert groups[0]["hooks"] == [other]
        assert prompt_hook.installed_command(path) == '"/bin/cswap" hook'

    def test_reinstall_replaces_instead_of_duplicating(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        prompt_hook.install(path, '"/bin/cswap" hook')
        prompt_hook.install(path, '"/bin/cswap" hook --threshold 80')
        groups = json.loads(path.read_text())["hooks"]["UserPromptSubmit"]
        assert len(groups) == 1
        assert groups[0]["hooks"][0]["command"].endswith("--threshold 80")

    def test_uninstall_removes_only_ours(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        other = {"type": "command", "command": "echo hi"}
        path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [other]}]}}))
        prompt_hook.install(path, '"/bin/cswap" hook')
        assert prompt_hook.uninstall(path) is True
        data = json.loads(path.read_text())
        assert data["hooks"]["UserPromptSubmit"] == [{"hooks": [other]}]
        assert prompt_hook.uninstall(path) is False

    def test_uninstall_cleans_up_empty_hooks(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"model": "opus"}))
        prompt_hook.install(path, '"/py" -m claude_swap hook')
        assert prompt_hook.uninstall(path) is True
        assert json.loads(path.read_text()) == {"model": "opus"}

    def test_install_writes_through_symlink(self, tmp_path: Path):
        real = tmp_path / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text("{}")
        link = tmp_path / "settings.json"
        link.symlink_to(real)
        prompt_hook.install(link, '"/bin/cswap" hook')
        assert link.is_symlink()
        assert prompt_hook.installed_command(real) == '"/bin/cswap" hook'

    def test_install_refuses_invalid_json(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text("{not json")
        with pytest.raises(ValueError):
            prompt_hook.install(path, '"/bin/cswap" hook')
        assert path.read_text() == "{not json"

    def test_forwarded_options_round_trip(self):
        opts = prompt_hook._forwarded_options(
            _args(rotate=None, no_detach_orca=False, min_interval=0, threshold=80.0,
                  strategy="consume-first", model="Fable,Opus", quiet=True)
        )
        assert opts == [
            "--threshold=80",
            "--strategy=consume-first",
            "--model=Fable,Opus",
            "--quiet",
        ]

    def test_forwarded_options_keep_precision_and_dash_values(self):
        opts = prompt_hook._forwarded_options(
            _args(rotate=None, no_detach_orca=False, min_interval=0, cooldown=1234567.0, model="-foo")
        )
        assert opts == ["--model=-foo", "--cooldown=1234567"]

    def test_windows_upper_case_exe_is_recognised(self):
        assert prompt_hook._is_ours({"command": r'"C:\x\cswap.EXE" hook'})
        assert prompt_hook._is_ours({"command": '"/py" -m claude_swap hook'})
        assert not prompt_hook._is_ours({"command": "echo cswap hooked"})

    def test_install_keeps_file_mode(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text("{}")
        path.chmod(0o600)
        prompt_hook.install(path, '"/bin/cswap" hook')
        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize(
        "content", ['{"hooks": []}', '{"hooks": {"UserPromptSubmit": {"a": 1}}}']
    )
    def test_install_refuses_unexpected_shapes(self, tmp_path: Path, content):
        path = tmp_path / "settings.json"
        path.write_text(content)
        with pytest.raises(ValueError):
            prompt_hook.install(path, '"/bin/cswap" hook')
        assert path.read_text() == content

    def test_install_accepts_null_hooks(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        path.write_text('{"hooks": null}')
        prompt_hook.install(path, '"/bin/cswap" hook')
        assert prompt_hook.installed_command(path) == '"/bin/cswap" hook'


# -- running the hook -----------------------------------------------------------


class TestRunHook:
    @pytest.fixture
    def backup_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "backup"

    def _run(self, backup_dir: Path, events: list, args=None, env=None, switch_result=None, payload=None):
        switcher = MagicMock()
        switcher.switch.return_value = switch_result
        switcher.backup_dir = backup_dir
        engine = MagicMock()

        def fake_engine(_switcher, _settings, on_event, **_kw):
            engine.tick.side_effect = lambda: [on_event(e) for e in events]
            return engine

        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch("claude_swap.autoswitch.AutoSwitchEngine", side_effect=fake_engine), \
             patch.dict("os.environ", env or {}, clear=False), \
             patch.object(prompt_hook, "_read_payload", return_value=payload or {}):
            code = prompt_hook.run_hook(args or _args())
        return code, engine, switcher

    def test_switch_is_reported_as_system_message(self, backup_dir, capsys):
        code, engine, _ = self._run(backup_dir, [_switch_event()])
        assert code == 0
        engine.tick.assert_called_once()
        out = json.loads(capsys.readouterr().out)
        assert "Account-2" in out["systemMessage"]

    def test_no_switch_prints_nothing(self, backup_dir, capsys):
        code, _, _ = self._run(backup_dir, [NoSwitchEvent(reason="below-threshold")])
        assert code == 0
        assert capsys.readouterr().out == ""

    def test_all_exhausted_is_reported(self, backup_dir, capsys):
        self._run(backup_dir, [AllExhaustedEvent(earliest_reset_at=None)])
        assert "exhausted" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_quiet_suppresses_message(self, backup_dir, capsys):
        self._run(backup_dir, [_switch_event()], args=_args(quiet=True))
        assert capsys.readouterr().out == ""

    def test_throttled_second_prompt_skips_tick(self, backup_dir):
        _, first, _ = self._run(backup_dir, [])
        _, second, _ = self._run(backup_dir, [])
        first.tick.assert_called_once()
        second.tick.assert_not_called()

    def test_min_interval_zero_never_throttles(self, backup_dir):
        self._run(backup_dir, [], args=_args(min_interval=0))
        _, second, _ = self._run(backup_dir, [], args=_args(min_interval=0))
        second.tick.assert_called_once()

    def test_skips_inside_session_profile(self, backup_dir):
        session = backup_dir / "sessions" / "2"
        session.mkdir(parents=True)
        _, engine, _ = self._run(backup_dir, [], env={"CLAUDE_CONFIG_DIR": str(session)})
        engine.tick.assert_not_called()

    def test_engine_crash_still_exits_zero(self, backup_dir, capsys):
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch("claude_swap.autoswitch.AutoSwitchEngine", side_effect=RuntimeError("boom")), \
             patch.object(prompt_hook, "_read_payload", return_value={}):
            assert prompt_hook.run_hook(_args()) == 0
        assert capsys.readouterr().out == ""


class TestRotateMode(TestRunHook):
    """--rotate: switch on every prompt, no threshold."""

    def test_rotate_switches_and_reports(self, backup_dir, capsys):
        result = {
            "switched": True,
            "message": "Switched to Account-2 (b@example.com)",
        }
        _, engine, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0),
            switch_result=result,
        )
        engine.tick.assert_not_called()
        switcher.switch.assert_called_once_with(
            strategy="next-available", json_output=True, reserve=15.0
        )
        assert "Account-2" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_rotate_passes_custom_reserve(self, backup_dir):
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0, reserve=15.0),
            switch_result={"switched": True, "message": "Switched to Account-3 (c@e)"},
        )
        switcher.switch.assert_called_once_with(
            strategy="next-available", json_output=True, reserve=15.0
        )

    def test_all_accounts_exhausted_is_reported(self, backup_dir, capsys):
        result = {
            "switched": False,
            "reason": "candidates-exhausted",
            "message": "All other accounts are at their 5h/7d limit — staying on Account-1.",
        }
        self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0),
            switch_result=result,
        )
        msg = json.loads(capsys.readouterr().out)["systemMessage"]
        assert "staying on Account-1" in msg

    def test_rotate_plain_passes_no_strategy(self, backup_dir):
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="plain", min_interval=0),
            switch_result={"switched": True, "message": "Switched to Account-2 (b@e)"},
        )
        switcher.switch.assert_called_once_with(strategy=None, json_output=True)

    def test_rotate_noop_prints_nothing(self, backup_dir, capsys):
        self._run(
            backup_dir, [], args=_args(rotate="best", min_interval=0),
            switch_result={"switched": False, "message": "Already on Account-1"},
        )
        assert capsys.readouterr().out == ""

    def test_rotate_dry_run_never_switches(self, backup_dir, capsys):
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="best", min_interval=0, dry_run=True),
        )
        switcher.switch.assert_not_called()
        assert "would rotate" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_rotate_failure_still_exits_zero(self, backup_dir, capsys):
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        switcher.switch.side_effect = RuntimeError("locked")
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch.object(prompt_hook, "_read_payload", return_value={}):
            assert prompt_hook.run_hook(_args(rotate="best", min_interval=0)) == 0
        assert capsys.readouterr().out == ""


class TestMinIntervalDefaults:
    def test_rotate_defaults_to_no_throttle(self):
        args = _args(rotate="next-available", min_interval=None)
        prompt_hook._resolve_min_interval(args)
        assert args.min_interval == 0

    def test_tick_mode_keeps_default_throttle(self):
        args = _args(min_interval=None)
        prompt_hook._resolve_min_interval(args)
        assert args.min_interval == prompt_hook.DEFAULT_MIN_INTERVAL

    def test_rotate_forwards_without_redundant_min_interval(self):
        opts = prompt_hook._forwarded_options(_args(no_detach_orca=False, rotate="best", min_interval=0))
        assert opts == ["--rotate=best"]

    def test_unset_reserve_is_not_forwarded(self):
        """No flag -> the installed command stays free of it, so hook.reserve
        (read at every run) governs already-open sessions too."""
        assert prompt_hook._forwarded_options(_args(no_detach_orca=False, rotate="next-available", min_interval=0)) == []

    def test_custom_reserve_is_forwarded(self):
        opts = prompt_hook._forwarded_options(
            _args(no_detach_orca=False, rotate="next-available", min_interval=0, reserve=10.0)
        )
        assert opts == ["--reserve=10"]

    def test_explicit_min_interval_is_forwarded(self):
        opts = prompt_hook._forwarded_options(_args(no_detach_orca=False, rotate="best", min_interval=5.0))
        assert opts == ["--rotate=best", "--min-interval=5"]


class TestOptOut(TestRunHook):
    """Ways a launcher (Orca, CI, a pinned shell) opts out of rotation."""

    def test_env_kill_switch_skips_everything(self, backup_dir):
        _, engine, switcher = self._run(
            backup_dir, [], args=_args(rotate="best", min_interval=0),
            env={"CSWAP_HOOK_DISABLE": "1"},
        )
        engine.tick.assert_not_called()
        switcher.switch.assert_not_called()

    @pytest.mark.parametrize("value", ["0", "", "no"])
    def test_env_other_values_do_not_disable(self, backup_dir, value):
        _, engine, _ = self._run(
            backup_dir, [], args=_args(min_interval=0),
            env={"CSWAP_HOOK_DISABLE": value},
        )
        engine.tick.assert_called_once()

    def test_skip_path_matches_subdirectory(self, backup_dir, tmp_path):
        orca = tmp_path / "orca"
        (orca / "workspaces" / "app").mkdir(parents=True)
        _, engine, _ = self._run(
            backup_dir, [], args=_args(min_interval=0, skip_path=[str(orca)]),
            payload={"cwd": str(orca / "workspaces" / "app")},
        )
        engine.tick.assert_not_called()

    def test_skip_path_leaves_other_dirs_alone(self, backup_dir, tmp_path):
        (tmp_path / "orca").mkdir()
        (tmp_path / "work").mkdir()
        _, engine, _ = self._run(
            backup_dir, [], args=_args(min_interval=0, skip_path=[str(tmp_path / "orca")]),
            payload={"cwd": str(tmp_path / "work")},
        )
        engine.tick.assert_called_once()

    def test_malformed_payload_is_ignored(self, backup_dir):
        _, engine, _ = self._run(
            backup_dir, [], args=_args(min_interval=0, skip_path=["~/orca"]),
            payload={"cwd": None},
        )
        engine.tick.assert_called_once()

    def test_skip_path_is_forwarded(self):
        opts = prompt_hook._forwarded_options(
            _args(rotate=None, no_detach_orca=False, min_interval=0, skip_path=["~/orca", "/tmp/x"])
        )
        # ~ is quoted: the hook expands it itself, so the shell must not.
        assert opts == ["'--skip-path=~/orca'", "--skip-path=/tmp/x"]


def test_read_payload_survives_garbage(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert prompt_hook._read_payload() == {}


class TestReserveFromSettings(TestRunHook):
    """hook.reserve is read at every run, so an already-open Claude Code
    session (which captured its command line at startup) follows it."""

    def _write_setting(self, backup_dir: Path, value) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "settings.json").write_text(json.dumps({"hook": {"reserve": value}}))

    def test_settings_value_is_used_when_flag_absent(self, backup_dir):
        self._write_setting(backup_dir, 15)
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0),
            switch_result={"switched": False},
        )
        assert switcher.switch.call_args.kwargs["reserve"] == 15.0

    def test_flag_beats_the_setting(self, backup_dir):
        self._write_setting(backup_dir, 15)
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0, reserve=3.0),
            switch_result={"switched": False},
        )
        assert switcher.switch.call_args.kwargs["reserve"] == 3.0

    def test_missing_settings_file_falls_back_to_default(self, backup_dir):
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0),
            switch_result={"switched": False},
        )
        assert switcher.switch.call_args.kwargs["reserve"] == prompt_hook.DEFAULT_RESERVE

    def test_corrupt_settings_file_falls_back_to_default(self, backup_dir):
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "settings.json").write_text("{not json")
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0),
            switch_result={"switched": False},
        )
        assert switcher.switch.call_args.kwargs["reserve"] == prompt_hook.DEFAULT_RESERVE


class TestUninstalledMeansOff(TestRunHook):
    """An open session keeps its captured hook command after uninstall; the
    command must notice it was removed and do nothing."""

    def test_not_installed_skips_everything(self, backup_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_hook, "claude_settings_path", lambda: tmp_path / "none.json")
        _, engine, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0, force=False),
        )
        engine.tick.assert_not_called()
        switcher.switch.assert_not_called()

    def test_installed_runs_normally(self, backup_dir, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        prompt_hook.install(settings, '"/bin/cswap" hook --rotate')
        monkeypatch.setattr(prompt_hook, "claude_settings_path", lambda: settings)
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0, force=False),
            switch_result={"switched": False},
        )
        switcher.switch.assert_called_once()

    def test_uninstall_stops_a_running_command(self, backup_dir, tmp_path, monkeypatch):
        settings = tmp_path / "settings.json"
        prompt_hook.install(settings, '"/bin/cswap" hook --rotate')
        prompt_hook.uninstall(settings)
        monkeypatch.setattr(prompt_hook, "claude_settings_path", lambda: settings)
        _, _, switcher = self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0, force=False),
        )
        switcher.switch.assert_not_called()


def _u(pct: float) -> dict:
    return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}


class TestPickOnLimit:
    """--rotate=on-limit: check every prompt, switch only at the limit."""

    def test_stays_while_current_has_room(self):
        usage = {"1": _u(40), "2": _u(0)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2"], 15) == (None, "stay")

    def test_switches_at_the_limit_to_most_room(self):
        usage = {"1": _u(90), "2": _u(60), "3": _u(10)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2", "3"], 15) == ("3", "limit")

    def test_never_lands_on_a_near_limit_account(self):
        usage = {"1": _u(100), "2": _u(90), "3": _u(97)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2", "3"], 15) == (None, "exhausted")

    def test_skips_dead_and_unknown_candidates(self):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        usage = {"1": _u(100), "2": USAGE_RELOGIN_REQUIRED, "3": None, "4": _u(50)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2", "3", "4"], 15) == ("4", "limit")

    def test_dead_current_login_moves_even_with_quota(self):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        usage = {"1": USAGE_RELOGIN_REQUIRED, "2": _u(20)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2"], 15) == ("2", "dead")

    def test_unknown_current_usage_never_acts(self):
        usage = {"1": None, "2": _u(0)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2"], 15) == (None, "unknown")

    def test_exactly_at_reserve_counts_as_limit(self):
        usage = {"1": _u(85), "2": _u(0)}
        assert prompt_hook.pick_on_limit(usage, "1", ["2"], 15) == ("2", "limit")

    def test_rotate_on_limit_reports_the_switch(self):
        sw = MagicMock()
        sw.current_account_number.return_value = "1"
        sw._get_sequence_data.return_value = {"sequence": [1, 2]}
        sw._disabled_from_data.return_value = False
        sw._account_is_switchable.return_value = True
        sw._usage_by_account.return_value = {"1": _u(95), "2": _u(10)}
        sw.switch_to.return_value = {"switched": True, "message": "Switched to Account-2 (b@e)"}
        msg = prompt_hook._rotate(sw, "on-limit", 15)
        sw.switch_to.assert_called_once_with("2", json_output=True)
        assert msg == "cswap: Account-1 is at its limit — Switched to Account-2 (b@e)"

    def test_rotate_on_limit_is_silent_while_there_is_room(self):
        sw = MagicMock()
        sw.current_account_number.return_value = "1"
        sw._get_sequence_data.return_value = {"sequence": [1, 2]}
        sw._disabled_from_data.return_value = False
        sw._account_is_switchable.return_value = True
        sw._usage_by_account.return_value = {"1": _u(30), "2": _u(0)}
        assert prompt_hook._rotate(sw, "on-limit", 15) is None
        sw.switch_to.assert_not_called()

    def test_disabled_accounts_are_not_candidates(self):
        sw = MagicMock()
        sw.current_account_number.return_value = "1"
        sw._get_sequence_data.return_value = {"sequence": [1, 2]}
        sw._disabled_from_data.side_effect = lambda data, n: n == "2"
        sw._account_is_switchable.return_value = True
        sw._usage_by_account.return_value = {"1": _u(95), "2": _u(0)}
        msg = prompt_hook._rotate(sw, "on-limit", 15)
        sw.switch_to.assert_not_called()
        assert "no other account has room" in msg

    def test_bare_rotate_means_the_default_mode(self):
        seen = {}
        with patch.object(prompt_hook, "run_hook", side_effect=lambda a: seen.setdefault("rotate", a.rotate) and 0):
            with pytest.raises(SystemExit):
                prompt_hook.hook_command_main(["--rotate"])
        assert seen["rotate"] == prompt_hook.DEFAULT_MODE == "next-available"


class TestNoticeRepeats:
    def test_same_notice_is_shown_once_per_window(self, tmp_path):
        msg = "cswap: All other accounts are at their limit — staying on Account-1."
        assert prompt_hook._fresh_notice(tmp_path, msg, now=1000) is True
        assert prompt_hook._fresh_notice(tmp_path, msg, now=1060) is False
        assert prompt_hook._fresh_notice(tmp_path, msg, now=1000 + prompt_hook.NOTICE_REPEAT_SECONDS) is True

    def test_a_different_notice_is_shown_right_away(self, tmp_path):
        assert prompt_hook._fresh_notice(tmp_path, "staying on Account-1", now=1000) is True
        assert prompt_hook._fresh_notice(tmp_path, "staying on Account-3", now=1001) is True

    def test_exhausted_rotation_is_quiet_the_second_time(self, tmp_path):
        sw = MagicMock()
        sw.backup_dir = tmp_path
        sw.switch.return_value = {"switched": False, "reason": "candidates-exhausted",
                                  "message": "All other accounts are at their limit — staying on Account-1."}
        assert prompt_hook._rotate(sw, "next-available", 15) is not None
        assert prompt_hook._rotate(sw, "next-available", 15) is None

    def test_a_real_switch_is_always_reported(self, tmp_path):
        sw = MagicMock()
        sw.backup_dir = tmp_path
        sw.switch.return_value = {"switched": True, "message": "Switched to Account-2 (b@e)"}
        assert prompt_hook._rotate(sw, "next-available", 15)
        assert prompt_hook._rotate(sw, "next-available", 15)


class TestFirstPromptLoginCheck:
    """The first prompt of each session must not go out on a revoked login."""

    def test_session_is_first_only_once(self, tmp_path):
        assert prompt_hook._is_first_prompt(tmp_path, "sess-1") is True
        assert prompt_hook._is_first_prompt(tmp_path, "sess-1") is False
        assert prompt_hook._is_first_prompt(tmp_path, "sess-2") is True

    def test_missing_session_id_is_never_first(self, tmp_path):
        assert prompt_hook._is_first_prompt(tmp_path, None) is False
        assert prompt_hook._is_first_prompt(tmp_path, "") is False

    def test_corrupt_session_file_recovers(self, tmp_path):
        (tmp_path / prompt_hook.SESSIONS_FILENAME).write_text("{not json")
        assert prompt_hook._is_first_prompt(tmp_path, "sess-1") is True

    def test_session_memory_is_bounded(self, tmp_path):
        for i in range(prompt_hook._SESSIONS_KEPT + 50):
            prompt_hook._is_first_prompt(tmp_path, f"s{i}")
        seen = json.loads((tmp_path / prompt_hook.SESSIONS_FILENAME).read_text())
        assert len(seen) == prompt_hook._SESSIONS_KEPT

    def test_revoked_login_moves_to_a_usable_account(self):
        sw = MagicMock()
        sw.current_account_number.return_value = "2"
        sw.switch.return_value = {"switched": True, "message": "Switched to Account-3 (c@e)"}
        with patch.object(prompt_hook, "_live_login_verdict", return_value="revoked"):
            note = prompt_hook._ensure_usable_login(_args(), sw)
        assert note == "Account-2's login was revoked — Switched to Account-3 (c@e)"
        assert sw.switch.call_args.kwargs["strategy"] == "next-available"

    def test_revoked_with_nowhere_to_go_says_run_login(self):
        sw = MagicMock()
        sw.current_account_number.return_value = "2"
        sw.switch.return_value = {"switched": False, "reason": "relogin-required"}
        with patch.object(prompt_hook, "_live_login_verdict", return_value="revoked"):
            note = prompt_hook._ensure_usable_login(_args(), sw)
        assert "run /login" in note

    @pytest.mark.parametrize("verdict", ["ok", "expired", "unknown"])
    def test_only_revoked_triggers_a_switch(self, verdict):
        sw = MagicMock()
        with patch.object(prompt_hook, "_live_login_verdict", return_value=verdict):
            assert prompt_hook._ensure_usable_login(_args(), sw) is None
        sw.switch.assert_not_called()

    def test_first_prompt_check_runs_even_when_throttled(self, tmp_path, capsys):
        backup = tmp_path / "backup"
        backup.mkdir()
        (backup / prompt_hook.STAMP_FILENAME).touch()  # just ran: throttled
        sw = MagicMock()
        sw.backup_dir = backup
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=sw), \
             patch.object(prompt_hook, "_read_payload", return_value={"session_id": "new"}), \
             patch.object(prompt_hook, "_ensure_usable_login", return_value="Account-2's login was revoked — Switched to Account-3 (c@e)") as ensure:
            prompt_hook.run_hook(_args(min_interval=999))
        ensure.assert_called_once()
        assert "revoked" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_after_moving_off_a_dead_login_no_second_switch(self, tmp_path):
        backup = tmp_path / "backup"
        sw = MagicMock()
        sw.backup_dir = backup
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=sw), \
             patch.object(prompt_hook, "_read_payload", return_value={"session_id": "new"}), \
             patch.object(prompt_hook, "_ensure_usable_login", return_value="moved"):
            prompt_hook.run_hook(_args(rotate="next-available", min_interval=0))
        sw.switch.assert_not_called()  # _ensure_usable_login did the only switch


class TestProbeAccessToken:
    def _creds(self, expires_in_ms: int) -> str:
        import time as _t

        return json.dumps({"claudeAiOauth": {
            "accessToken": "tok", "refreshToken": "rt",
            "expiresAt": int(_t.time() * 1000) + expires_in_ms,
        }})

    def test_ok(self):
        from claude_swap import oauth

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value = MagicMock()
            assert oauth.probe_access_token(self._creds(3_600_000)) == "ok"

    def test_401_within_expiry_is_revoked(self):
        import urllib.error

        from claude_swap import oauth

        err = urllib.error.HTTPError("u", 401, "revoked", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            assert oauth.probe_access_token(self._creds(3_600_000)) == "revoked"

    def test_expired_token_is_not_asked(self):
        from claude_swap import oauth

        with patch("urllib.request.urlopen") as urlopen:
            assert oauth.probe_access_token(self._creds(-60_000)) == "expired"
        urlopen.assert_not_called()

    def test_network_error_is_unknown(self):
        from claude_swap import oauth

        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            assert oauth.probe_access_token(self._creds(3_600_000)) == "unknown"

    def test_other_http_status_is_unknown(self):
        import urllib.error

        from claude_swap import oauth

        err = urllib.error.HTTPError("u", 500, "oops", {}, None)
        with patch("urllib.request.urlopen", side_effect=err):
            assert oauth.probe_access_token(self._creds(3_600_000)) == "unknown"

    def test_no_token_is_unknown(self):
        from claude_swap import oauth

        assert oauth.probe_access_token("{}") == "unknown"


class TestHealAfterLogin:
    """A /login over a dead backup is saved automatically."""

    def _switcher(self, sentinel):
        sw = MagicMock()
        sw.current_account_number.return_value = "2"
        sw._usage_by_account.return_value = {"2": sentinel}
        return sw

    def test_dead_backup_with_live_login_is_saved(self):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        sw = self._switcher(USAGE_RELOGIN_REQUIRED)
        with patch.object(prompt_hook, "_live_login_verdict", return_value="ok"):
            assert prompt_hook._heal_current_account(sw) == "saved your new login for Account-2"
        sw.add_account.assert_called_once_with(assume_yes=True)

    def test_revoked_live_login_is_not_saved(self):
        """Saving a dead login over a dead backup would falsely claim success."""
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        sw = self._switcher(USAGE_RELOGIN_REQUIRED)
        with patch.object(prompt_hook, "_live_login_verdict", return_value="revoked"):
            assert prompt_hook._heal_current_account(sw) is None
        sw.add_account.assert_not_called()

    def test_healthy_account_is_left_alone(self):
        sw = self._switcher({"five_hour": {"pct": 10}})
        assert prompt_hook._heal_current_account(sw) is None
        sw.add_account.assert_not_called()

    def test_unmanaged_login_is_left_alone(self):
        sw = MagicMock()
        sw.current_account_number.return_value = None
        assert prompt_hook._heal_current_account(sw) is None
        sw.add_account.assert_not_called()

    def test_add_output_never_reaches_stdout(self, capsys):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        sw = self._switcher(USAGE_RELOGIN_REQUIRED)
        sw.add_account.side_effect = lambda **kw: print("Added Account 2: b@example.com")
        with patch.object(prompt_hook, "_live_login_verdict", return_value="ok"):
            prompt_hook._heal_current_account(sw)
        assert capsys.readouterr().out == ""

    def test_add_failure_is_silent(self):
        from claude_swap.json_output import USAGE_RELOGIN_REQUIRED

        sw = self._switcher(USAGE_RELOGIN_REQUIRED)
        sw.add_account.side_effect = RuntimeError("keychain locked")
        with patch.object(prompt_hook, "_live_login_verdict", return_value="ok"):
            assert prompt_hook._heal_current_account(sw) is None


class TestKeepOrcaDetached(TestRunHook):
    """--detach-orca (and the old --sync-orca) keep Orca off the Claude login."""

    def test_detach_runs_before_the_switch(self, backup_dir):
        order = []
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        switcher.switch.side_effect = lambda **kw: order.append("switch") or {
            "switched": True, "message": "Switched to Account-2 (b@e)"}
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch("claude_swap.orca.keep_detached", side_effect=lambda: order.append("detach")), \
             patch.object(prompt_hook, "_read_payload", return_value={}):
            prompt_hook.run_hook(_args(rotate="best", min_interval=0, no_detach_orca=False))
        assert order == ["detach", "switch"]

    def test_old_sync_orca_flag_now_detaches(self, backup_dir):
        with patch("claude_swap.orca.keep_detached", return_value=None) as keep:
            self._run(
                backup_dir, [], args=_args(rotate="best", min_interval=0, no_detach_orca=False),
                switch_result={"switched": False},
            )
        keep.assert_called_once()

    def test_no_orca_contact_with_no_detach_orca(self, backup_dir):
        with patch("claude_swap.orca.keep_detached") as keep:
            self._run(
                backup_dir, [], args=_args(rotate="best", min_interval=0),
                switch_result={"switched": True, "message": "Switched to Account-2 (b@e)"},
            )
        keep.assert_not_called()

    def test_detach_is_reported_even_without_a_switch(self, backup_dir, capsys):
        with patch("claude_swap.orca.keep_detached", return_value="Orca detached from the Claude login"):
            self._run(
                backup_dir, [], args=_args(rotate="best", min_interval=0, no_detach_orca=False),
                switch_result={"switched": False},
            )
        assert "Orca detached" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_detach_failure_never_breaks_the_prompt(self, backup_dir, capsys):
        with patch("claude_swap.orca.keep_detached", side_effect=RuntimeError("boom")):
            code, _, _ = self._run(
                backup_dir, [], args=_args(rotate="best", min_interval=0, no_detach_orca=False),
                switch_result={"switched": True, "message": "Switched to Account-2 (b@e)"},
            )
        assert code == 0
        assert "Account-2" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_relogin_required_is_reported(self, backup_dir, capsys):
        self._run(
            backup_dir, [], args=_args(rotate="next-available", min_interval=0),
            switch_result={"switched": False, "reason": "relogin-required",
                           "message": "Account-2 need a re-login — staying on Account-1."},
        )
        assert "re-login" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_detach_is_the_default_and_not_written(self):
        base = dict(rotate=None, min_interval=0, no_detach_orca=False)
        assert prompt_hook._forwarded_options(_args(**base)) == []
        assert prompt_hook._forwarded_options(_args(**base, sync_orca=True)) == []
        assert prompt_hook._forwarded_options(_args(**{**base, "no_detach_orca": True})) == ["--no-detach-orca"]


def test_bad_flag_on_run_path_does_not_block_prompt():
    with pytest.raises(SystemExit) as exc:
        prompt_hook.hook_command_main(["--strategy=nope"])
    assert exc.value.code == 0


def test_bad_flag_on_install_still_errors():
    with pytest.raises(SystemExit) as exc:
        prompt_hook.hook_command_main(["install", "--strategy=nope"])
    assert exc.value.code == 2


def test_cli_dispatches_hook(monkeypatch):
    from claude_swap import cli

    monkeypatch.setattr("sys.argv", ["cswap", "hook", "status"])
    with patch("claude_swap.prompt_hook.hook_command_main") as main:
        cli.main()
    main.assert_called_once_with(["status"])
