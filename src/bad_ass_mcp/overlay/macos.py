"""macOS annotation overlay (started by bad_ass_mcp.overlay.start_overlay).

A borderless, transparent, click-through window above everything on every
screen, owned by an accessory app (no Dock icon, never activated). Items
arrive in AX coordinates: points, origin at the top-left of the primary
screen, y down.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSScreen,
    NSScreenSaverWindowLevel,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorIgnoresCycle,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSAttributedString, NSMakeRect, NSObject, NSTimer, NSUnionRect

HOLD = 1.6
inbox: queue.Queue = queue.Queue()
items: list[tuple] = []  # (expires_at, kind, coords, label, fail, below)
OX = OY = 0.0  # AX point + (OX, OY) = view point


def _color(r, g, b, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)


EMBER = _color(1.0, 0.35, 0.13)
RED = _color(0.88, 0.19, 0.19)
LABEL_BG = _color(0.11, 0.11, 0.13, 0.85)


def _label(text: str, x: float, y: float, fail: bool = False) -> None:
    attrs = {
        NSFontAttributeName: NSFont.boldSystemFontOfSize_(15),
        NSForegroundColorAttributeName: NSColor.whiteColor(),
    }
    s = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
    size = s.size()
    (RED if fail else LABEL_BG).setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(x - 6, y - 3, size.width + 12, size.height + 6), 5, 5
    ).fill()
    s.drawAtPoint_((x, y))


class OverlayWindow(NSWindow):
    """Hidden from accessibility, so BAM's list_windows (and any screen
    reader) never sees the overlay as an app window."""

    def isAccessibilityElement(self):
        return False

    def accessibilityIsIgnored(self):
        return True


class OverlayView(NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        now = time.monotonic()
        for _, kind, c, label, fail, below in [i for i in items if i[0] > now]:
            (RED if fail else EMBER).set()
            if kind == "box":
                x, y, w, h = c[0] + OX, c[1] + OY, c[2], c[3]
                p = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSMakeRect(x - 4, y - 4, w + 8, h + 8), 6, 6
                )
                p.setLineWidth_(3)
                p.stroke()
                tx, ty = x - 4, (y + h + 10 if below else y - 32)
            elif kind == "arrow":
                fx, fy, ex, ey = c[0] + OX, c[1] + OY, c[2] + OX, c[3] + OY
                p = NSBezierPath.bezierPath()
                p.setLineWidth_(3)
                p.moveToPoint_((fx, fy))
                p.lineToPoint_((ex, ey))
                d = 14 if ex >= fx else -14
                p.moveToPoint_((ex - d, ey - 10))
                p.lineToPoint_((ex, ey))
                p.lineToPoint_((ex - d, ey + 10))
                p.stroke()
                NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(fx - 6, fy - 6, 12, 12)).fill()
                tx, ty = min(fx, ex), fy - 40
            else:
                tx, ty = c[0] + OX, c[1] + OY
            if label:
                _label(label, tx, max(ty, 4), fail)


class Ticker(NSObject):
    def tick_(self, timer):
        changed = False
        while True:
            try:
                m = inbox.get_nowait()
            except queue.Empty:
                break
            if m is None:
                NSApplication.sharedApplication().terminate_(None)
                return
            until = time.monotonic() + float(m.get("hold", HOLD))
            for kind in ("box", "arrow", "text"):
                if kind in m:
                    items.append(
                        (
                            until,
                            kind,
                            m[kind],
                            str(m.get("label", "")),
                            m.get("kind") == "fail",
                            bool(m.get("below")),
                        )
                    )
                    changed = True
                    break
        now = time.monotonic()
        if any(i[0] <= now for i in items):
            items[:] = [i for i in items if i[0] > now]
            changed = True
        if changed:
            self.view.setNeedsDisplay_(True)


def reader() -> None:
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict):
            inbox.put(m)
    inbox.put(None)


def main() -> None:
    global OX, OY
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    screens = NSScreen.screens()
    frame = screens[0].frame()
    primary_h = frame.size.height
    for s in screens[1:]:
        frame = NSUnionRect(frame, s.frame())
    # Cocoa y runs up from the primary screen's bottom; AX y runs down from
    # its top. In a flipped view spanning `frame`: view y = AX y + OY.
    OX = -frame.origin.x
    OY = (frame.origin.y + frame.size.height) - primary_h
    win = OverlayWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    win.setOpaque_(False)
    win.setBackgroundColor_(NSColor.clearColor())
    win.setHasShadow_(False)
    win.setIgnoresMouseEvents_(True)
    win.setLevel_(NSScreenSaverWindowLevel)
    win.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces
        | NSWindowCollectionBehaviorStationary
        | NSWindowCollectionBehaviorIgnoresCycle
    )
    win.setReleasedWhenClosed_(False)
    view = OverlayView.alloc().initWithFrame_(NSMakeRect(0, 0, frame.size.width, frame.size.height))
    win.setContentView_(view)
    win.orderFrontRegardless()
    ticker = Ticker.alloc().init()
    ticker.view = view
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.05, ticker, "tick:", None, True
    )
    threading.Thread(target=reader, daemon=True).start()
    print("ready", flush=True)
    app.run()


if __name__ == "__main__":
    main()
