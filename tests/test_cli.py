"""spond-familie-kommandoen: setup-claude og status (ingen nettverk)."""

from __future__ import annotations

import json

import pytest

from spond_familie import cli
from spond_familie.tokens import SpondTokens, TokenStore


def _run(argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    return e.value.code


def test_setup_claude_adds_entry_and_keeps_others(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"annen": {"command": "x"}}, "theme": "dark"}))
    assert _run(["setup-claude", "--config", str(cfg)]) == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["annen"] == {"command": "x"} and data["theme"] == "dark"
    assert data["mcpServers"]["spond"]["command"]
    assert "env" not in data["mcpServers"]["spond"]
    assert (tmp_path / "claude_desktop_config.json.bak-spond-familie").exists()
    assert "claude mcp add --scope user spond" in capsys.readouterr().out


def test_setup_claude_readonly_and_fresh_file(tmp_path):
    cfg = tmp_path / "ny" / "claude_desktop_config.json"
    assert _run(["setup-claude", "--readonly", "--config", str(cfg)]) == 0
    entry = json.loads(cfg.read_text())["mcpServers"]["spond"]
    assert entry["env"] == {"SPOND_MCP_READONLY": "1"}


def test_setup_claude_refuses_broken_json(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{ ikke json")
    assert _run(["setup-claude", "--config", str(cfg)]) == 1
    assert cfg.read_text() == "{ ikke json"


def test_status_without_and_with_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SPOND_MCP_HOME", str(tmp_path))
    assert _run(["status"]) == 1
    assert "spond-familie login" in capsys.readouterr().out
    import time
    now = time.time()
    TokenStore().write(SpondTokens("A", now + 3600 * 20, "R-hemmelig", now + 86400 * 89, now, "login"))
    assert _run(["status"]) == 0
    out = capsys.readouterr().out
    assert "dager igjen" in out and "R-hemmelig" not in out
