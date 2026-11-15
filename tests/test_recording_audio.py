"""Recording system sound with a take.

start_recording(audio=True) records what the machine plays (loopback),
never the microphone, beside the video. The sound is written on the same
clock as the frames: when nothing plays, the system hands over no samples
at all, so the gap is filled with silence to keep the two in step. At
stop the sound is muxed into the MP4; a GIF has no sound to carry.
"""

from __future__ import annotations

import io
import math
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from bad_ass_mcp import server
from bad_ass_mcp.backend import base
from bad_ass_mcp.backend.base import DesktopBackend
from tests.fake_backend import FakeBackend


class _Sink(io.BytesIO):
    def flush(self) -> None:
        pass


class _Source:
    """A loopback source at 1000 frames a second, 4 bytes a frame."""

    rate = 1000
    channels = 2
    sample_fmt = "s16le"
    frame_bytes = 4

    def __init__(self, packets=()):
        self._packets = list(packets)
        self.closed = False

    def read(self):
        return self._packets.pop(0) if self._packets else None

    def close(self):
        self.closed = True


def _tap(source, sink=None, origin=None):
    sink = sink or _Sink()
    tap = base._AudioTap(lambda: source, sink, origin=origin)
    tap.start()
    tap.wait_ready(timeout=2)
    return tap, sink


def test_silence_keeps_the_clock_when_nothing_plays():
    src = _Source()
    tap, sink = _tap(src)
    time.sleep(0.3)
    tap.stop()
    frames = len(sink.getvalue()) // 4
    assert 220 <= frames <= 420
    assert set(sink.getvalue()) == {0}
    assert src.closed


def test_played_sound_is_written_as_it_arrives():
    played = b"\x07\x00" * 2 * 50  # 50 frames
    tap, sink = _tap(_Source([played]))
    time.sleep(0.2)
    tap.stop()
    data = sink.getvalue()
    assert played in data
    assert len(data) % 4 == 0


def test_sound_starts_on_the_video_clock():
    """The take began before the tap opened: the time between is silence,
    so sound and frames line up from the first frame."""
    tap, sink = _tap(_Source(), origin=time.monotonic() - 0.5)
    tap.stop()
    assert len(sink.getvalue()) // 4 >= 450


def test_a_source_that_will_not_open_fails_the_start():
    def broken():
        raise OSError("no render device")

    tap = base._AudioTap(broken, _Sink())
    tap.start()
    with pytest.raises(RuntimeError, match="no render device"):
        tap.wait_ready(timeout=2)


def test_tap_reports_the_format_for_ffmpeg():
    tap, _ = _tap(_Source())
    tap.stop()
    assert tap.ffmpeg_input == ["-f", "s16le", "-ar", "1000", "-ac", "2"]


# ── muxing at stop ───────────────────────────────────────────────────────


def test_mp4_export_muxes_the_sound(tmp_path):
    video = tmp_path / "cap.mp4"
    video.write_bytes(b"v")
    pcm = tmp_path / "cap.pcm"
    pcm.write_bytes(b"\0" * 8)
    out = tmp_path / "out.mp4"
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        out.write_bytes(b"done")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    audio = (str(pcm), ["-f", "f32le", "-ar", "48000", "-ac", "2"])
    with patch.object(subprocess, "run", fake_run):
        DesktopBackend._export_recording(str(video), str(out), "mp4", audio=audio)
    cmd = runs[0]
    assert cmd[cmd.index("-f") + 1] == "f32le"
    assert str(pcm) in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert not pcm.exists()


def test_gif_export_drops_the_sound(tmp_path):
    video = tmp_path / "cap.mp4"
    video.write_bytes(b"v")
    pcm = tmp_path / "cap.pcm"
    pcm.write_bytes(b"\0" * 8)
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    audio = (str(pcm), ["-f", "f32le", "-ar", "48000", "-ac", "2"])
    with patch.object(subprocess, "run", fake_run):
        DesktopBackend._export_recording(str(video), str(tmp_path / "o.gif"), "gif", audio=audio)
    assert str(pcm) not in runs[0]
    assert not pcm.exists()


def test_finishing_stops_the_tap_before_ffmpeg(tmp_path):
    video = tmp_path / "cap.mp4"
    video.write_bytes(b"x")
    order = []
    proc = MagicMock()
    proc.stdin.write.side_effect = lambda b: order.append("ffmpeg")
    tap = MagicMock()
    tap.stop.side_effect = lambda: order.append("tap")
    DesktopBackend._finish_recorder((proc, str(video), None, None, tap))
    assert order == ["tap", "ffmpeg"]


# ── the tool ─────────────────────────────────────────────────────────────


def test_tool_passes_audio_to_the_backend():
    b = FakeBackend()
    seen = {}
    b.start_recording = lambda window_id=None, fps=15, audio=False: seen.update(audio=audio) or "h"
    with patch.object(server, "_backend", lambda: b):
        out = server.start_recording("1", audio=True)
    assert out["ok"] and seen["audio"] is True


def test_audio_is_off_unless_asked():
    b = FakeBackend()
    seen = {}
    b.start_recording = lambda window_id=None, fps=15, audio=False: seen.update(audio=audio) or "h"
    with patch.object(server, "_backend", lambda: b):
        server.start_recording("1")
    assert seen["audio"] is False


def test_platforms_without_audio_say_so():
    with pytest.raises(NotImplementedError, match="audio"):
        DesktopBackend._audio_unsupported(True)
    DesktopBackend._audio_unsupported(False)


# ── Windows wiring ───────────────────────────────────────────────────────


def _win_start(window_id, **kw):
    from bad_ass_mcp.backend import windows as w

    backend = object.__new__(w.WindowsBackend)
    backend._recordings = {}
    backend._window_rect = lambda hwnd: (0, 0, 640, 480)
    backend._spawn_recorder = lambda cmd, path: (MagicMock(), path, "log")
    with (
        patch.object(w, "_dwm_frame_bounds", lambda hwnd: (0, 0, 640, 480)),
        patch.object(w, "_FramePump"),
        patch.object(w, "_AudioTap") as tap_cls,
    ):
        handle = backend.start_recording(window_id, fps=15, **kw)
    return backend._recordings[handle], tap_cls


@pytest.mark.parametrize("window_id", ["42", None])
def test_windows_starts_a_tap_when_asked(window_id):
    entry, tap_cls = _win_start(window_id, audio=True)
    assert entry[4] is tap_cls.return_value
    tap_cls.return_value.start.assert_called_once()
    tap_cls.return_value.wait_ready.assert_called_once()


def test_full_screen_sound_starts_when_ffmpeg_does():
    """gdigrab captures from its spawn, and the spawn waits a moment for
    ffmpeg to fail; the sound's clock must start at the spawn, not after."""
    from bad_ass_mcp.backend import windows as w

    backend = object.__new__(w.WindowsBackend)
    backend._recordings = {}
    spawned_at = []

    def spawn(cmd, path):
        spawned_at.append(time.monotonic())
        time.sleep(0.2)
        return (MagicMock(), path, "log")

    backend._spawn_recorder = spawn
    with patch.object(w, "_AudioTap") as tap_cls:
        backend.start_recording(None, audio=True)
    assert tap_cls.call_args.kwargs["origin"] <= spawned_at[0]


def test_windows_records_no_sound_by_default():
    entry, tap_cls = _win_start("42")
    tap_cls.assert_not_called()
    assert len(entry) < 5 or entry[4] is None


# ── the real thing: a tone played while the tap listens ──────────────────


def _tone_wav(seconds=1.0, rate=44100, hz=440, level=0.2):
    n = int(seconds * rate)
    samples = b"".join(
        struct.pack("<h", int(32767 * level * math.sin(2 * math.pi * hz * i / rate)))
        for i in range(n)
    )
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(samples),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        rate,
        rate * 2,
        2,
        16,
        b"data",
        len(samples),
    )
    return header + samples


@pytest.mark.skipif(sys.platform != "win32", reason="WASAPI loopback")
def test_windows_loopback_hears_a_tone_played_on_the_machine(tmp_path):
    import winsound

    from bad_ass_mcp.backend import windows as w

    sink = _Sink()
    tap = base._AudioTap(w._LoopbackSource, sink)
    tap.start()
    try:
        tap.wait_ready(timeout=3)
    except RuntimeError as e:
        pytest.skip(f"no loopback device here: {e}")
    wav = _tone_wav()
    player = threading.Thread(target=winsound.PlaySound, args=(wav, winsound.SND_MEMORY))
    player.start()
    player.join()
    time.sleep(0.2)
    tap.stop()
    data = sink.getvalue()
    fmt = tap.ffmpeg_input[1]
    assert fmt in {"f32le", "s16le"}
    if fmt == "f32le":
        vals = struct.unpack(f"<{len(data) // 4}f", data[: len(data) // 4 * 4])
        peak = max(abs(v) for v in vals)
        assert peak > 0.01, "the tone never reached the loopback"
    else:
        vals = struct.unpack(f"<{len(data) // 2}h", data[: len(data) // 2 * 2])
        assert max(abs(v) for v in vals) > 300
