"""Windows annotation overlay (started by bad_ass_mcp.overlay.start_overlay).

A layered, click-through, top-most, never-activated popup over the virtual
screen, transparent through a magenta color key. Built from rusty-win-crab's
prototype, tested on the rustydemon Sylvanas script: runs pass with it up,
WindowFromPoint under a box still returns the app, gdigrab desktop capture
shows it and BAM's window-capture screenshots don't.

argv: --exclude-capture  hide it from screenshots and recordings too.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import sys
import threading
import time

HOLD = 1.6
KEY = 0x00FF00FF  # magenta, the transparent color (BGR)
EMBER = 0x00205AFF
RED = 0x003030E0
WHITE = 0x00FFFFFF
LABEL_BG = 0x00201C1C

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
# LAYERED | TRANSPARENT | TOPMOST | NOACTIVATE | TOOLWINDOW
WS_EX = 0x00080000 | 0x00000020 | 0x00000008 | 0x08000000 | 0x00000080
LWA_COLORKEY = 0x1
WM_PAINT, WM_DESTROY, WM_TIMER, WM_CLOSE = 0x000F, 0x0002, 0x0113, 0x0010
DT_NOPREFIX, DT_SINGLELINE, DT_CALCRECT = 0x0800, 0x0020, 0x0400
WDA_EXCLUDEFROMCAPTURE = 0x11

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD,
    wt.LPCWSTR,
    wt.LPCWSTR,
    wt.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wt.HWND,
    wt.HMENU,
    wt.HINSTANCE,
    wt.LPVOID,
]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wt.HDC),
        ("fErase", wt.BOOL),
        ("rcPaint", wt.RECT),
        ("fRestore", wt.BOOL),
        ("fIncUpdate", wt.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # per-monitor v2
VX, VY = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
VW, VH = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)

items: list[tuple] = []  # (expires_at, kind, coords, label, color)
lock = threading.Lock()


def _label(hdc, text: str, x: int, y: int) -> None:
    r = wt.RECT(x, y, x + 2000, y + 40)
    user32.DrawTextW(hdc, text, -1, ctypes.byref(r), DT_SINGLELINE | DT_NOPREFIX | DT_CALCRECT)
    pad = wt.RECT(r.left - 6, r.top - 3, r.right + 6, r.bottom + 3)
    bg = gdi32.CreateSolidBrush(LABEL_BG)
    user32.FillRect(hdc, ctypes.byref(pad), bg)
    gdi32.DeleteObject(bg)
    gdi32.SetTextColor(hdc, WHITE)
    user32.DrawTextW(hdc, text, -1, ctypes.byref(r), DT_SINGLELINE | DT_NOPREFIX)


def paint(hwnd) -> None:
    ps = PAINTSTRUCT()
    hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
    bg = gdi32.CreateSolidBrush(KEY)
    user32.FillRect(hdc, ctypes.byref(wt.RECT(0, 0, VW, VH)), bg)
    gdi32.DeleteObject(bg)
    old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))  # NULL_BRUSH
    gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
    font = gdi32.CreateFontW(-22, 0, 0, 0, 700, 0, 0, 0, 0, 0, 0, 4, 0, "Segoe UI")
    old_font = gdi32.SelectObject(hdc, font)
    now = time.monotonic()
    with lock:
        live = [i for i in items if i[0] > now]
    for _, kind, c, label, color in live:
        pen = gdi32.CreatePen(0, 4, color)
        old_pen = gdi32.SelectObject(hdc, pen)
        if kind == "box":
            x, y, w, h = int(c[0]) - VX, int(c[1]) - VY, int(c[2]), int(c[3])
            gdi32.Rectangle(hdc, x - 4, y - 4, x + w + 4, y + h + 4)
            tx, ty = x, y - 36
        elif kind == "arrow":
            fx, fy = int(c[0]) - VX, int(c[1]) - VY
            ex, ey = int(c[2]) - VX, int(c[3]) - VY
            gdi32.MoveToEx(hdc, fx, fy, None)
            gdi32.LineTo(hdc, ex, ey)
            d = 14 if ex >= fx else -14
            gdi32.MoveToEx(hdc, ex, ey, None)
            gdi32.LineTo(hdc, ex - d, ey - 10)
            gdi32.MoveToEx(hdc, ex, ey, None)
            gdi32.LineTo(hdc, ex - d, ey + 10)
            gdi32.Ellipse(hdc, fx - 8, fy - 8, fx + 8, fy + 8)
            tx, ty = min(fx, ex), fy - 44
        else:
            tx, ty = int(c[0]) - VX, int(c[1]) - VY
        gdi32.SelectObject(hdc, old_pen)
        gdi32.DeleteObject(pen)
        if label:
            _label(hdc, label, tx, max(ty, 4))
    gdi32.SelectObject(hdc, old_font)
    gdi32.DeleteObject(font)
    gdi32.SelectObject(hdc, old_brush)
    user32.EndPaint(hwnd, ctypes.byref(ps))


@WNDPROC
def wndproc(hwnd, msg, wp, lp):
    if msg == WM_PAINT:
        paint(hwnd)
        return 0
    if msg == WM_TIMER:  # repaint only while something is on screen or just expired
        with lock:
            had = bool(items)
            items[:] = [i for i in items if i[0] > time.monotonic()]
        if had:
            user32.InvalidateRect(hwnd, None, False)
        return 0
    if msg == WM_DESTROY:
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def add(m: dict) -> None:
    until = time.monotonic() + float(m.get("hold", HOLD))
    color = RED if m.get("kind") == "fail" else EMBER
    label = str(m.get("label", ""))
    for kind in ("box", "arrow", "text"):
        if kind in m:
            with lock:
                items.append((until, kind, m[kind], label, color))
            return


def reader(hwnd) -> None:
    for line in sys.stdin:
        try:
            add(json.loads(line))
        except (ValueError, TypeError, AttributeError):
            continue
        user32.InvalidateRect(hwnd, None, False)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)  # stdin ended: the run is over


def main() -> None:
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName="BamOverlay")
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        WS_EX, "BamOverlay", "BAM overlay", WS_POPUP, VX, VY, VW, VH, None, None, hinst, None
    )
    if not hwnd:
        print(f"CreateWindowExW failed ({kernel32.GetLastError()})", file=sys.stderr)
        sys.exit(1)
    user32.SetLayeredWindowAttributes(hwnd, KEY, 0, LWA_COLORKEY)
    if "--exclude-capture" in sys.argv:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
    user32.SetTimer(hwnd, 1, 100, None)
    threading.Thread(target=reader, args=(hwnd,), daemon=True).start()
    print("ready", flush=True)
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
