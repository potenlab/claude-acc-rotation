"""Keep the Orca app's active Claude account in step with cswap's.

Orca (https://orca.computer) manages Claude accounts of its own and writes the
same system login cswap does — ``~/.claude/.credentials.json``, the
``Claude Code-credentials`` Keychain item and ``oauthAccount`` in
``~/.claude.json``. It re-asserts its choice on every Claude pane launch, on
window focus and on a background usage poll, so a switch cswap makes is undone
minutes later unless Orca is told about it.

There is no public API for that. The runtime does expose ``accounts.selectClaude``
over the local unix socket named in its metadata file, which is what this module
calls. That is **undocumented**: every entry point degrades to "do nothing" when
Orca isn't running, the method is missing, or anything else goes wrong — the
caller's switch has already happened and must never fail because of Orca.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path

# Enough for a local socket round-trip; the hook budget is the real constraint.
DEFAULT_TIMEOUT = 5.0
_SELECT_TIMEOUT = 15.0  # a switch writes credentials + Keychain


class OrcaUnavailable(Exception):
    """Orca isn't running, or its runtime can't be reached."""


def metadata_path() -> Path:
    """Where Orca writes its runtime endpoint and auth token."""
    override = os.environ.get("ORCA_RUNTIME_METADATA")
    if override:
        return Path(override)
    base = os.environ.get("ORCA_USER_DATA_PATH")
    if base:
        return Path(base) / "orca-runtime.json"
    return Path.home() / "Library/Application Support/orca/orca-runtime.json"


def _metadata() -> dict:
    try:
        data = json.loads(metadata_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise OrcaUnavailable(f"no Orca runtime metadata: {e}") from e
    if not isinstance(data, dict) or not data.get("authToken"):
        raise OrcaUnavailable("Orca runtime metadata is incomplete")
    return data


def _endpoint(meta: dict) -> str:
    for transport in meta.get("transports") or []:
        if isinstance(transport, dict) and transport.get("kind") == "unix":
            endpoint = transport.get("endpoint")
            if endpoint:
                return str(endpoint)
    raise OrcaUnavailable("Orca runtime exposes no unix socket")


def call(method: str, params: dict | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send one JSON-line request to the Orca runtime and return its result.

    Keepalive frames (``{"_keepalive": true}``) are skipped: the runtime
    interleaves them with the terminal frame during a slow call.
    """
    meta = _metadata()
    request_id = str(uuid.uuid4())
    payload = json.dumps(
        {
            "id": request_id,
            "authToken": meta["authToken"],
            "method": method,
            "params": params or {},
        }
    ).encode()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(_endpoint(meta))
        sock.sendall(payload + b"\n")
        buffer = b""
        while True:
            newline = buffer.find(b"\n")
            if newline == -1:
                chunk = sock.recv(65536)
                if not chunk:
                    raise OrcaUnavailable("Orca closed the connection")
                buffer += chunk
                continue
            line, buffer = buffer[:newline], buffer[newline + 1 :]
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as e:
                raise OrcaUnavailable(f"invalid response frame: {e}") from e
            if not isinstance(frame, dict) or frame.get("_keepalive"):
                continue
            if frame.get("id") != request_id:
                raise OrcaUnavailable("mismatched response id")
            if not frame.get("ok"):
                raise OrcaUnavailable(f"{method} failed: {frame.get('error')}")
            result = frame.get("result")
            return result if isinstance(result, dict) else {}
    except (OSError, socket.timeout) as e:
        raise OrcaUnavailable(f"Orca runtime unreachable: {e}") from e
    finally:
        sock.close()


def claude_accounts(timeout: float = DEFAULT_TIMEOUT) -> tuple[dict[str, str], str | None]:
    """Return ({email: accountId}, active account id) for Orca's Claude accounts."""
    claude = call("accounts.list", timeout=timeout).get("claude") or {}
    by_email = {
        a["email"]: a["id"]
        for a in claude.get("accounts") or []
        if isinstance(a, dict) and a.get("email") and a.get("id")
    }
    active = claude.get("activeAccountId")
    return by_email, active if isinstance(active, str) else None


def sync_active_account(email: str, timeout: float = _SELECT_TIMEOUT) -> str | None:
    """Point Orca at ``email`` so it stops reverting the login to its own pick.

    Returns a short note when something was changed, otherwise None. Never
    raises: an Orca that is closed, older, or mid-switch just means the sync
    is skipped.
    """
    try:
        by_email, active = claude_accounts(timeout=DEFAULT_TIMEOUT)
        account_id = by_email.get(email)
        if account_id is None:
            # Orca doesn't manage this account; it can't revert to it either.
            return None
        if account_id == active:
            return None
        call("accounts.selectClaude", {"accountId": account_id}, timeout=timeout)
        return f"Orca now follows {email}"
    except OrcaUnavailable:
        return None
    except Exception:  # an undocumented API must never break a switch
        return None


def is_running() -> bool:
    try:
        claude_accounts()
        return True
    except OrcaUnavailable:
        return False
