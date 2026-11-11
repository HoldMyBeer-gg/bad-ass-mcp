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


def test_bad_config_json_is_one_readable_line(tmp_path, capsys):
    from bad_ass_mcp.bam.cli import parse

    cfg = tmp_path / "bad.json"
    cfg.write_text('{"window": "x",\n  oops}')
    with pytest.raises(SystemExit):
        parse(["--config", str(cfg), str(tmp_path)])
    msg = capsys.readouterr().err
    assert "bad.json" in msg and "not valid JSON" in msg and "line 2" in msg


def test_bad_config_json_exits_2_like_other_config_errors(tmp_path, capsys):
    from bad_ass_mcp.bam.cli import parse

    cfg = tmp_path / "bad.json"
    cfg.write_text("{oops")
    with pytest.raises(SystemExit) as e:
        parse(["--config", str(cfg), str(tmp_path)])
    assert e.value.code == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_the_run_is_stamped_with_the_scripts_repo_commit(tmp_path):
    """Run from the bad-ass-mcp checkout, BAM stamped bad-ass-mcp's HEAD
    instead of the app's. The scripts' repo is the one under test."""
    import subprocess

    from bad_ass_mcp.bam.cli import _commit

    repo = tmp_path / "app"
    (repo / "smoke").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "smoke" / "a.json").write_text("{}")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "x"], check=True)
    head = subprocess.run(
        [*git, "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert _commit([repo / "smoke" / "a.json"]) == head
