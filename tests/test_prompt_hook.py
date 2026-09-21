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

    def _run(self, backup_dir: Path, events: list, args=None, env=None):
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        engine = MagicMock()

        def fake_engine(_switcher, _settings, on_event, **_kw):
            engine.tick.side_effect = lambda: [on_event(e) for e in events]
            return engine

        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch("claude_swap.autoswitch.AutoSwitchEngine", side_effect=fake_engine), \
             patch.dict("os.environ", env or {}, clear=False), \
             patch.object(prompt_hook, "_drain_stdin"):
            code = prompt_hook.run_hook(args or _args())
        return code, engine

    def test_switch_is_reported_as_system_message(self, backup_dir, capsys):
        code, engine = self._run(backup_dir, [_switch_event()])
        assert code == 0
        engine.tick.assert_called_once()
        out = json.loads(capsys.readouterr().out)
        assert "Account-2" in out["systemMessage"]

    def test_no_switch_prints_nothing(self, backup_dir, capsys):
        code, _ = self._run(backup_dir, [NoSwitchEvent(reason="below-threshold")])
        assert code == 0
        assert capsys.readouterr().out == ""

    def test_all_exhausted_is_reported(self, backup_dir, capsys):
        self._run(backup_dir, [AllExhaustedEvent(earliest_reset_at=None)])
        assert "exhausted" in json.loads(capsys.readouterr().out)["systemMessage"]

    def test_quiet_suppresses_message(self, backup_dir, capsys):
        self._run(backup_dir, [_switch_event()], args=_args(quiet=True))
        assert capsys.readouterr().out == ""

    def test_throttled_second_prompt_skips_tick(self, backup_dir):
        _, first = self._run(backup_dir, [])
        _, second = self._run(backup_dir, [])
        first.tick.assert_called_once()
        second.tick.assert_not_called()

    def test_min_interval_zero_never_throttles(self, backup_dir):
        self._run(backup_dir, [], args=_args(min_interval=0))
        _, second = self._run(backup_dir, [], args=_args(min_interval=0))
        second.tick.assert_called_once()

    def test_skips_inside_session_profile(self, backup_dir):
        session = backup_dir / "sessions" / "2"
        session.mkdir(parents=True)
        _, engine = self._run(backup_dir, [], env={"CLAUDE_CONFIG_DIR": str(session)})
        engine.tick.assert_not_called()

    def test_engine_crash_still_exits_zero(self, backup_dir, capsys):
        switcher = MagicMock()
        switcher.backup_dir = backup_dir
        with patch("claude_swap.switcher.ClaudeAccountSwitcher", return_value=switcher), \
             patch("claude_swap.autoswitch.AutoSwitchEngine", side_effect=RuntimeError("boom")), \
             patch.object(prompt_hook, "_drain_stdin"):
            assert prompt_hook.run_hook(_args()) == 0
        assert capsys.readouterr().out == ""


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
