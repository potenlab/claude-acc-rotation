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
        rotate=None,
        skip_path=[],
        sync_orca=False,
        reserve=None,
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
            _args(threshold=80.0, strategy="consume-first", model="Fable,Opus", quiet=True)
        )
        assert opts == [
            "--threshold=80",
            "--strategy=consume-first",
            "--model=Fable,Opus",
            "--quiet",
        ]

    def test_forwarded_options_keep_precision_and_dash_values(self):
        opts = prompt_hook._forwarded_options(_args(cooldown=1234567.0, model="-foo"))
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
            strategy="next-available", json_output=True, reserve=5.0
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
        opts = prompt_hook._forwarded_options(_args(rotate="best", min_interval=0))
        assert opts == ["--rotate=best"]

    def test_unset_reserve_is_not_forwarded(self):
        """No flag -> the installed command stays free of it, so hook.reserve
        (read at every run) governs already-open sessions too."""
        assert prompt_hook._forwarded_options(_args(rotate="next-available", min_interval=0)) == [
            "--rotate=next-available"
        ]

    def test_custom_reserve_is_forwarded(self):
        opts = prompt_hook._forwarded_options(
            _args(rotate="next-available", min_interval=0, reserve=10.0)
        )
        assert opts == ["--rotate=next-available", "--reserve=10"]

    def test_explicit_min_interval_is_forwarded(self):
        opts = prompt_hook._forwarded_options(_args(rotate="best", min_interval=5.0))
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
        opts = prompt_hook._forwarded_options(_args(skip_path=["~/orca", "/tmp/x"]))
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


class TestOrcaSync(TestRunHook):
    def test_sync_runs_after_a_switch(self, backup_dir, capsys):
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        switcher._get_current_account.return_value = ("b@example.com", "org")
        switcher.switch.return_value = {"switched": True, "message": "Switched to Account-2 (b@example.com)"}
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch("claude_swap.orca.sync_active_account", return_value="Orca now follows b@example.com") as sync, \
             patch.object(prompt_hook, "_read_payload", return_value={}):
            prompt_hook.run_hook(_args(rotate="best", min_interval=0, sync_orca=True))
        sync.assert_called_once_with("b@example.com")
        assert "Orca now follows" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_no_sync_without_the_flag(self, backup_dir):
        with patch("claude_swap.orca.sync_active_account") as sync:
            self._run(
                backup_dir, [], args=_args(rotate="best", min_interval=0),
                switch_result={"switched": True, "message": "Switched to Account-2 (b@e)"},
            )
        sync.assert_not_called()

    def test_no_sync_when_nothing_switched(self, backup_dir):
        with patch("claude_swap.orca.sync_active_account") as sync:
            self._run(
                backup_dir, [NoSwitchEvent(reason="below-threshold")],
                args=_args(min_interval=0, sync_orca=True),
            )
        sync.assert_not_called()

    def test_sync_failure_never_breaks_the_message(self, backup_dir, capsys):
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        switcher._get_current_account.side_effect = RuntimeError("no login")
        switcher.switch.return_value = {"switched": True, "message": "Switched to Account-2 (b@e)"}
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch.object(prompt_hook, "_read_payload", return_value={}):
            assert prompt_hook.run_hook(_args(rotate="best", min_interval=0, sync_orca=True)) == 0
        assert "Account-2" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_sync_orca_is_forwarded(self):
        assert prompt_hook._forwarded_options(_args(sync_orca=True)) == ["--sync-orca"]


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
