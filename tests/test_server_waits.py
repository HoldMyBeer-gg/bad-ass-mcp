"""The wait_* tools poll on the event loop instead of blocking it.

A sync tool runs inline in the server's event loop, so a 180 s
wait_for_element starved everything behind it: a screenshot queued for the
whole wait, and a notifications/cancelled from the client was read only
after the result had already gone out. Claude Code then saw a response for
a request it had given up on and dropped the stdio connection. Polling
with an async sleep between probes serves other calls during a wait and
lets a cancel end it with no response at all."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import anyio
import pytest
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage

from bad_ass_mcp import server
from tests.fake_backend import FAKE_WINDOW_ID, FakeBackend


@pytest.fixture
def backend():
    b = FakeBackend()
    with patch.object(server, "_backend", lambda: b):
        yield b


async def _ticks_during(coro, seconds: float) -> int:
    """How many times a 5 ms ticker got the loop while `coro` ran."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    t = asyncio.ensure_future(ticker())
    try:
        await asyncio.wait_for(coro, seconds)
    finally:
        t.cancel()
    return ticks


def test_wait_for_element_yields_to_the_loop_while_it_polls(backend):
    ticks = asyncio.run(
        _ticks_during(
            server.wait_for_element(FAKE_WINDOW_ID, role="button", name="Nope", timeout=0.3),
            2.0,
        )
    )
    assert ticks >= 10


def test_wait_for_window_yields_to_the_loop_while_it_polls(backend):
    ticks = asyncio.run(_ticks_during(server.wait_for_window("Ghost", timeout=0.3), 2.0))
    assert ticks >= 10


def test_wait_for_text_yields_to_the_loop_while_it_polls(backend):
    ticks = asyncio.run(_ticks_during(server.wait_for_text("txt-name", "never", timeout=0.3), 2.0))
    assert ticks >= 10


def test_results_keep_their_shape(backend):
    async def go():
        hit = await server.wait_for_element(FAKE_WINDOW_ID, role="button", name="OK", timeout=0.5)
        miss = await server.wait_for_element(
            FAKE_WINDOW_ID, role="button", name="Nope", timeout=0.1
        )
        backend.schedule_window(delay=0.05)
        win = await server.wait_for_window("Fake App", timeout=1.0)
        no_win = await server.wait_for_window("Ghost", timeout=0.1)
        text = await server.wait_for_text("combo-size", "Medium", timeout=0.5)
        no_text = await server.wait_for_text("txt-name", "never", timeout=0.1)
        return hit, miss, win, no_win, text, no_text

    hit, miss, win, no_win, text, no_text = asyncio.run(go())
    assert hit["id"] == "btn-ok" and hit["states"] == ["enabled"]
    assert miss == {"error": "Timeout: element not found"}
    assert win["name"] == "Fake App"
    assert no_win["error"].startswith("Timeout: no window matching")
    assert text == {"ok": True, "value": "Medium"}
    assert no_text["ok"] is False and no_text["error"].startswith("Timeout:")


def test_a_late_element_is_still_found(backend):
    async def go():
        async def rename():
            await asyncio.sleep(0.15)
            backend._values["txt-name"] = "late"

        asyncio.ensure_future(rename())
        return await server.wait_for_text("txt-name", "late", timeout=2.0)

    assert asyncio.run(go()) == {"ok": True, "value": "late"}


def test_a_cancelled_wait_stops_probing(backend):
    probes = []
    real = backend.find_elements

    def counting(*a, **kw):
        probes.append(time.monotonic())
        return real(*a, **kw)

    backend.find_elements = counting

    async def go():
        task = asyncio.ensure_future(
            server.wait_for_element(FAKE_WINDOW_ID, role="button", name="Nope", timeout=5.0)
        )
        await asyncio.sleep(0.25)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        seen = len(probes)
        await asyncio.sleep(0.3)
        return seen, len(probes)

    seen, after = asyncio.run(go())
    assert seen >= 2
    assert after == seen


# ── through the MCP server itself ─────────────────────────────────────


def _rpc(method: str, id: int | None = None, **params) -> SessionMessage:
    body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
    if id is not None:
        body["id"] = id
    return SessionMessage(JSONRPCMessage.model_validate(body))


def _call(id: int, tool: str, **arguments) -> SessionMessage:
    return _rpc("tools/call", id, name=tool, arguments=arguments)


async def _session(script):
    """Run the real server over in-memory streams and hand `script` the
    client ends. Returns every message the server wrote, as dicts."""
    low = server.mcp._mcp_server
    got: list[dict] = []
    async with create_client_server_memory_streams() as (client, srv):
        read, write = client
        async with anyio.create_task_group() as tg:
            tg.start_soon(low.run, srv[0], srv[1], low.create_initialization_options())

            async def collect():
                async for m in read:
                    if isinstance(m, Exception):
                        raise m
                    got.append(json.loads(m.message.model_dump_json(exclude_none=True)))

            tg.start_soon(collect)
            await write.send(
                _rpc(
                    "initialize",
                    1,
                    protocolVersion="2025-06-18",
                    capabilities={},
                    clientInfo={"name": "test", "version": "0"},
                )
            )
            await write.send(_rpc("notifications/initialized"))
            await script(write)
            tg.cancel_scope.cancel()
    return got


def test_a_cancelled_call_gets_no_reply_and_the_next_call_is_served(backend):
    """The trail from the field: wait (id 2), cancel it, another call (id 3).
    The other call must be answered long before the wait's timeout, and
    nothing at all may be sent for id 2 afterwards: the client has dropped
    its handler, and a late reply is what tore the connection down."""

    async def script(write):
        await write.send(
            _call(2, "wait_for_element", window_id=FAKE_WINDOW_ID, name="Nope", timeout=3.0)
        )
        await anyio.sleep(0.15)
        await write.send(_rpc("notifications/cancelled", requestId=2, reason="user"))
        t0 = time.monotonic()
        await write.send(_call(3, "list_windows"))
        await anyio.sleep(0.6)
        assert time.monotonic() - t0 < 2.0

    got = anyio.run(_session, script)
    ids = [m.get("id") for m in got]
    assert 3 in ids, got
    assert 2 not in ids, [m for m in got if m.get("id") == 2]


def test_a_call_made_during_a_wait_is_answered_while_the_wait_runs(backend):
    async def script(write):
        await write.send(
            _call(2, "wait_for_element", window_id=FAKE_WINDOW_ID, name="Nope", timeout=1.5)
        )
        await anyio.sleep(0.1)
        await write.send(_call(3, "list_windows"))
        await anyio.sleep(0.5)

    got = anyio.run(_session, script)
    ids = [m.get("id") for m in got]
    assert 3 in ids and 2 not in ids, ids
