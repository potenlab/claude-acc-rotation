"""Tests for the Orca runtime bridge (undocumented accounts.selectClaude)."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from claude_swap import orca

ACCOUNTS = {
    "claude": {
        "accounts": [
            {"id": "id-a", "email": "a@example.com"},
            {"id": "id-b", "email": "b@example.com"},
        ],
        "activeAccountId": "id-a",
    }
}


class FakeRuntime:
    """A unix-socket server speaking Orca's one-JSON-line-per-frame protocol."""

    def __init__(self, tmp_path: Path, *, keepalives: int = 0, fail: str | None = None):
        # AF_UNIX paths cap at ~104 bytes on macOS; pytest's tmp_path is longer.
        self._dir = Path(tempfile.mkdtemp(prefix="cswap-orca-"))
        self.path = self._dir / "s.sock"
        self.token = "tok-123"
        self.keepalives = keepalives
        self.fail = fail
        self.requests: list[dict] = []
        self.active = ACCOUNTS["claude"]["activeAccountId"]
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.path))
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def metadata(self) -> dict:
        return {
            "authToken": self.token,
            "transports": [{"kind": "unix", "endpoint": str(self.path)}],
        }

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if not data.strip():
                    continue
                request = json.loads(data.split(b"\n")[0])
                self.requests.append(request)
                for _ in range(self.keepalives):
                    conn.sendall(b'{"_keepalive": true}\n')
                conn.sendall((json.dumps(self._response(request)) + "\n").encode())

    def _response(self, request: dict) -> dict:
        if self.fail == "error":
            return {"id": request["id"], "ok": False, "error": "switch in progress"}
        if self.fail == "id":
            return {"id": "wrong-id", "ok": True, "result": {}}
        if request["method"] == "accounts.selectClaude":
            self.active = request["params"]["accountId"]  # None = system default
        payload = json.loads(json.dumps(ACCOUNTS))
        payload["claude"]["activeAccountId"] = self.active
        return {"id": request["id"], "ok": True, "result": payload}

    def close(self) -> None:
        self._server.close()
        shutil.rmtree(self._dir, ignore_errors=True)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    server = FakeRuntime(tmp_path)
    meta = tmp_path / "orca-runtime.json"
    meta.write_text(json.dumps(server.metadata()))
    monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(meta))
    yield server
    server.close()


class TestCall:
    def test_lists_accounts_and_active(self, runtime):
        by_email, active = orca.claude_accounts()
        assert by_email == {"a@example.com": "id-a", "b@example.com": "id-b"}
        assert active == "id-a"

    def test_sends_auth_token_and_method(self, runtime):
        orca.claude_accounts()
        request = runtime.requests[-1]
        assert request["authToken"] == "tok-123"
        assert request["method"] == "accounts.list"

    def test_keepalive_frames_are_skipped(self, tmp_path, monkeypatch):
        server = FakeRuntime(tmp_path, keepalives=3)
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps(server.metadata()))
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(meta))
        try:
            assert orca.claude_accounts()[1] == "id-a"
        finally:
            server.close()

    def test_runtime_error_raises_unavailable(self, tmp_path, monkeypatch):
        server = FakeRuntime(tmp_path, fail="error")
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps(server.metadata()))
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(meta))
        try:
            with pytest.raises(orca.OrcaUnavailable):
                orca.claude_accounts()
        finally:
            server.close()

    def test_mismatched_id_raises(self, tmp_path, monkeypatch):
        server = FakeRuntime(tmp_path, fail="id")
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps(server.metadata()))
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(meta))
        try:
            with pytest.raises(orca.OrcaUnavailable):
                orca.claude_accounts()
        finally:
            server.close()

    def test_missing_metadata_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(tmp_path / "nope.json"))
        with pytest.raises(orca.OrcaUnavailable):
            orca.claude_accounts()
        assert orca.is_running() is False


class TestSync:
    def test_selects_the_new_account(self, runtime):
        note = orca.sync_active_account("b@example.com")
        assert note == "Orca now follows b@example.com"
        select = [r for r in runtime.requests if r["method"] == "accounts.selectClaude"]
        assert select[-1]["params"] == {"accountId": "id-b"}
        assert runtime.active == "id-b"

    def test_already_active_is_a_no_op(self, runtime):
        assert orca.sync_active_account("a@example.com") is None
        assert [r["method"] for r in runtime.requests] == ["accounts.list"]

    def test_account_orca_does_not_manage_is_skipped(self, runtime):
        assert orca.sync_active_account("stranger@example.com") is None
        assert [r["method"] for r in runtime.requests] == ["accounts.list"]

    def test_orca_not_running_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(tmp_path / "gone.json"))
        assert orca.sync_active_account("a@example.com") is None

    def test_select_failure_is_silent(self, tmp_path, monkeypatch):
        server = FakeRuntime(tmp_path, fail="error")
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps(server.metadata()))
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(meta))
        try:
            assert orca.sync_active_account("b@example.com") is None
        finally:
            server.close()


class TestDetach:
    def test_detach_clears_the_active_account(self, runtime):
        assert orca.detach() is True
        select = [r for r in runtime.requests if r["method"] == "accounts.selectClaude"]
        assert select[-1]["params"] == {"accountId": None}
        assert runtime.active is None

    def test_detach_when_already_detached_is_a_no_op(self, runtime):
        runtime.active = None
        assert orca.detach() is False
        assert [r["method"] for r in runtime.requests] == ["accounts.list"]

    def test_keep_detached_reports_only_when_it_acted(self, runtime):
        assert orca.keep_detached() == "Orca detached from the Claude login"
        assert orca.keep_detached() is None

    def test_keep_detached_is_silent_when_orca_is_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(tmp_path / "gone.json"))
        assert orca.keep_detached() is None

    def test_status_reports_management(self, runtime):
        info = orca.status()
        assert info == {
            "running": True, "managing": True, "activeEmail": "a@example.com",
            "accounts": ["a@example.com", "b@example.com"],
        }
        orca.detach()
        assert orca.status()["managing"] is False

    def test_status_when_not_running(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(tmp_path / "gone.json"))
        assert orca.status()["running"] is False


def test_metadata_path_prefers_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCA_RUNTIME_METADATA", str(tmp_path / "x.json"))
    assert orca.metadata_path() == tmp_path / "x.json"

    monkeypatch.delenv("ORCA_RUNTIME_METADATA")
    monkeypatch.setenv("ORCA_USER_DATA_PATH", str(tmp_path))
    assert orca.metadata_path() == tmp_path / "orca-runtime.json"
