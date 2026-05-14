#!/usr/bin/env python3
"""AgentVolt HTTP bridge — v7 (persistent WebSocket).

Listens on $BRIDGE_PORT (default 18790) and proxies a small HTTP surface
to the local OpenClaw gateway over a single, long-lived WebSocket. Each
HTTP request maps to one (or a few) RPC calls on that socket — no more
forking the OpenClaw CLI per request, which used to add 3.5s warm /
~30s cold per call and made the dashboard "Disconnected" badge stick.

HTTP surface (unchanged from v6 so the AC frontend doesn't need updates):
    GET  /health         healthcheck — also reports WS connection state
    GET  /capabilities   bridge feature flags
    GET  /chat/history   list chat history (DB-friendly shape)
    POST /chat/send      send message, poll for response (or fire-and-forget)
    POST /chat/stream    SSE: status / heartbeat / token / done / error
    GET  /debug/stats    /proc/meminfo + uptime + WS state
"""
import asyncio
import http.server
import json
import os
import socketserver
import sys
import threading
import time
import uuid
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed

GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "18789"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "18790"))
GATEWAY_URL = os.environ.get("GATEWAY_URL", f"ws://127.0.0.1:{GATEWAY_PORT}/")
BRIDGE_VERSION = 7
SESSION_KEY = "agent:main:main"
PROTOCOL_VERSION = 4
# Cap on request body size for /chat/send and /chat/stream. Defends against
# a malicious Content-Length header allocating arbitrary memory. A chat
# message of 256KB is plenty (~50 pages of plain text).
MAX_BODY_BYTES = 256 * 1024
# Operator scopes — must match what the CLI used to claim. The gateway
# treats a local backend client with shared-token auth as trusted (see
# shouldSkipLocalBackendSelfPairing) so device pairing is bypassed.
OPERATOR_SCOPES = [
    "operator.admin",
    "operator.read",
    "operator.write",
    "operator.approvals",
    "operator.pairing",
    "operator.talk.secrets",
]

ERROR_REWRITES = (
    (
        "[assistant turn failed before producing content]",
        "I had trouble processing that message. This was likely a temporary issue - please try again.",
    ),
    (
        "exceeded your current quota",
        "The AI service is temporarily unavailable. Please try again in a few minutes.",
    ),
    (
        "FailoverError:",
        "I had trouble processing that message. This was likely a temporary issue - please try again.",
    ),
)


def _rewrite_error(text: str) -> str:
    for needle, replacement in ERROR_REWRITES:
        if needle in text:
            return replacement
    return text


def _filter_messages(messages):
    out = []
    for m in messages:
        if not m.get("content"):
            continue
        text = "".join(
            c.get("text", "") for c in m["content"] if c.get("type") == "text"
        )
        if not text:
            continue
        if text.startswith("⚠"):
            continue
        low = text.lower()
        if "heartbeat" in low or "HEARTBEAT" in text:
            continue
        out.append({
            "role": m["role"],
            "content": _rewrite_error(text),
            "timestamp": m.get("timestamp", 0),
        })
    return out


class GatewayClient:
    """Persistent WebSocket connection to the local OpenClaw gateway.

    Owns its own asyncio loop in a background thread. Sync callers from
    the HTTP handler threads use `request()` which submits a coroutine
    to that loop and blocks for the result.

    Reconnects on close with exponential backoff. While disconnected,
    pending requests fail fast with GatewayUnavailable so HTTP handlers
    can return 503 instead of hanging.
    """

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()  # bound to loop after start
        self._stopping = False
        self._connect_attempts = 0
        self._last_error: Optional[str] = None
        self._connected_since_ms: Optional[int] = None

    # ---- lifecycle -----------------------------------------------------
    def start(self):
        ready = threading.Event()

        def _run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._connected = asyncio.Event()
            ready.set()
            self.loop.create_task(self._connect_forever())
            self.loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True, name="gw-ws")
        self._thread.start()
        ready.wait(timeout=5)

    def status(self) -> dict:
        return {
            "url": self.url,
            "connected": bool(self._ws is not None),
            "connectedSinceMs": self._connected_since_ms,
            "connectAttempts": self._connect_attempts,
            "lastError": self._last_error,
            "pending": len(self._pending),
        }

    # ---- connection management ----------------------------------------
    async def _connect_forever(self):
        backoff = 1.0
        while not self._stopping:
            self._connect_attempts += 1
            try:
                # max_size matches the gateway's MAX_PAYLOAD; keeping it
                # generous because chat history responses can be large.
                async with websockets.connect(
                    self.url,
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    await self._handshake(ws)
                    self._ws = ws
                    self._connected.set()
                    self._connected_since_ms = int(time.time() * 1000)
                    self._last_error = None
                    backoff = 1.0
                    print(
                        f"[bridge] gateway connected ({self.url})",
                        flush=True,
                    )
                    try:
                        await self._read_loop(ws)
                    finally:
                        self._ws = None
                        self._connected.clear()
                        self._connected_since_ms = None
                        # Any callers blocked waiting for a response need
                        # to be woken so they can return an error.
                        self._fail_all_pending(GatewayUnavailable("connection closed"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                print(f"[bridge] gateway connect failed: {self._last_error}", file=sys.stderr, flush=True)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 15.0)

    async def _handshake(self, ws):
        connect_id = uuid.uuid4().hex
        params = {
            "minProtocol": PROTOCOL_VERSION,
            "maxProtocol": PROTOCOL_VERSION,
            "client": {
                "id": "gateway-client",
                "displayName": "av-bridge",
                "version": f"{BRIDGE_VERSION}.0.0",
                "platform": "linux",
                "mode": "backend",
                "instanceId": uuid.uuid4().hex,
            },
            "auth": {"token": self.token},
            "role": "operator",
            "scopes": OPERATOR_SCOPES,
        }
        await ws.send(json.dumps({
            "type": "req",
            "id": connect_id,
            "method": "connect",
            "params": params,
        }))
        # Server replies with {type:"res", id, ok:true, payload:{type:"hello-ok", ...}}
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        frame = json.loads(raw)
        if frame.get("type") != "res" or frame.get("id") != connect_id:
            raise RuntimeError(f"unexpected handshake frame: {frame}")
        if not frame.get("ok"):
            raise RuntimeError(f"handshake rejected: {frame.get('error')}")
        payload = frame.get("payload", {})
        if payload.get("type") != "hello-ok":
            raise RuntimeError(f"handshake missing hello-ok: {payload}")

    async def _read_loop(self, ws):
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            ftype = frame.get("type")
            if ftype == "res":
                fid = frame.get("id")
                fut = self._pending.pop(fid, None)
                if fut is not None and not fut.done():
                    if frame.get("ok"):
                        fut.set_result(frame.get("payload"))
                    else:
                        err = frame.get("error") or {}
                        msg = err.get("message") or json.dumps(err)
                        fut.set_exception(GatewayRpcError(msg, err))
            # event / tick / shutdown frames are ignored — v7 still
            # uses request/response only. Could subscribe to chat
            # events later for true streaming.

    def _fail_all_pending(self, err: Exception):
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    # ---- request API (called from HTTP handler threads) ----------------
    def request(self, method: str, params: dict, timeout: float = 30.0) -> Any:
        """Send an RPC and block (in the calling thread) until the response
        comes back, the gateway disconnects, or the timeout fires."""
        if self.loop is None or not self.loop.is_running():
            raise GatewayUnavailable("gateway client not started")
        fut = asyncio.run_coroutine_threadsafe(
            self._do_request(method, params, timeout),
            self.loop,
        )
        try:
            return fut.result(timeout=timeout + 2.0)
        except asyncio.TimeoutError as e:
            raise GatewayTimeout(f"timeout calling {method}") from e

    async def _do_request(self, method: str, params: dict, timeout: float) -> Any:
        if self._ws is None:
            # Wait briefly for a reconnect to land. Don't wait forever —
            # the HTTP caller has its own timeout.
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=min(timeout, 5.0))
            except asyncio.TimeoutError:
                raise GatewayUnavailable("gateway disconnected")
        if self._ws is None:
            raise GatewayUnavailable("gateway disconnected")
        rid = uuid.uuid4().hex
        fut: asyncio.Future = self.loop.create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send(json.dumps({
                "type": "req",
                "id": rid,
                "method": method,
                "params": params,
            }))
            return await asyncio.wait_for(fut, timeout=timeout)
        except (ConnectionClosed, OSError) as e:
            self._pending.pop(rid, None)
            raise GatewayUnavailable(f"send failed: {e}") from e
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise GatewayTimeout(f"timeout calling {method}")
        finally:
            self._pending.pop(rid, None)


class GatewayUnavailable(Exception):
    """Raised when no WebSocket is connected. HTTP layer should map to 503."""


class GatewayTimeout(Exception):
    """Raised when an RPC didn't get a response in time. HTTP layer maps to 504."""


class GatewayRpcError(Exception):
    """Raised when the gateway returned ok:false. Carries the error envelope."""

    def __init__(self, message: str, envelope: dict | None = None):
        super().__init__(message)
        self.envelope = envelope or {}


# Module-level singleton; populated in main() before the HTTP server starts.
GATEWAY: Optional[GatewayClient] = None


class Handler(http.server.BaseHTTPRequestHandler):
    # ---- transport helpers ---------------------------------------------
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _json(self, data, status=200):
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _auth(self):
        a = self.headers.get("Authorization", "")
        if not a.startswith("Bearer ") or a[7:] != GATEWAY_TOKEN:
            self._json({"error": "Unauthorized"}, 401)
            return False
        return True

    def _read_body(self):
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            self._json({"error": "Invalid Content-Length"}, 400)
            return None, 400
        if length < 0 or length > MAX_BODY_BYTES:
            self._json({"error": f"Request body too large (max {MAX_BODY_BYTES} bytes)"}, 413)
            return None, 413
        try:
            raw = self.rfile.read(length) if length > 0 else b""
            return json.loads(raw or b"{}"), None
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "Malformed JSON body"}, 400)
            return None, 400

    def log_message(self, *a):
        pass

    def _gw_error_status(self, e: Exception) -> int:
        if isinstance(e, GatewayUnavailable):
            return 503
        if isinstance(e, GatewayTimeout):
            return 504
        return 500

    # ---- routing -------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            return self._health()
        if self.path == "/capabilities":
            return self._json({
                "version": BRIDGE_VERSION,
                "streaming": True,
                "transport": "websocket",
            })
        if self.path.startswith("/chat/history"):
            return self._history()
        if self.path == "/debug/stats":
            return self._debug_stats()
        self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/chat/send":
            return self._send()
        if self.path == "/chat/stream":
            return self._stream()
        self._json({"error": "Not found"}, 404)

    # ---- chat ----------------------------------------------------------
    def _health(self):
        # /health stays unauthenticated so Fly + AC dashboard can probe
        # without leaking the gateway token. The gateway connection state
        # is included so the AC dashboard can show "Connected" only when
        # the bridge actually has a live socket to the gateway.
        st = GATEWAY.status() if GATEWAY else {"connected": False}
        ok = bool(st.get("connected"))
        return self._json({
            "ok": ok,
            "version": BRIDGE_VERSION,
            "gateway": st,
        }, status=200 if ok else 503)

    def _hist_raw(self, timeout: float = 10.0):
        try:
            payload = GATEWAY.request(
                "chat.history",
                {"sessionKey": SESSION_KEY},
                timeout=timeout,
            )
        except GatewayTimeout as e:
            return None, str(e), True
        except GatewayUnavailable as e:
            return None, str(e), False
        except GatewayRpcError as e:
            return None, str(e), False
        except Exception as e:
            return None, f"{type(e).__name__}: {e}", False
        if not isinstance(payload, dict):
            return [], None, False
        return _filter_messages(payload.get("messages", [])), None, False

    def _history(self):
        if not self._auth():
            return
        msgs, err, timed_out = self._hist_raw()
        if err is not None:
            return self._json({"error": err}, 504 if timed_out else 503)
        self._json({"messages": msgs})

    def _send(self):
        if not self._auth():
            return
        try:
            body, body_err = self._read_body()
            if body_err is not None:
                return
            msg = body.get("message", "")
            fire_and_forget = bool(body.get("fireAndForget"))
            if not isinstance(msg, str) or not msg.strip():
                return self._json({"error": "Message required"}, 400)

            idem = f"av-{uuid.uuid4().hex[:12]}"
            params = {
                "sessionKey": SESSION_KEY,
                "message": msg,
                "idempotencyKey": idem,
            }

            if fire_and_forget:
                def _bg():
                    try:
                        GATEWAY.request("chat.send", params, timeout=120)
                    except Exception as e:
                        print(f"[bridge] fire-and-forget send failed: {e}", file=sys.stderr)
                threading.Thread(target=_bg, daemon=True).start()
                return self._json({"sent": True})

            # Track the timestamp of the last assistant message we saw before
            # send — newer is the only reliable "new reply arrived" signal.
            prev, _, _ = self._hist_raw(timeout=8.0)
            prev_last_asst_ts = max(
                (m.get("timestamp", 0) for m in (prev or []) if m.get("role") == "assistant"),
                default=0,
            )

            try:
                GATEWAY.request("chat.send", params, timeout=45.0)
            except GatewayTimeout:
                pass  # send may finish even after timeout; we poll

            # Poll history for the new assistant reply. Each iteration is
            # ~(sleep + history-timeout) ≈ 0.6 + 4 = 4.6s; 10 iterations
            # ≈ 46s, leaving headroom under Vercel's 60s function timeout.
            # Polls are now WS calls (~100-300ms each) instead of CLI
            # forks (~3.5s warm), so we can tighten the loop a lot.
            for _ in range(20):
                time.sleep(0.6)
                h, _, _ = self._hist_raw(timeout=4.0)
                if not h:
                    continue
                for m in reversed(h):
                    if m["role"] == "assistant" and m.get("timestamp", 0) > prev_last_asst_ts:
                        return self._json(m)
            self._json({"error": "Response timeout"}, 504)
        except Exception as e:
            self._json({"error": str(e)}, self._gw_error_status(e))

    def _stream(self):
        if not self._auth():
            return
        try:
            body, body_err = self._read_body()
            if body_err is not None:
                return
            msg = body.get("message", "")
            if not isinstance(msg, str) or not msg.strip():
                return self._json({"error": "Message required"}, 400)

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def _sse(event, data):
                try:
                    self.wfile.write(
                        f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
                    )
                    self.wfile.flush()
                except Exception:
                    raise ConnectionError("Client disconnected")

            _sse("status", {"phase": "sending", "message": "Sending to agent..."})
            prev, _, _ = self._hist_raw(timeout=8.0)
            prev_last_asst_ts = max(
                (m.get("timestamp", 0) for m in (prev or []) if m.get("role") == "assistant"),
                default=0,
            )

            idem = f"av-{uuid.uuid4().hex[:12]}"
            send_params = {
                "sessionKey": SESSION_KEY,
                "message": msg,
                "idempotencyKey": idem,
            }
            send_done = threading.Event()

            def _do_send():
                try:
                    GATEWAY.request("chat.send", send_params, timeout=120)
                except Exception:
                    pass
                finally:
                    send_done.set()

            threading.Thread(target=_do_send, daemon=True).start()
            _sse("status", {"phase": "thinking", "message": "Agent is thinking..."})

            # Poll for the new assistant reply. WS calls are fast enough
            # to poll every ~600ms; we send periodic heartbeats so the
            # client knows we're still alive.
            max_polls = 80  # ~50s wall time at 0.6s sleep + ~4s timeout
            found = False
            for i in range(max_polls):
                time.sleep(0.6)
                if i > 0 and i % 8 == 0:
                    _sse("heartbeat", {"elapsed": int(i * 0.6)})

                h, _, _ = self._hist_raw(timeout=4.0)
                if not h:
                    continue
                latest_asst = None
                for m in reversed(h):
                    if m["role"] == "assistant" and m.get("timestamp", 0) > prev_last_asst_ts:
                        latest_asst = m
                        break
                if latest_asst is not None:
                    content = latest_asst["content"]
                    chunk = 20
                    for j in range(0, len(content), chunk):
                        _sse("token", {"content": content[j:j + chunk], "done": False})
                        time.sleep(0.015)
                    _sse("token", {"content": "", "done": True})
                    _sse("done", {
                        "role": "assistant",
                        "content": content,
                        "timestamp": latest_asst.get("timestamp", int(time.time() * 1000)),
                    })
                    found = True
                    break

            if not found:
                _sse("error", {
                    "message": "Response timeout - agent may still be processing. Refresh to check.",
                })
        except ConnectionError:
            pass
        except Exception as e:
            try:
                self.wfile.write(
                    f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n".encode()
                )
                self.wfile.flush()
            except Exception:
                pass

    # ---- debug ---------------------------------------------------------
    def _debug_stats(self):
        if not self._auth():
            return
        out = {"gateway": GATEWAY.status() if GATEWAY else None}
        try:
            with open("/proc/meminfo", "r") as f:
                mem = {}
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    mem[k.strip()] = v.strip()
                wanted = [
                    "MemTotal", "MemFree", "MemAvailable",
                    "Buffers", "Cached",
                    "SwapTotal", "SwapFree", "SwapCached",
                    "Dirty", "Writeback",
                ]
                out["host_meminfo"] = {k: mem.get(k) for k in wanted}
        except Exception as e:
            out["host_meminfo_error"] = str(e)
        try:
            with open("/proc/loadavg", "r") as f:
                out["host_loadavg"] = f.read().strip()
        except Exception as e:
            out["host_loadavg_error"] = str(e)
        try:
            with open("/proc/1/status", "r") as f:
                status = {}
                for line in f:
                    if ":" not in line:
                        continue
                    k, v = line.split(":", 1)
                    if k.startswith("Vm"):
                        status[k] = v.strip()
                out["pid1_vm"] = status
        except Exception as e:
            out["pid1_vm_error"] = str(e)
        self._json(out)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global GATEWAY
    if not GATEWAY_TOKEN:
        print("[bridge] GATEWAY_TOKEN env var is required", file=sys.stderr)
        sys.exit(1)
    GATEWAY = GatewayClient(GATEWAY_URL, GATEWAY_TOKEN)
    GATEWAY.start()
    server = ThreadingServer(("0.0.0.0", BRIDGE_PORT), Handler)
    print(
        f"[bridge v{BRIDGE_VERSION}] listening on 0.0.0.0:{BRIDGE_PORT} → gateway {GATEWAY_URL}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
