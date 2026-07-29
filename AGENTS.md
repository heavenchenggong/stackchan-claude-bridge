# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Two data-flow directions (don't conflate them)

- **Cloud → Mac (PULL, existing):** `bridge/mcp_pipe.py` dials out to `wss://api.xiaozhi.me/mcp/` and pipes to `bridge/server.py` (FastMCP: `claude_code*` tools). The **xiaozhi cloud LLM** calls the Mac's tools. Results go to `/tmp/claude-bridge-outbox/` and are read back when the human asks. See `docs/architecture.md`.
- **Mac → device (PUSH, `bridge/stackchan_push.py`):** a Mac-side agent, on its own initiative, drives the robot over the **LAN** (`ws://<device-ip>:8080/ws`) — no cloud. `speak` / `express` / `gesture`. See `docs/mac-push.md`.

## `bridge/stackchan_push.py` — speak / express / gesture (Mac → device)

Standalone module + CLI (not wired into the cloud-facing `server.py`, on purpose — it's the opposite direction).

- `speak(text)`: macOS `say` → `ffmpeg -c:a libopus -ar <rate> -ac 1 -frame_duration <ms> -f ogg` → serve a **unique** `.ogg` on a tiny HTTP server bound to the Mac LAN IP → send ONE MCP `self.play_audio_url {url, token}` over the LAN WS → wait for the device to GET it. The device has **no on-device TTS**; the Mac produces the audio.
- `express(face)` → `self.face.expression {emotion}`; `gesture(nod|shake)` → `self.head.nod` / `self.head.shake` (these device tools already exist, see `docs/firmware-changes.md`).
- CLI: `speak "…" [--face f] [--gesture nod] [--dry-run]`, `express f`, `gesture nod`, `doctor`.

### Interface contract (MUST match the firmware, built in parallel — Path A2)

```
ws://<device-ip>:8080/ws
{"type":"mcp","payload":{"jsonrpc":"2.0","id":<n>,"method":"tools/call","params":{"name":"<tool>","arguments":{...}}}}
```

Two assumptions to reconcile when the firmware PR lands (each is a one-line change):

1. **Token placement:** `token` is injected into `arguments` of **every** tool call (the only explicit contract example is `play_audio_url`'s `arguments`). If the firmware checks it at the envelope level, change `build_envelope()`.
2. **Audio format:** defaults to **16 kHz mono / 60 ms Opus frames / OGG** (reference-impl values). Configurable via `SC_OPUS_RATE` / `SC_OPUS_FRAME_MS` to match the firmware decoder. (`ffprobe` showing 48000 Hz in the OGG header is normal Ogg-Opus behaviour, not a bug.)

### Config env vars (`bridge/.env`, never hardcode secrets — see `.env.example`)

`SC_DEVICE_IP` (req), `SC_TOKEN` (req), `SC_DEVICE_PORT`=8080, `SC_DEVICE_WS_PATH`=/ws, `SC_MAC_IP` (auto, **subnet-matched to the device to avoid picking a VPN/utun IP**), `SC_SERVE_PORT`=8790, `SC_SERVE_DIR`=/tmp/sc, `SC_OPUS_RATE`=16000, `SC_OPUS_FRAME_MS`=60, `SC_TTS_VOICE`.

## Build / test / lint

- Deps live in a gitignored `bridge/.venv`. Runtime deps: `websockets>=14`, `python-dotenv` (both in `requirements.txt`). `speak` also needs `ffmpeg` **with libopus** + macOS `say`.
- `bridge/.venv/bin/ruff check bridge/` — the repo lints with ruff (E,F defaults; no repo config). (Pre-existing `server.py:155` F541 is not from the push feature.)
- `bridge/.venv/bin/python bridge/tests/test_stackchan_push.py` — 9-check E2E against a **mock device** (WS server that validates the token and actually HTTP-GETs the audio URL like the firmware would). No live hardware needed.
- `bridge/.venv/bin/python bridge/stackchan_push.py doctor` — self-check of config + `say`/`ffmpeg`.
