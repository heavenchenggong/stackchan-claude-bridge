"""Local verification for stackchan_push (no live device required).

Runs a *mock device* — an asyncio WebSocket server that validates the shared
token and, for play_audio_url, actually HTTP-GETs the advertised URL just like the
firmware would — so the whole Mac half (synthesize → serve → send → confirm-fetch)
is exercised end-to-end against the pinned interface contract.

Run directly:   bridge/.venv/bin/python bridge/tests/test_stackchan_push.py
Or with pytest: bridge/.venv/bin/python -m pytest bridge/tests/ -q
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sys
import urllib.request
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stackchan_push as sp  # noqa: E402

sp.FETCH_GRACE = 0.05  # keep the E2E fast

HAS_TTS = shutil.which("say") is not None and shutil.which("ffmpeg") is not None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _cfg(device_port: int, serve_port: int, token: str = "s3cr3t") -> sp.Config:
    return sp.Config(
        device_ip="127.0.0.1",
        device_port=device_port,
        device_ws_path="/ws",
        token=token,
        mac_ip="127.0.0.1",
        serve_port=serve_port,
        serve_dir=Path("/tmp/sc-test"),
        opus_rate=16000,
        opus_frame_ms=60,
        tts_voice=None,
        say_bin="say",
        ffmpeg_bin="ffmpeg",
    )


class MockDevice:
    """Mimics the firmware LAN WS server per the contract."""

    def __init__(self, token: str = "s3cr3t", *, fetch_urls: bool = True):
        self.token = token
        self.fetch_urls = fetch_urls
        self.received: list[dict] = []
        self.fetched_bytes: bytes | None = None
        self._server = None

    async def __aenter__(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            payload = msg["payload"]
            params = payload["params"]
            args = params["arguments"]
            self.received.append({"name": params["name"], "arguments": args})
            reply_id = payload["id"]

            if args.get("token") != self.token:  # auth per "token on every request"
                await ws.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": reply_id,
                            "error": {"code": -32001, "message": "invalid token"},
                        }
                    )
                )
                continue

            if params["name"] == "self.play_audio_url" and self.fetch_urls:
                url = args["url"]
                self.fetched_bytes = await asyncio.to_thread(
                    lambda: urllib.request.urlopen(url, timeout=5).read()
                )

            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": reply_id,
                        "result": {"success": True},
                    }
                )
            )


# ── pure / contract tests ─────────────────────────────────────────────────────────
def test_envelope_matches_contract():
    env = sp.build_envelope(
        "self.play_audio_url", {"url": "http://m:8790/utt-1.ogg"}, "TOK", 7
    )
    assert env["type"] == "mcp"
    p = env["payload"]
    assert p["jsonrpc"] == "2.0" and p["id"] == 7 and p["method"] == "tools/call"
    assert p["params"]["name"] == "self.play_audio_url"
    assert p["params"]["arguments"] == {
        "url": "http://m:8790/utt-1.ogg",
        "token": "TOK",
    }
    print("✓ envelope matches the wire contract")


def test_token_injected_on_every_tool():
    for tool, args in (
        ("self.face.expression", {"emotion": "happy"}),
        ("self.head.nod", {}),
    ):
        env = sp.build_envelope(tool, args, "TOK", 1)
        assert env["payload"]["params"]["arguments"]["token"] == "TOK"
    print("✓ token injected into arguments on every tool")


def test_gesture_validation():
    try:
        asyncio.run(sp.gesture(_cfg(1, 2), "wiggle"))
    except sp.PushError:
        print("✓ unknown gesture rejected")
    else:
        raise AssertionError("expected PushError for unknown gesture")


def test_lan_ip_prefers_device_subnet():
    orig = sp._local_ipv4s
    sp._local_ipv4s = lambda: [("utun3", "10.5.0.2"), ("en0", "192.168.0.219")]
    try:
        assert (
            sp.detect_lan_ip("192.168.0.50") == "192.168.0.219"
        )  # subnet match, not VPN
        assert sp.detect_lan_ip("10.5.0.99") == "10.5.0.2"
    finally:
        sp._local_ipv4s = orig
    print("✓ LAN IP detection prefers the device's subnet (skips VPN)")


# ── audio synth ────────────────────────────────────────────────────────────────────
def test_synthesize_produces_valid_ogg():
    if not HAS_TTS:
        print("• skip synth (say/ffmpeg absent)")
        return
    ogg = sp.synthesize(_cfg(1, 2), "测试 speak 路径")
    assert ogg.exists() and ogg.stat().st_size > 0
    assert ogg.read_bytes()[:4] == b"OggS"
    print(f"✓ synthesize → valid non-empty OGG ({ogg.stat().st_size} bytes)")


# ── E2E against the mock device ─────────────────────────────────────────────────────
async def _run_express_ok():
    async with MockDevice() as dev:
        cfg = _cfg(dev.port, _free_port())
        await sp.express(cfg, "happy")
        assert dev.received[-1] == {
            "name": "self.face.expression",
            "arguments": {"emotion": "happy", "token": "s3cr3t"},
        }


def test_express_roundtrip():
    asyncio.run(_run_express_ok())
    print("✓ express → device got self.face.expression + token, replied ok")


async def _run_bad_token():
    async with MockDevice(token="right") as dev:
        cfg = _cfg(dev.port, _free_port(), token="wrong")
        try:
            await sp.gesture(cfg, "nod")
        except sp.DeviceError:
            return
        raise AssertionError("expected DeviceError on bad token")


def test_bad_token_errors():
    asyncio.run(_run_bad_token())
    print("✓ bad token → DeviceError (device rejected)")


async def _run_unreachable():
    cfg = _cfg(_free_port(), _free_port())  # nothing listening on device port
    try:
        await sp.express(cfg, "sad")
    except sp.DeviceUnreachable:
        return
    raise AssertionError("expected DeviceUnreachable")


def test_unreachable_errors():
    asyncio.run(_run_unreachable())
    print("✓ device unreachable → DeviceUnreachable (clear error)")


async def _run_speak_e2e():
    async with MockDevice() as dev:
        cfg = _cfg(dev.port, _free_port())
        await sp.speak(cfg, "跑完了，Sharpe 一点八", face="happy", gesture_kind="nod")
        names = [r["name"] for r in dev.received]
        assert names[0] == "self.play_audio_url"
        assert "self.face.expression" in names and "self.head.nod" in names
        # the device actually fetched the served .ogg and it is a valid OGG stream
        assert dev.fetched_bytes and dev.fetched_bytes[:4] == b"OggS"
        return len(dev.fetched_bytes)


def test_speak_end_to_end():
    if not HAS_TTS:
        print("• skip speak E2E (say/ffmpeg absent)")
        return
    n = asyncio.run(_run_speak_e2e())
    print(
        f"✓ speak E2E: synth → serve → play_audio_url → device fetched {n} bytes → +face +nod"
    )


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
    print("─" * 50)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
