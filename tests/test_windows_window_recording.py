"""Recording one window, not the screen region it covers.

The goblin showcase, November 14th: a take of Blender recorded the MO2
window that sat in front of it, because gdigrab copies a screen region.
ffmpeg's gfxcapture (Windows Graphics Capture) was tried first: it sends a
frame only when the window changes, so a still window gave one frame and
then stalled the whole take. Instead the backend copies the window itself
(PrintWindow, which draws covered windows too) at a steady rate and feeds
the frames to ffmpeg as raw video on stdin.
"""

from __future__ import annotations

import io
import time
from unittest.mock import MagicMock, patch

from bad_ass_mcp.backend import windows as w
from bad_ass_mcp.backend.base import DesktopBackend


class _Sink(io.BytesIO):
    def flush(self) -> None:
        pass


def _pump(grab, fps=50, size=(2, 2)):
    sink = _Sink()
    pump = w._FramePump(grab, sink, fps=fps, size=size)
    pump.start()
    return pump, sink


def test_pump_writes_frames_at_a_steady_rate():
    frame = b"\x01" * 16
    pump, sink = _pump(lambda: frame, fps=50)
    time.sleep(0.4)
    pump.stop()
    frames = len(sink.getvalue()) // 16
    assert len(sink.getvalue()) % 16 == 0
    assert 14 <= frames <= 26


def test_pump_repeats_the_last_frame_when_a_grab_fails():
    grabs = iter([b"\x07" * 16])
    pump, sink = _pump(lambda: next(grabs, None), fps=50)
    time.sleep(0.2)
    pump.stop()
    data = sink.getvalue()
    assert len(data) >= 32
    assert set(data) == {7}


def test_pump_starts_black_until_the_first_grab_works():
    pump, sink = _pump(lambda: None, fps=50)
    time.sleep(0.1)
    pump.stop()
    data = sink.getvalue()
    assert data and set(data) == {0}


def test_pump_keeps_the_clock_when_a_grab_is_slow():
    """A grab slower than the frame interval must not make the video
    shorter than the take: the frame is written again to catch up."""

    def slow():
        time.sleep(0.1)
        return b"\x02" * 16

    pump, sink = _pump(slow, fps=50)
    time.sleep(0.5)
    pump.stop()
    frames = len(sink.getvalue()) // 16
    assert frames >= 18


def test_pump_stops_when_ffmpeg_is_gone():
    class Broken(_Sink):
        def write(self, b):
            raise BrokenPipeError

    pump = w._FramePump(lambda: b"\x00" * 16, Broken(), fps=50, size=(2, 2))
    pump.start()
    time.sleep(0.1)
    assert not pump.is_alive()


def _start(window_id, frame=(100, 100, 1201, 801), rect=(93, 100, 1214, 807)):
    backend = object.__new__(w.WindowsBackend)
    backend._recordings = {}
    backend._window_rect = lambda hwnd: rect
    spawned = []
    backend._spawn_recorder = lambda cmd, path: (spawned.append(cmd), (MagicMock(), path, "log"))[1]
    with (
        patch.object(w, "_dwm_frame_bounds", lambda hwnd: frame),
        patch.object(w, "_FramePump") as pump_cls,
    ):
        handle = backend.start_recording(window_id, fps=15)
    return backend, handle, spawned[0], pump_cls


def test_window_recording_reads_raw_frames_from_stdin():
    _, _, cmd, _ = _start("42")
    assert "gdigrab" not in cmd
    assert cmd[cmd.index("-f") + 1] == "rawvideo"
    assert cmd[cmd.index("-pix_fmt") + 1] == "bgra"
    assert cmd[cmd.index("-framerate") + 1] == "15"
    assert cmd[cmd.index("-i") + 1] == "-"


def test_window_recording_size_is_the_visible_frame_evened():
    _, _, cmd, _ = _start("42")
    assert cmd[cmd.index("-video_size") + 1] == "1200x800"


def test_window_recording_falls_back_to_the_window_rect_without_dwm():
    _, _, cmd, _ = _start("42", frame=None)
    assert cmd[cmd.index("-video_size") + 1] == "1214x806"


def test_window_recording_starts_a_pump_into_ffmpeg():
    backend, handle, _, pump_cls = _start("42")
    entry = backend._recordings[handle]
    assert entry[3] is pump_cls.return_value
    pump_cls.return_value.start.assert_called_once()
    _, kwargs = pump_cls.call_args
    assert kwargs["fps"] == 15
    assert kwargs["size"] == (1200, 800)


def test_full_screen_recording_still_uses_gdigrab():
    backend = object.__new__(w.WindowsBackend)
    backend._recordings = {}
    spawned = []
    backend._spawn_recorder = lambda cmd, path: (spawned.append(cmd), (MagicMock(), path))[1]
    backend.start_recording(None)
    assert "gdigrab" in spawned[0]


def test_finishing_a_pumped_recording_closes_stdin_instead_of_q(tmp_path):
    """'q' on stdin would land in the middle of the raw video. End of input
    is what finishes a piped capture."""
    video = tmp_path / "cap.mp4"
    video.write_bytes(b"x")
    proc = MagicMock()
    pump = MagicMock()
    DesktopBackend._finish_recorder((proc, str(video), None, pump))
    pump.stop.assert_called_once()
    proc.stdin.close.assert_called_once()
    proc.stdin.write.assert_not_called()


def test_crop_takes_the_frame_out_of_the_window_bitmap():
    # A 4x3 bitmap, pixel value = row*4+col; take the 2x2 block at (1, 1).
    bmp = b"".join(bytes([r * 4 + c] * 4) for r in range(3) for c in range(4))
    out = w._crop_bgra(bmp, 4, 1, 1, 2, 2)
    assert out == bytes([5] * 4 + [6] * 4 + [9] * 4 + [10] * 4)
