"""Spond-MCP: verktøyregistrering i stdio- og HTTP-modus, annotasjoner og lesemodus."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")

from _mcp_fixtures import config  # noqa: E402

from spond_familie.runtime import Runtime  # noqa: E402
from spond_familie.server import HttpOptions, build_server, run_tool_cli, serve_http  # noqa: E402

READ = {"overview", "list_groups", "list_events", "get_event", "list_posts", "list_chats",
        "get_chat", "find_person", "attendance_stats", "status"}
WRITE = {"rsvp", "send_message"}
ALL = READ | WRITE | {"export_attendance_xlsx"}


def _tools(tmp_path, monkeypatch, *, readonly: bool, http: bool):
    if readonly:
        monkeypatch.setenv("SPOND_MCP_READONLY", "1")
    else:
        monkeypatch.delenv("SPOND_MCP_READONLY", raising=False)
    rt = Runtime(config=config(), mode="remote" if http else "local")
    opts = HttpOptions("https://mcp.example:8443", state_dir=tmp_path) if http else None
    mcp = build_server(rt, http=opts)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("http", [False, True])
def test_all_tools_registered_with_hints(tmp_path, monkeypatch, http):
    tools = _tools(tmp_path, monkeypatch, readonly=False, http=http)
    assert set(tools) == ALL
    for name in READ:
        assert tools[name].annotations.readOnlyHint is True, name
    for name in WRITE | {"export_attendance_xlsx"}:
        assert tools[name].annotations.readOnlyHint is False, name
    assert tools["send_message"].annotations.idempotentHint is False
    assert "confirm" in tools["rsvp"].inputSchema["properties"]


@pytest.mark.parametrize("http", [False, True])
def test_readonly_hides_write_tools(tmp_path, monkeypatch, http):
    tools = _tools(tmp_path, monkeypatch, readonly=True, http=http)
    assert not set(tools) & WRITE
    assert READ <= set(tools)


def test_cli_refuses_confirm(capsys):
    assert run_tool_cli("rsvp", '{"event_id": "x", "child": "a", "answer": "ja", "confirm": true}') == 2
    assert "kan ikke bekrefte" in capsys.readouterr().err


def test_http_refuses_with_password_in_env(monkeypatch, capsys):
    monkeypatch.setenv("SPOND_PASSWORD", "hemmelig")
    assert serve_http(8765, "https://mcp.example:8443") == 2
    assert "SPOND_PASSWORD" in capsys.readouterr().err


def test_http_requires_https_public_url(monkeypatch, capsys):
    monkeypatch.delenv("SPOND_PASSWORD", raising=False)
    assert serve_http(8765, None) == 2
    assert serve_http(8765, "http://plain.example") == 2
