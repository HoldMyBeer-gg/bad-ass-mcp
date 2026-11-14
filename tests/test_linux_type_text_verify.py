"""Linux type_text checks the field afterwards, like Windows and macOS.

On the Deck, type_text("FrameXML") into a native-Wayland egui field
returned ok=True while the field stayed empty (rusty-deck-crab)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.test_linux_internals import _make_backend


def _field(value_after):
    """A node whose text interface reads `value_after` (None: no text iface)."""
    node = MagicMock()
    node.get_process_id.return_value = 77
    node.get_application.return_value.get_process_id.return_value = 77  # native widget
    return node


def _run(backend, mod, node, *, reads, xwid=None, editable_ok=True):
    text_iface = MagicMock()
    if reads is None:
        text_iface.get_text.side_effect = Exception("no text interface")
    else:
        text_iface.get_text.return_value = reads
    editable = MagicMock()
    if not editable_ok:
        editable.set_text_contents.side_effect = Exception("ignored")

    def iface(_node, kind):
        if kind == "text":
            return text_iface
        if kind == "editable_text":
            return editable
        raise Exception(f"no {kind}")

    with (
        patch.object(backend, "_resolve", return_value=node),
        patch.object(mod, "_iface", side_effect=iface),
        patch.object(backend, "_find_xwid", return_value=xwid),
        patch.object(mod.Atspi, "generate_keyboard_event"),
        patch("time.sleep"),
    ):
        return backend.type_text("h", "FrameXML")


def test_text_that_never_landed_is_a_failure():
    backend, mod = _make_backend()
    r = _run(backend, mod, _field(""), reads="")
    assert not r.ok


def test_text_that_landed_is_ok():
    backend, mod = _make_backend()
    r = _run(backend, mod, _field("FrameXML"), reads="FrameXML")
    assert r.ok


def test_a_field_that_cannot_be_read_back_is_trusted():
    backend, mod = _make_backend()
    r = _run(backend, mod, _field(None), reads=None)
    assert r.ok


def test_a_password_field_reading_bullets_of_the_right_length_is_ok():
    backend, mod = _make_backend()
    r = _run(backend, mod, _field("•" * 8), reads="•" * 8)
    assert r.ok


def test_the_failure_names_what_the_field_reads():
    backend, mod = _make_backend()
    node = _field("")
    with (
        patch.object(backend, "_type_text_once", return_value=mod.ActionResult(ok=True)),
        patch.object(backend, "_resolve", return_value=node),
        patch.object(backend, "_read_text", return_value=""),
    ):
        r = backend.type_text("h", "FrameXML")
    assert not r.ok and "reads ''" in r.error


# ── slow apps (rusty-kali-crab, XFCE/X11, rustydemon reading over sshfs) ──
#
# type_text("*.mesh") came back ok=False, "the field reads '*.me'", while a
# get_value a moment later read the whole text. The readback ran straight
# after the keystrokes, before a busy app had applied them all.


def _settling(backend, mod, reads):
    """type_text with the readback returning `reads` in turn (the last one
    repeats), with sleep stubbed and the clock advancing 0.1 s per look."""
    node = _field("")
    seq = list(reads)
    ticks = iter(i * 0.1 for i in range(200))

    def read(_node):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    with (
        patch.object(backend, "_type_text_once", return_value=mod.ActionResult(ok=True)),
        patch.object(backend, "_resolve", return_value=node),
        patch.object(backend, "_read_text", side_effect=read),
        patch("time.sleep"),
        patch("time.monotonic", side_effect=lambda: next(ticks)),
    ):
        return backend.type_text("h", "*.mesh")


def test_a_slow_app_that_catches_up_within_a_second_is_ok():
    backend, mod = _make_backend()
    r = _settling(backend, mod, ["*.me", "*.mes", "*.mesh"])
    assert r.ok


def test_a_prefix_that_never_completes_says_only_that_much_arrived():
    backend, mod = _make_backend()
    with patch.object(mod, "_is_wayland_session", return_value=False):
        r = _settling(backend, mod, ["*.me"])
    assert not r.ok
    assert "reads '*.me'" in r.error
    assert "only a prefix" in r.error
    assert "Wayland" not in r.error


def test_the_wayland_hint_is_given_only_on_a_wayland_session():
    backend, mod = _make_backend()
    with patch.object(mod, "_is_wayland_session", return_value=True):
        on = _settling(backend, mod, [""])
    with patch.object(mod, "_is_wayland_session", return_value=False):
        off = _settling(backend, mod, [""])
    assert not on.ok and "Wayland" in on.error
    assert not off.ok and "Wayland" not in off.error
