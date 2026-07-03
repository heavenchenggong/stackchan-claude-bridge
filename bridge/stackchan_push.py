"""stackchan_push.py — Mac → Stack-chan LAN push (speak / express / gesture).

Mac-**initiated** control of the Stack-chan robot over its LAN WebSocket control
server. This is the *opposite direction* of the existing bridge: `server.py` +
`mcp_pipe.py` are cloud → Mac (the xiaozhi LLM PULLs the Mac's tools). This module
is Mac → device — an agent on this Mac decides, on its own initiative, to make the
robot say something / change face / gesture, with no cloud and no human turn.

Design: Path A2 from the scout report (see docs/mac-push.md):

    speak(text)
      1. TTS on the Mac:   say -o /tmp/sc/utt.aiff "<text>"
      2. transcode to OGG/Opus:
           ffmpeg -y -i utt.aiff -c:a libopus -ar 16000 -ac 1 -frame_duration 60 -f ogg utt-<uniq>.ogg
      3. serve /tmp/sc on a tiny HTTP server bound to the Mac's LAN IP
      4. send the device ONE MCP call over its LAN WebSocket (ws://<device-ip>:8080/ws):
           self.play_audio_url {"url": "http://<mac-ip>:<port>/utt-<uniq>.ogg", "token": "<shared>"}
    express(face)   -> self.face.expression {"emotion": face}
    gesture(kind)   -> self.head.nod / self.head.shake

The audio comes from the Mac because the device has **no on-device TTS** (report §1.3);
the device side just fetches the URL, demuxes the OGG, and plays the Opus frames.

── Interface contract (MUST match the firmware, built in parallel) ────────────────
  LAN endpoint : ws://<device-ip>:8080/ws
  Envelope     : {"type":"mcp","payload":{"jsonrpc":"2.0","id":<n>,
                   "method":"tools/call","params":{"name":"<tool>","arguments":{...}}}}
  Speak tool   : self.play_audio_url  args {"url": "...", "token": "<shared>"}
  Expression   : self.face.expression args {"emotion": "<face>"}
  Gesture      : self.head.nod / self.head.shake
  Auth         : `token` is included in `arguments` on *every* request (see NOTE below).

  NOTE on token placement: the contract's only explicit example puts `token` inside
  `arguments` (play_audio_url). To honour "include token on every request" we inject
  it into `arguments` for every tool call. If the firmware ends up checking it at the
  envelope level instead, that is a one-line change in `build_envelope()`.

  NOTE on audio format: the firmware Opus decoder dictates the exact sample rate /
  frame duration. Until the firmware PR pins it, we default to the reference impl's
  proven values — 16 kHz mono, 60 ms Opus frames, OGG container — and make the rate
  (SC_OPUS_RATE) and frame duration (SC_OPUS_FRAME_MS) env-configurable so matching
  the firmware is a one-line change. (ffprobe reports the OGG header rate as 48000 Hz;
  that is standard Ogg-Opus behaviour — the source is resampled to SC_OPUS_RATE and
  the decoder resamples on playback.)

── Config (env / bridge/.env, never hardcoded secrets) ───────────────────────────
  SC_DEVICE_IP      device LAN IP                     (required for a real send)
  SC_DEVICE_PORT    device LAN WS port                (default 8080)
  SC_DEVICE_WS_PATH device LAN WS path                (default /ws)
  SC_TOKEN          shared auth token                 (required for a real send)
  SC_MAC_IP         Mac LAN IP put in the audio URL   (default: auto, subnet-matched)
  SC_SERVE_PORT     Mac HTTP file-server port         (default 8790)
  SC_SERVE_DIR      dir holding the .ogg files        (default /tmp/sc)
  SC_OPUS_RATE      Opus sample rate (Hz)             (default 16000)
  SC_OPUS_FRAME_MS  Opus frame duration (ms)          (default 60)
  SC_TTS_VOICE      `say` voice, e.g. Meijia/Tingting (default: system voice)
  SC_SAY_BIN        `say` binary                      (default say)
  SC_FFMPEG_BIN     ffmpeg binary                     (default ffmpeg)

── CLI ───────────────────────────────────────────────────────────────────────────
  python stackchan_push.py speak  "跑完了，HYPE 回测 Sharpe 1.8" [--face happy] [--gesture nod]
  python stackchan_push.py express happy
  python stackchan_push.py gesture nod
  python stackchan_push.py doctor            # print resolved config + check say/ffmpeg
  add --dry-run to speak/express/gesture to compose+print without touching the device.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import websockets

try:  # optional; the live bridge already depends on it
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("stackchan-push")

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_DEVICE_PORT = 8080
DEFAULT_DEVICE_WS_PATH = "/ws"
DEFAULT_SERVE_PORT = 8790
DEFAULT_SERVE_DIR = "/tmp/sc"
DEFAULT_OPUS_RATE = 16000
DEFAULT_OPUS_FRAME_MS = 60

CONNECT_TIMEOUT = 5.0  # WS open timeout (s)
REPLY_TIMEOUT = 5.0  # wait for the device's JSON-RPC reply (s)
FETCH_TIMEOUT = 15.0  # wait for the device to GET the audio URL (s)
FETCH_GRACE = 2.0  # keep serving after the GET so TCP drains (s)
UTT_MAX_AGE = 600  # prune served .ogg files older than this (s)

VALID_GESTURES = {"nod": "self.head.nod", "shake": "self.head.shake"}

_id_seq = 0


def _next_id() -> int:
    global _id_seq
    _id_seq += 1
    return _id_seq


# ── errors ──────────────────────────────────────────────────────────────────────
class PushError(RuntimeError):
    """Base for all push failures (message is human-facing)."""


class SynthError(PushError):
    """TTS / transcode failed."""


class DeviceUnreachable(PushError):
    """Could not open / talk to the device LAN WebSocket."""


class DeviceError(PushError):
    """Device replied with a JSON-RPC error (e.g. bad token, unknown tool)."""


# ── config ──────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    device_ip: str | None
    device_port: int
    device_ws_path: str
    token: str | None
    mac_ip: str | None
    serve_port: int
    serve_dir: Path
    opus_rate: int
    opus_frame_ms: int
    tts_voice: str | None
    say_bin: str
    ffmpeg_bin: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            device_ip=(os.environ.get("SC_DEVICE_IP") or "").strip() or None,
            device_port=int(os.environ.get("SC_DEVICE_PORT") or DEFAULT_DEVICE_PORT),
            device_ws_path=os.environ.get("SC_DEVICE_WS_PATH")
            or DEFAULT_DEVICE_WS_PATH,
            token=(os.environ.get("SC_TOKEN") or "").strip() or None,
            mac_ip=(os.environ.get("SC_MAC_IP") or "").strip() or None,
            serve_port=int(os.environ.get("SC_SERVE_PORT") or DEFAULT_SERVE_PORT),
            serve_dir=Path(os.environ.get("SC_SERVE_DIR") or DEFAULT_SERVE_DIR),
            opus_rate=int(os.environ.get("SC_OPUS_RATE") or DEFAULT_OPUS_RATE),
            opus_frame_ms=int(
                os.environ.get("SC_OPUS_FRAME_MS") or DEFAULT_OPUS_FRAME_MS
            ),
            tts_voice=(os.environ.get("SC_TTS_VOICE") or "").strip() or None,
            say_bin=os.environ.get("SC_SAY_BIN") or "say",
            ffmpeg_bin=os.environ.get("SC_FFMPEG_BIN") or "ffmpeg",
        )

    @property
    def ws_url(self) -> str:
        return f"ws://{self.device_ip}:{self.device_port}{self.device_ws_path}"

    def require_device(self) -> None:
        """Fail early with a clear message if we cannot do a real send."""
        missing = []
        if not self.device_ip:
            missing.append("SC_DEVICE_IP (device LAN IP)")
        if not self.token:
            missing.append("SC_TOKEN (shared auth token)")
        if missing:
            raise PushError(
                "cannot reach the device — missing config: "
                + ", ".join(missing)
                + ". Set them in bridge/.env or the environment (see .env.example)."
            )

    def resolved_mac_ip(self) -> str:
        """Mac LAN IP to advertise in the audio URL (subnet-matched to the device)."""
        if self.mac_ip:
            return self.mac_ip
        ip = detect_lan_ip(self.device_ip)
        if not ip:
            raise PushError(
                "could not auto-detect the Mac's LAN IP — set SC_MAC_IP explicitly "
                "(the device must be able to reach the Mac's HTTP server on the LAN)."
            )
        return ip


# ── LAN IP detection (subnet-aware; VPN/utun-safe) ────────────────────────────────
def _local_ipv4s() -> list[tuple[str, str]]:
    """Return [(iface, ipv4), ...] from `ifconfig`, skipping loopback / VPN interfaces."""
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return []
    result: list[tuple[str, str]] = []
    iface = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":", 1)[0]
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if not (m and iface):
            continue
        ip = m.group(1)
        if iface == "lo0" or ip.startswith("127."):
            continue
        # skip tunnels / VPN / link-local-only virtual ifaces
        if iface.startswith(("utun", "ppp", "ipsec", "gif", "stf", "awdl", "llw")):
            continue
        result.append((iface, ip))
    return result


def detect_lan_ip(device_ip: str | None) -> str | None:
    """Best Mac LAN IP for the device to fetch from.

    Prefer a local address on the same /24 as the device (the reliable signal —
    the UDP-connect trick misfires to a VPN address when a VPN is up). Fall back to
    the first private (RFC1918) address, then the UDP trick.
    """
    candidates = _local_ipv4s()

    if device_ip:
        prefix = device_ip.rsplit(".", 1)[0] + "."
        for _iface, ip in candidates:
            if ip.startswith(prefix):
                return ip

    for _iface, ip in candidates:
        if ip.startswith(("192.168.", "10.")) or re.match(
            r"172\.(1[6-9]|2\d|3[01])\.", ip
        ):
            return ip

    # last resort: outbound-interface trick (may return a VPN address)
    try:
        s = __import__("socket").socket(2, 2)  # AF_INET, SOCK_DGRAM
        try:
            s.connect((device_ip or "8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


# ── TTS + transcode ──────────────────────────────────────────────────────────────
def _prune_old_utterances(serve_dir: Path) -> None:
    now = time.time()
    for f in serve_dir.glob("utt-*.ogg"):
        try:
            if now - f.stat().st_mtime > UTT_MAX_AGE:
                f.unlink()
        except OSError:
            pass


def synthesize(cfg: Config, text: str) -> Path:
    """`say` the text to AIFF, transcode to OGG/Opus, return the unique .ogg path.

    Unique filename per utterance avoids the device replaying a stale-cached file.
    """
    text = (text or "").strip()
    if not text:
        raise SynthError("nothing to speak (empty text)")

    cfg.serve_dir.mkdir(parents=True, exist_ok=True)
    _prune_old_utterances(cfg.serve_dir)

    stamp = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    aiff = cfg.serve_dir / f"utt-{stamp}.aiff"
    ogg = cfg.serve_dir / f"utt-{stamp}.ogg"

    say_cmd = [cfg.say_bin]
    if cfg.tts_voice:
        say_cmd += ["-v", cfg.tts_voice]
    say_cmd += ["-o", str(aiff), text]
    _run(say_cmd, SynthError, "macOS `say` (TTS)")
    if not aiff.exists() or aiff.stat().st_size == 0:
        raise SynthError("`say` produced no audio")

    ffmpeg_cmd = [
        cfg.ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(aiff),
        "-c:a",
        "libopus",
        "-ar",
        str(cfg.opus_rate),
        "-ac",
        "1",
        "-frame_duration",
        str(cfg.opus_frame_ms),
        "-f",
        "ogg",
        str(ogg),
    ]
    try:
        _run(ffmpeg_cmd, SynthError, "ffmpeg libopus transcode")
    finally:
        aiff.unlink(missing_ok=True)  # keep only the .ogg we serve

    if not ogg.exists() or ogg.stat().st_size == 0:
        raise SynthError("ffmpeg produced an empty .ogg")
    with ogg.open("rb") as fh:
        if fh.read(4) != b"OggS":
            raise SynthError("ffmpeg output is not a valid OGG stream")
    logger.info("synthesized %s (%d bytes)", ogg.name, ogg.stat().st_size)
    return ogg


def _run(cmd: list[str], err_cls: type[PushError], what: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise err_cls(f"{what}: command not found ({cmd[0]})") from e
    except subprocess.TimeoutExpired as e:
        raise err_cls(f"{what}: timed out") from e
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-300:]
        raise err_cls(f"{what}: exit {proc.returncode}: {tail}")


# ── MCP envelope + LAN WebSocket send ─────────────────────────────────────────────
def build_envelope(tool: str, arguments: dict, token: str | None, msg_id: int) -> dict:
    """Compose the exact wire message the firmware expects.

    `token` is injected into `arguments` (see module NOTE on token placement).
    """
    args = dict(arguments)
    args["token"] = token
    return {
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
    }


def _unwrap_reply(msg: dict) -> dict:
    """Accept either a bare JSON-RPC reply or one wrapped in {"type":..,"payload":..}."""
    if isinstance(msg.get("payload"), dict):
        return msg["payload"]
    return msg


async def send_mcp(cfg: Config, tool: str, arguments: dict) -> dict | None:
    """Send one MCP tools/call over the device LAN WS; return its reply (or None).

    Raises DeviceUnreachable on connection failure, DeviceError on a JSON-RPC error
    reply (e.g. bad token / unknown tool). Returns the reply dict if the device
    answers within REPLY_TIMEOUT, else None (sent, unconfirmed).
    """
    cfg.require_device()
    msg_id = _next_id()
    envelope = build_envelope(tool, arguments, cfg.token, msg_id)
    logger.info("→ %s  %s", tool, cfg.ws_url)
    try:
        async with websockets.connect(cfg.ws_url, open_timeout=CONNECT_TIMEOUT) as ws:
            await ws.send(json.dumps(envelope))
            deadline = time.monotonic() + REPLY_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "no reply from device within %.0fs (sent anyway)", REPLY_TIMEOUT
                    )
                    return None
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.warning(
                        "no reply from device within %.0fs (sent anyway)", REPLY_TIMEOUT
                    )
                    return None
                try:
                    reply = _unwrap_reply(json.loads(raw))
                except (ValueError, TypeError):
                    continue  # ignore non-JSON / unexpected frames
                if reply.get("id") != msg_id:
                    continue  # a broadcast for someone else; keep waiting
                if "error" in reply:
                    raise DeviceError(f"device rejected {tool}: {reply['error']}")
                return reply
    except (
        OSError,
        asyncio.TimeoutError,
        websockets.exceptions.WebSocketException,
    ) as e:
        raise DeviceUnreachable(f"cannot reach device at {cfg.ws_url}: {e}") from e


# ── ephemeral LAN file server (self-contained per speak) ──────────────────────────
class _AudioServer:
    """Serve exactly the target .ogg from serve_dir on the Mac's LAN IP, briefly.

    Bound to the Mac LAN IP (not 0.0.0.0) per spec; only GETs of *.ogg in serve_dir
    succeed; sets `fetched` once the device pulls the target file.
    """

    def __init__(self, serve_dir: Path, mac_ip: str, port: int, target: str):
        self.fetched = Event()
        directory = str(serve_dir)
        target_name = target
        fetched_event = self.fetched

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=directory, **kw)

            def do_GET(self):  # noqa: N802
                if not self.path.split("?", 1)[0].endswith(".ogg"):
                    self.send_error(403, "only .ogg is served")
                    return
                super().do_GET()
                if self.path.lstrip("/").split("?", 1)[0] == target_name:
                    fetched_event.set()

            def log_message(self, format, *args):  # noqa: A002  # quiet; route to our logger
                logger.debug("http: " + format, *args)

        self._httpd = ThreadingHTTPServer((mac_ip, port), Handler)
        self._httpd.allow_reuse_address = True
        self._thread = Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_AudioServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# ── high-level actions ────────────────────────────────────────────────────────────
async def speak(
    cfg: Config, text: str, face: str | None = None, gesture_kind: str | None = None
) -> None:
    """Make the robot say `text` out loud, now, Mac-initiated. Optionally emote/gesture."""
    cfg.require_device()
    mac_ip = cfg.resolved_mac_ip()
    ogg = synthesize(cfg, text)
    url = f"http://{mac_ip}:{cfg.serve_port}/{ogg.name}"

    try:
        server_ctx = _AudioServer(cfg.serve_dir, mac_ip, cfg.serve_port, ogg.name)
    except OSError as e:
        raise PushError(
            f"could not bind HTTP file server on {mac_ip}:{cfg.serve_port} ({e}). "
            "Another speak may be in flight, or set SC_SERVE_PORT / SC_MAC_IP."
        ) from e

    with server_ctx as server:
        logger.info("serving %s", url)
        await send_mcp(cfg, "self.play_audio_url", {"url": url})
        got = await asyncio.to_thread(server.fetched.wait, FETCH_TIMEOUT)
        if got:
            logger.info("device fetched the audio ✓")
            await asyncio.to_thread(time.sleep, FETCH_GRACE)  # let the body drain
        else:
            logger.warning(
                "device did not fetch %s within %.0fs — is the firmware flashed with "
                "self.play_audio_url, and can it reach %s?",
                url,
                FETCH_TIMEOUT,
                mac_ip,
            )

    # optional expression / gesture alongside the speech (best-effort)
    if face:
        await express(cfg, face)
    if gesture_kind:
        await gesture(cfg, gesture_kind)


async def express(cfg: Config, face: str) -> None:
    """Set the robot's face, e.g. neutral / happy / sad / angry / surprised."""
    face = (face or "").strip()
    if not face:
        raise PushError("express: empty face")
    await send_mcp(cfg, "self.face.expression", {"emotion": face})


async def gesture(cfg: Config, kind: str) -> None:
    """Nod or shake the head."""
    kind = (kind or "").strip().lower()
    tool = VALID_GESTURES.get(kind)
    if not tool:
        raise PushError(
            f"gesture: unknown kind {kind!r} (want one of {sorted(VALID_GESTURES)})"
        )
    await send_mcp(cfg, tool, {})


# ── dry-run + doctor (no device required) ─────────────────────────────────────────
def _dry_run(cfg: Config, tool: str, arguments: dict) -> None:
    token = cfg.token or "<SC_TOKEN unset>"
    env = build_envelope(tool, arguments, token, _next_id())
    print(
        f"[dry-run] would send to {cfg.ws_url if cfg.device_ip else 'ws://<SC_DEVICE_IP>:%d%s' % (cfg.device_port, cfg.device_ws_path)}:"
    )
    print(json.dumps(env, ensure_ascii=False, indent=2))


def _doctor(cfg: Config) -> int:
    print("stackchan_push doctor")
    print("─" * 40)
    print(f"  device WS   : {cfg.ws_url if cfg.device_ip else '(SC_DEVICE_IP unset)'}")
    print(f"  token       : {'set' if cfg.token else '(SC_TOKEN unset)'}")
    try:
        mac_ip = cfg.resolved_mac_ip()
    except PushError as e:
        mac_ip = f"(unresolved: {e})"
    print(f"  mac IP      : {mac_ip}{'' if cfg.mac_ip else '  [auto]'}")
    print(f"  serve       : http://{mac_ip}:{cfg.serve_port}/  (dir {cfg.serve_dir})")
    print(
        f"  opus        : {cfg.opus_rate} Hz mono, {cfg.opus_frame_ms} ms frames, OGG"
    )
    print(f"  tts voice   : {cfg.tts_voice or '(system default)'}")
    ok = True
    # `say` needs a real .aiff target (rejects /dev/null), so probe into a temp file
    probe = Path(os.environ.get("TMPDIR") or "/tmp") / f"sc-doctor-{os.getpid()}.aiff"
    try:
        subprocess.run(
            [cfg.say_bin, "-o", str(probe), "test"],
            capture_output=True,
            timeout=10,
            check=True,
        )
        print(f"  say        : ok ({cfg.say_bin})")
    except Exception as e:
        ok = False
        print(f"  say        : FAIL ({e})")
    finally:
        probe.unlink(missing_ok=True)
    try:
        codecs = subprocess.run(
            [cfg.ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        if "libopus" in codecs:
            print("  ffmpeg     : ok (libopus present)")
        else:
            ok = False
            print("  ffmpeg     : FAIL (libopus encoder missing)")
    except Exception as e:
        ok = False
        print(f"  ffmpeg     : FAIL ({e})")
    print("─" * 40)
    print(
        "  → ready"
        if ok and cfg.device_ip and cfg.token
        else "  → set missing config / tools before a real send"
    )
    return 0 if ok else 1


# ── CLI ────────────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stackchan_push.py",
        description="Mac → Stack-chan LAN push: speak / express / gesture.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("speak", help="say text out loud on the robot")
    sp.add_argument("text", help="text to speak")
    sp.add_argument("--face", help="also set this expression")
    sp.add_argument("--gesture", choices=sorted(VALID_GESTURES), help="also nod/shake")
    sp.add_argument("--dry-run", action="store_true", help="compose+print, don't send")

    ep = sub.add_parser("express", help="set the robot's face")
    ep.add_argument("face", help="emotion, e.g. happy/sad/neutral")
    ep.add_argument("--dry-run", action="store_true")

    gp = sub.add_parser("gesture", help="nod / shake the head")
    gp.add_argument("kind", choices=sorted(VALID_GESTURES))
    gp.add_argument("--dry-run", action="store_true")

    sub.add_parser("doctor", help="print resolved config + check say/ffmpeg")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = Config.from_env()

    try:
        if args.cmd == "doctor":
            return _doctor(cfg)

        if args.cmd == "speak" and args.dry_run:
            mac_ip = cfg.mac_ip or "<SC_MAC_IP>"
            ogg = synthesize(cfg, args.text)
            _dry_run(
                cfg,
                "self.play_audio_url",
                {"url": f"http://{mac_ip}:{cfg.serve_port}/{ogg.name}"},
            )
            if args.face:
                _dry_run(cfg, "self.face.expression", {"emotion": args.face})
            if args.gesture:
                _dry_run(cfg, VALID_GESTURES[args.gesture], {})
            return 0
        if args.cmd == "express" and args.dry_run:
            _dry_run(cfg, "self.face.expression", {"emotion": args.face})
            return 0
        if args.cmd == "gesture" and args.dry_run:
            _dry_run(cfg, VALID_GESTURES[args.kind], {})
            return 0

        if args.cmd == "speak":
            asyncio.run(
                speak(cfg, args.text, face=args.face, gesture_kind=args.gesture)
            )
        elif args.cmd == "express":
            asyncio.run(express(cfg, args.face))
        elif args.cmd == "gesture":
            asyncio.run(gesture(cfg, args.kind))
        return 0
    except PushError as e:
        logger.error("%s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
