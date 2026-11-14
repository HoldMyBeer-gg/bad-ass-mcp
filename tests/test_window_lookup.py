"""Window lookup that works the same on every platform.

A script names its window once ("Rusty Demon") and that has to find the
app on macOS (where the listed name is the process, "rustydemon"), Windows
(the title) and Linux (the AT-SPI app name, empty for egui/accesskit). So a
pattern matches the listed name OR any top-level window title.

Also: on a Wayland session a native-Wayland client cannot know its global
position, so AT-SPI reports frame extents at 0,0. Bounds are reported only
when they are known to be real (X11, or the app's XWayland window).
"""

from __future__ import annotations

import json
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from bad_ass_mcp.bam.runner import run_script
from bad_ass_mcp.types import WindowInfo
from tests.fake_backend import FakeBackend
from tests.test_linux_internals import _make_backend

CONFIG = {"step_timeout": 0.2, "launch_timeout": 0.5}


def write(tmp_path, name, script):
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps({"name": name, "platforms": ["linux"], **script}))
    return p


# ── WindowInfo.matches ───────────────────────────────────────────────


def test_pattern_matches_the_listed_name():
    assert WindowInfo(id="1", name="Fake App", pid=1, focused=False).matches("fake")


def test_pattern_matches_a_window_title_when_the_name_differs():
    w = WindowInfo(
        id="1", name="rustydemon", pid=1, focused=False, titles=("Rusty Demon - CASC Explorer",)
    )
    assert w.matches("Rusty Demon")
    assert w.matches("rustydemon")
    assert not w.matches("Dolphin")


def test_pattern_matches_a_title_when_the_name_is_empty():
    w = WindowInfo(id="1", name="", pid=1, focused=False, titles=("Rusty Demon - CASC Explorer",))
    assert w.matches("rusty demon")


# ── runner: the config window is found by title ──────────────────────


class _NamedByProcess(FakeBackend):
    """macOS-style: listed under the process name, title only in `titles`."""

    def __init__(self, name="rustydemon"):
        super().__init__()
        self._name = name

    def list_windows(self):
        return [
            WindowInfo(
                id="fake-window-1",
                name=self._name,
                pid=9999,
                focused=True,
                titles=("Rusty Demon - CASC Explorer",),
            )
        ]


def test_config_window_is_found_by_title_on_macos_style_listing(tmp_path):
    script = write(tmp_path, "t", {"phases": []})
    cfg = {**CONFIG, "window": "Rusty Demon"}
    res = run_script(_NamedByProcess(), script, "linux", cfg, tmp_path / "out")
    assert res["status"] == "PASS", res.get("failure")


def test_config_window_is_found_by_title_when_the_app_name_is_empty(tmp_path):
    script = write(tmp_path, "t", {"phases": []})
    cfg = {**CONFIG, "window": "Rusty Demon"}
    res = run_script(_NamedByProcess(name=""), script, "linux", cfg, tmp_path / "out")
    assert res["status"] == "PASS", res.get("failure")


def test_config_window_may_be_per_platform(tmp_path):
    script = write(tmp_path, "t", {"phases": [{"name": "p", "checks": [{"windows": 1}]}]})
    cfg = {**CONFIG, "window": {"linux": "Fake App", "windows": "Something Else"}}
    res = run_script(FakeBackend(), script, "linux", cfg, tmp_path / "out")
    assert res["status"] == "PASS", res.get("failure")


def test_config_schema_accepts_a_per_platform_window():
    from bad_ass_mcp.bam.validate import validate_config

    assert validate_config({"window": {"macos": "rustydemon", "linux": "Rusty Demon"}}) == []


# ── runner: helper processes (portal file dialogs) ───────────────────


class _WithPortal(FakeBackend):
    """The portal process exists before launch and owns the file dialog."""

    def list_windows(self):
        portal = WindowInfo(
            id="portal",
            name="xdg-desktop-portal-kde",
            pid=4242,
            focused=False,
            titles=("Open File",),
        )
        return super().list_windows() + [portal]


class _Proc:
    pid = 9999

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


def _launching(monkeypatch):
    from bad_ass_mcp.bam import runner as runner_mod

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *a, **k: _Proc())


def test_a_named_helper_process_window_can_be_driven(tmp_path, monkeypatch):
    _launching(monkeypatch)
    script = write(
        tmp_path,
        "portal",
        {"phases": [{"name": "dialog", "window": "Open File", "learn": {}, "steps": []}]},
    )
    cfg = {**CONFIG, "window": "Fake App", "app": "fake-app", "helpers": ["xdg-desktop-portal"]}
    res = run_script(_WithPortal(), script, "linux", cfg, tmp_path / "out")
    assert res["status"] == "PASS", res.get("failure")


def test_without_helpers_a_pre_existing_process_is_still_never_driven(tmp_path, monkeypatch):
    _launching(monkeypatch)
    script = write(
        tmp_path,
        "portal",
        {"phases": [{"name": "dialog", "window": "Open File", "learn": {}, "steps": []}]},
    )
    cfg = {**CONFIG, "window": "Fake App", "app": "fake-app"}
    res = run_script(_WithPortal(), script, "linux", cfg, tmp_path / "out")
    assert res["status"] == "FAIL"
    assert res["failure"]["action"] == "window"


def test_config_schema_accepts_helpers():
    from bad_ass_mcp.bam.validate import validate_config

    assert validate_config({"window": "x", "helpers": ["xdg-desktop-portal"]}) == []
    assert validate_config({"window": "x", "helpers": {"linux": ["xdg-desktop-portal"]}}) == []


# ── Linux backend: titles and Wayland bounds ─────────────────────────


def _atspi_app(name, pid, frame_title, extents):
    frame = MagicMock()
    frame.get_child_count.return_value = 3
    ss = MagicMock()
    ss.contains.return_value = False
    frame.get_state_set.return_value = ss
    frame.get_name.return_value = frame_title
    ext = MagicMock(x=extents[0], y=extents[1], width=extents[2], height=extents[3])
    frame.get_extents.return_value = ext
    app = MagicMock()
    app.get_name.return_value = name
    app.get_process_id.return_value = pid
    app.get_child_count.return_value = 1
    app.get_child_at_index.return_value = frame
    app.get_toolkit_name.return_value = "accesskit"
    return app


def _listed(backend, mod, app, *, session, x11=()):
    env = {
        "XDG_SESSION_TYPE": session,
        "WAYLAND_DISPLAY": "wayland-0" if session == "wayland" else "",
    }
    desktop = MagicMock()
    desktop.get_child_count.return_value = 1
    desktop.get_child_at_index.return_value = app
    with (
        patch.dict("os.environ", env),
        patch.object(mod.Atspi, "get_desktop", return_value=desktop),
        patch.object(backend, "_x11_list_windows", return_value=list(x11)),
    ):
        return backend.list_windows()


def test_linux_window_titles_come_from_its_frames():
    backend, mod = _make_backend()
    app = _atspi_app("", 77, "Rusty Demon - CASC Explorer", (410, 177, 1100, 700))
    wins = _listed(backend, mod, app, session="x11")
    assert wins[0].titles == ("Rusty Demon - CASC Explorer",)
    assert wins[0].matches("Rusty Demon")


def test_linux_x11_session_keeps_atspi_bounds():
    backend, mod = _make_backend()
    app = _atspi_app("rustydemon", 77, "Rusty Demon", (410, 177, 1100, 700))
    wins = _listed(backend, mod, app, session="x11")
    assert wins[0].bounds == (410, 177, 1100, 700)


def test_linux_native_wayland_window_has_unknown_bounds():
    """AT-SPI says 0,0 because the client can't know where it is. Saying
    nothing beats a wrong origin that sends crops and clicks astray."""
    backend, mod = _make_backend()
    app = _atspi_app("rustydemon", 77, "Rusty Demon", (0, 0, 1100, 700))
    wins = _listed(backend, mod, app, session="wayland")
    assert wins[0].bounds is None


def test_linux_xwayland_window_reports_its_real_bounds_on_wayland():
    backend, mod = _make_backend()
    app = _atspi_app("rustydemon", 77, "Rusty Demon", (0, 0, 1100, 700))
    x11 = [
        WindowInfo(id="77", name="Rusty Demon", pid=77, focused=False, bounds=(410, 177, 1100, 700))
    ]
    wins = _listed(backend, mod, app, session="wayland", x11=x11)
    assert wins[0].bounds == (410, 177, 1100, 700)


def test_linux_window_geometry_refuses_wayland_frame_extents():
    """screenshot(window_id) crops to _window_geometry. With no XWayland
    window, the AT-SPI frame fallback must not hand back a 0,0 origin."""
    backend, mod = _make_backend()
    app = _atspi_app("rustydemon", 77, "Rusty Demon", (0, 0, 1100, 700))
    with (
        patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}),
        patch.object(backend, "_find_app", return_value=app),
        patch.object(mod.subprocess, "check_output", side_effect=FileNotFoundError),
    ):
        assert backend._window_geometry("77") is None


# ── macOS backend: titles from AXTitle ───────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="needs PyObjC")
def test_macos_window_titles_come_from_ax_windows():
    from bad_ass_mcp.backend import macos
    from bad_ass_mcp.backend.macos import MacOSBackend

    backend = MacOSBackend()
    app = MagicMock()
    app.processIdentifier.return_value = 4321
    app.localizedName.return_value = "rustydemon"
    ws = MagicMock()
    ws.runningApplications.return_value = [app]
    ws.frontmostApplication.return_value = app
    ax_win = object()

    def fake_ax_get(el, attr):
        if attr == "AXWindows":
            return [ax_win]
        if attr == "AXTitle" and el is ax_win:
            return "Rusty Demon - CASC Explorer"
        return None

    with (
        patch.object(macos, "NSWorkspace", MagicMock(sharedWorkspace=MagicMock(return_value=ws))),
        patch.object(macos, "AXUIElementCreateApplication"),
        patch.object(macos, "_ax_get", side_effect=fake_ax_get),
        patch.object(macos, "_cg_onscreen_windows", return_value=[]),
        patch.object(backend, "_cg_primary_bounds_for_pid", return_value=None),
    ):
        wins = backend.list_windows()
    assert wins[0].name == "rustydemon"
    assert wins[0].titles == ("Rusty Demon - CASC Explorer",)


# ── every backend's wait_for_window matches titles too ───────────────


@pytest.mark.parametrize("modname", ["linux", "macos", "windows"])
def test_wait_for_window_uses_the_shared_matcher(modname):
    import importlib

    path = importlib.util.find_spec(f"bad_ass_mcp.backend.{modname}").origin
    src = open(path, encoding="utf-8").read()
    body = src[src.index("def wait_for_window") :]
    body = body[: body.index("\n    def ", 10)]
    assert ".matches(title_pattern)" in body


# ── follow-ups from the Deck and macOS runs ─────────────────────────


def _node_for_pid(pid, extents):
    node = MagicMock()
    node.get_process_id.return_value = pid
    ext = MagicMock(x=extents[0], y=extents[1], width=extents[2], height=extents[3])
    comp = MagicMock()
    comp.get_extents.return_value = ext
    node.get_component.return_value = comp
    node.get_component_iface = MagicMock(return_value=comp)
    return node, comp


def _bounds_on(backend, mod, node, comp, *, session, xwid):
    env = {
        "XDG_SESSION_TYPE": session,
        "WAYLAND_DISPLAY": "wayland-0" if session == "wayland" else "",
    }
    with (
        patch.dict("os.environ", env),
        patch.object(backend, "_resolve", return_value=node),
        patch.object(mod, "_iface", return_value=comp),
        patch.object(backend, "_find_xwid", return_value=xwid),
    ):
        return backend.element_bounds("h")


def test_linux_element_bounds_are_unknown_for_a_native_wayland_app():
    """Measured on the Deck: a native-Wayland app's element extents are
    window-relative (Search at 322,34 where XWayland says 772,232)."""
    backend, mod = _make_backend()
    node, comp = _node_for_pid(77, (322, 34, 60, 24))
    assert _bounds_on(backend, mod, node, comp, session="wayland", xwid=None) is None


def test_linux_element_bounds_are_real_for_an_xwayland_app():
    backend, mod = _make_backend()
    node, comp = _node_for_pid(77, (772, 232, 60, 24))
    assert _bounds_on(backend, mod, node, comp, session="wayland", xwid="0x400007") == (
        772.0,
        232.0,
        60.0,
        24.0,
    )


def test_linux_element_bounds_on_x11_are_trusted():
    backend, mod = _make_backend()
    node, comp = _node_for_pid(77, (772, 232, 60, 24))
    assert _bounds_on(backend, mod, node, comp, session="x11", xwid=None) is not None


def test_screenshot_of_a_native_wayland_window_says_why():
    backend, mod = _make_backend()
    with (
        patch.dict("os.environ", {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0"}),
        patch.object(backend, "_find_app", return_value=MagicMock(get_child_count=lambda: 0)),
        patch.object(backend, "_window_geometry", return_value=None),
    ):
        with pytest.raises(ValueError, match="Wayland"):
            backend.screenshot("77", output_path="/tmp/x.png")


def test_a_wrong_form_says_what_the_allowed_forms_are():
    from bad_ass_mcp.bam.validate import validate_config

    issues = validate_config({"window": 5})
    assert issues
    msg = issues[0].message
    assert "a string" in msg and "per-platform" in msg


def test_run_without_the_platform_extra_is_one_line(tmp_path):
    from bad_ass_mcp.bam import cli

    with (
        patch(
            "bad_ass_mcp.server._backend",
            side_effect=RuntimeError(
                "macOS backend requires PyObjC: pip install 'bad-ass-mcp[macos]'"
            ),
        ),
        patch.object(cli, "_problems", return_value=[]),
    ):
        with pytest.raises(SystemExit) as e:
            cli.main([str(tmp_path)])
    assert "PyObjC" in str(e.value.code)


def test_a_pointer_click_without_bounds_gives_the_real_reason():
    """A right-click never uses a11y invoke, so 'no actionable pattern'
    isn't why it failed."""
    desk = FakeBackend()
    with patch.object(desk, "element_bounds", return_value=None):
        r = desk._click_element_at("btn-ok", button="right")
    assert not r.ok
    assert "no screen bounds" in r.error
    assert "a11y pattern" not in r.error


def test_linux_untitled_atspi_window_takes_its_x11_title():
    """Seen on Kali X11: an egui/accesskit build with no AT-SPI name and an
    untitled frame, while X11 WM_NAME carries the title."""
    backend, mod = _make_backend()
    app = _atspi_app("", 69360, "", (5, 64, 1014, 699))
    x11 = [
        WindowInfo(
            id="69360",
            name="Rusty Demon - CASC Explorer",
            pid=69360,
            focused=False,
            bounds=(5, 64, 1014, 699),
            accessible=False,
        )
    ]
    wins = _listed(backend, mod, app, session="x11", x11=x11)
    assert len(wins) == 1
    assert wins[0].accessible is True
    assert wins[0].titles == ("Rusty Demon - CASC Explorer",)
    assert wins[0].matches("Rusty Demon")


def _x11_then_atspi(backend, polls_before_atspi):
    """list_windows that shows only the X11 entry for the first N polls,
    as on Kali right after launch, then the AT-SPI entry."""
    x11_only = WindowInfo(
        id="71550", name="Rusty Demon - CASC Explorer", pid=71550, focused=True, accessible=False
    )
    atspi = WindowInfo(
        id="71550", name="", pid=71550, focused=True, titles=("Rusty Demon - CASC Explorer",)
    )
    calls = {"n": 0}

    def listing():
        calls["n"] += 1
        return [x11_only] if calls["n"] <= polls_before_atspi else [atspi]

    return patch.object(backend, "list_windows", side_effect=listing)


def test_linux_wait_for_window_lets_atspi_catch_up_after_an_x11_only_match():
    backend, _mod = _make_backend()
    with _x11_then_atspi(backend, 2), patch("time.sleep"):
        w = backend.wait_for_window("Rusty Demon", timeout=5)
    assert w.accessible is True


def test_linux_wait_for_window_still_returns_a_genuinely_x11_only_window():
    backend, _mod = _make_backend()
    backend._ATSPI_GRACE_S = 0.05
    started = time.monotonic()
    with _x11_then_atspi(backend, 10**9):
        w = backend.wait_for_window("Rusty Demon", timeout=5)
    assert w is not None and w.accessible is False
    # Bounded by time (Kali measured ~3 s when it was 15 polls).
    assert time.monotonic() - started < 1.0


# ── a helper process is not one of its own windows ───────────────────────────
# The portal is listed whether or not a dialog is open. `no_window` must count
# the windows it shows, not the process, or it can never pass against a helper.


def _el(role, name="", children=()):
    from bad_ass_mcp.types import ElementHandle

    return ElementHandle(id=f"{role}-{name}", role=role, name=name, children=list(children))


class _PortalProcess(FakeBackend):
    """Lists the portal as an application; `dialog_role` is the role of the
    dialog it currently shows (None: no dialog open)."""

    dialog_role: str | None = None

    def list_windows(self):
        portal = WindowInfo(
            id="portal", name="xdg-desktop-portal-kde", pid=4242, focused=False, titles=()
        )
        return super().list_windows() + [portal]

    def get_tree(self, window_id, *, max_depth=None):
        if window_id != "portal":
            return super().get_tree(window_id, max_depth=max_depth)
        kids = [_el(self.dialog_role, "Save")] if self.dialog_role else []
        return _el("application", "xdg-desktop-portal-kde", kids)


def _no_window_run(tmp_path, monkeypatch, backend):
    _launching(monkeypatch)
    script = write(
        tmp_path,
        "closed",
        {"phases": [], "checks": [{"no_window": "xdg-desktop-portal-kde"}]},
    )
    cfg = {**CONFIG, "window": "Fake App", "app": "fake-app", "helpers": ["xdg-desktop-portal"]}
    return run_script(backend, script, "linux", cfg, tmp_path / "out")


def test_no_window_passes_when_a_helper_process_shows_no_dialog(tmp_path, monkeypatch):
    res = _no_window_run(tmp_path, monkeypatch, _PortalProcess())
    assert res["status"] == "PASS", res.get("failure")


@pytest.mark.parametrize("role", ["dialog", "file chooser", "frame"])
def test_no_window_fails_while_a_helper_process_shows_a_dialog(tmp_path, monkeypatch, role):
    backend = _PortalProcess()
    backend.dialog_role = role
    res = _no_window_run(tmp_path, monkeypatch, backend)
    assert res["status"] == "FAIL"
    assert "unexpected window" in res["failure"]["reason"]


# ── failure evidence comes from the window the phase ran in ──────────────────


class _DialogNoScreenshot(_PortalProcess):
    """A dialog whose screenshot can't be taken (as for a portal window on
    Wayland), so no fail.png is written for it."""

    dialog_role = "dialog"

    def list_windows(self):
        wins = super().list_windows()
        wins[-1] = WindowInfo(
            id="portal", name="xdg-desktop-portal-kde", pid=4242, focused=False, titles=("Save",)
        )
        return wins

    def screenshot(self, window_id=None, output_path=None):
        if window_id == "portal":
            raise RuntimeError("no screenshot for this window")
        return super().screenshot(window_id, output_path)

    def learn_layout(self, window_id, descriptors):
        if window_id == "portal":
            return {k: None for k in descriptors}
        return super().learn_layout(window_id, descriptors)


def test_the_failure_tree_is_the_dialogs_even_without_a_screenshot(tmp_path, monkeypatch):
    _launching(monkeypatch)
    script = write(
        tmp_path,
        "dialog-fails",
        {
            "phases": [
                {
                    "name": "save",
                    "window": "xdg-desktop-portal-kde",
                    "learn": {"missing": {"name": "not there"}},
                    "steps": [],
                }
            ]
        },
    )
    cfg = {
        **CONFIG,
        "window": "Fake App",
        "app": "fake-app",
        "helpers": ["xdg-desktop-portal"],
        "step_timeout": 0.1,
    }
    out = tmp_path / "out"
    res = run_script(_DialogNoScreenshot(), script, "linux", cfg, out)
    assert res["status"] == "FAIL"
    tree = json.loads((out / "tree.json").read_text())
    assert tree["name"] == "xdg-desktop-portal-kde", tree["name"]
