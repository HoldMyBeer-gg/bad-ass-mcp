"""A recording survives the server, and the recorder does not.

From a Windows run: when the server died mid-take, its ffmpeg child kept
recording the whole desktop until its 30 minute bound (a privacy problem),
and a hard kill would have left an MP4 with no moov atom, so nothing to
play. The capture is now written fragmented, so a partial file plays, and
the recorder is tied to the server's life."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from bad_ass_mcp.backend.base import DesktopBackend
from tests.fake_backend import FakeBackend

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process guard")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(pred, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ── the capture plays after a crash ───────────────────────────────────


def test_capture_is_written_fragmented():
    """Fragmented MP4 (empty moov up front, a fragment per keyframe) plays
    from whatever was flushed, and a keyframe is forced every two seconds
    so a crash loses at most that much."""
    cmd = DesktopBackend._h264_output("/tmp/x.mp4")
    flags = cmd[cmd.index("-movflags") + 1]
    assert "+frag_keyframe" in flags and "+empty_moov" in flags
    assert "+faststart" not in flags
    assert cmd[cmd.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)"
    assert cmd[cmd.index("-flush_packets") + 1] == "1"  # fragments reach the disk as written


def test_mp4_export_remuxes_to_a_seekable_file(tmp_path, monkeypatch):
    """A clean stop still delivers the ordinary file players seek in: a
    stream copy with the moov atom moved up front, no re-encode."""
    src = tmp_path / "bam-rec-x.mp4"
    src.write_bytes(b"fragments")
    dst = tmp_path / "out.mp4"
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        dst.write_bytes(b"remuxed")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = FakeBackend()._export_recording(str(src), str(dst), "mp4")
    assert out == str(dst)
    cmd = calls[0]
    assert cmd[0] == "ffmpeg" and cmd[cmd.index("-c") + 1] == "copy"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert dst.read_bytes() == b"remuxed"
    assert not src.exists()


def test_mp4_export_falls_back_to_the_capture_without_ffmpeg(tmp_path, monkeypatch):
    src = tmp_path / "bam-rec-x.mp4"
    src.write_bytes(b"fragments")
    dst = tmp_path / "out.mp4"

    def no_ffmpeg(cmd, **kw):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", no_ffmpeg)
    assert FakeBackend()._export_recording(str(src), str(dst), "mp4") == str(dst)
    assert dst.read_bytes() == b"fragments" and not src.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_killed_capture_still_plays(tmp_path):
    """The real thing: record a test pattern with the recording flags, kill
    ffmpeg outright, and ffprobe still reads frames from what is left."""
    video = tmp_path / "bam-rec-k.mp4"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-re",
        "-f",
        "lavfi",
        "-r",
        "15",
        "-i",
        "testsrc=size=64x64",
        *DesktopBackend._h264_output(str(video)),
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    assert _wait_until(lambda: video.exists() and video.stat().st_size > 2000, 15.0)
    time.sleep(2.5)  # past the first forced keyframe, so a fragment is on disk
    proc.kill()
    proc.wait()
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_packets",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_packets",
            "-of",
            "csv=p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    assert int(probe.stdout.strip() or 0) > 0


# ── the recorder dies with the server ─────────────────────────────────


@posix
def test_recorder_dies_with_its_parent(tmp_path):
    pidfile = tmp_path / "inner.pid"
    video = tmp_path / "bam-rec-p.mp4"
    parent = textwrap.dedent(
        f"""
        import time
        from bad_ass_mcp.backend.base import DesktopBackend
        DesktopBackend._spawn_recorder(
            ["sh", "-c", "echo $$ > {pidfile}; exec sleep 60"], {str(video)!r}
        )
        time.sleep(60)
        """
    )
    server = subprocess.Popen([sys.executable, "-c", parent], cwd=os.getcwd())
    try:
        assert _wait_until(lambda: pidfile.exists() and pidfile.read_text().strip(), 10.0)
        inner = int(pidfile.read_text().strip())
        assert _alive(inner)
        server.kill()
        server.wait()
        assert _wait_until(lambda: not _alive(inner), 5.0), "recorder outlived the server"
    finally:
        if server.poll() is None:
            server.kill()
        try:
            os.kill(int(pidfile.read_text().strip()), 9)
        except Exception:
            pass


@posix
def test_quit_byte_reaches_the_recorder_through_the_guard(tmp_path):
    """stop_recording tells ffmpeg to finish with a 'q' on stdin; the guard
    in between must pass it on (a background job in sh gets /dev/null as
    stdin unless the script says otherwise)."""
    out = tmp_path / "stdin.txt"
    video = tmp_path / "bam-rec-q.mp4"
    entry = DesktopBackend._spawn_recorder(
        ["sh", "-c", f"head -c 1 > {out}; : > {video}"], str(video)
    )
    assert DesktopBackend._finish_recorder(entry) == str(video)
    assert out.read_text() == "q"
    assert entry[0].returncode == 0


@posix
def test_a_recorder_that_ignores_quit_is_killed_whole(tmp_path):
    """On the timeout path the kill must reach ffmpeg, not only the guard."""
    pidfile = tmp_path / "inner.pid"
    video = tmp_path / "bam-rec-t.mp4"
    video.write_bytes(b"")
    entry = DesktopBackend._spawn_recorder(
        ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 60"], str(video)
    )
    assert _wait_until(lambda: pidfile.exists() and pidfile.read_text().strip(), 5.0)
    inner = int(pidfile.read_text().strip())
    DesktopBackend._finish_recorder(entry, grace=0.3)
    assert _wait_until(lambda: not _alive(inner), 3.0), "inner recorder survived the kill"
