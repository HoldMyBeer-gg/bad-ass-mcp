"""The bad-ass-mcp command: things that must not drop into the stdio server."""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

import pytest

from bad_ass_mcp import server


def _main(argv, *, tty=False):
    stdin = io.StringIO()
    stdin.isatty = lambda: tty
    with (
        patch.object(sys, "argv", ["bad-ass-mcp", *argv]),
        patch.object(sys, "stdin", stdin),
        patch.object(server.mcp, "run") as run,
    ):
        with pytest.raises(SystemExit) as e:
            server.main()
    return e.value.code, run


def test_version_prints_and_exits(capsys):
    code, run = _main(["--version"])
    assert code == 0
    run.assert_not_called()
    assert capsys.readouterr().out.strip().startswith("bad-ass-mcp ")


def test_server_refuses_to_start_on_a_terminal(capsys):
    """Typed at a shell, the stdio server would block forever waiting for
    an MCP client. Say what it is instead."""
    code, run = _main([], tty=True)
    assert code == 2
    run.assert_not_called()
    assert "MCP server" in capsys.readouterr().err


def test_unknown_flag_is_an_error_not_a_server(capsys):
    code, run = _main(["--frobnicate"])
    assert code == 2
    run.assert_not_called()


def test_server_starts_when_a_client_pipes_stdin():
    stdin = io.StringIO()
    stdin.isatty = lambda: False
    with (
        patch.object(sys, "argv", ["bad-ass-mcp"]),
        patch.object(sys, "stdin", stdin),
        patch.object(server.mcp, "run") as run,
    ):
        server.main()
    run.assert_called_once()


def test_bad_config_json_is_one_readable_line(tmp_path):
    from bad_ass_mcp.bam.cli import parse

    cfg = tmp_path / "bad.json"
    cfg.write_text('{"window": "x",\n  oops}')
    with pytest.raises(SystemExit) as e:
        parse(["--config", str(cfg), str(tmp_path)])
    msg = str(e.value.code)
    assert "bad.json" in msg and "not valid JSON" in msg and "line 2" in msg
