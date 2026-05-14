#!/usr/bin/env python3
"""AgentVolt HTTP bridge.

Listens on $BRIDGE_PORT (default 18790) and proxies a small HTTP surface
to the local OpenClaw CLI:

    GET  /health         healthcheck
    GET  /capabilities   bridge feature flags
    GET  /chat/history   list chat history (DB-friendly shape)
    POST /chat/send      send message, poll for response (or fire-and-forget)
    POST /chat/stream    SSE: status / heartbeat / token / done / error
    GET  /debug/stats    /proc/meminfo + uptime

Wire-compatible with the Hetzner bridge (lib/openclaw.ts buildBridgeScript)
so Next.js routes work unchanged. The only differences:
  - reads GATEWAY_TOKEN + BRIDGE_PORT from env (no per-instance image build)
  - calls `openclaw` directly (no `docker exec` prefix; we ARE the container)
  - dropped docker-specific debug endpoints (/debug/logs, /debug/ps); the
    docker exec memory probe in /debug/stats is replaced with /proc reads
"""
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import uuid

GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")
PORT = int(os.environ.get("BRIDGE_PORT", "18790"))
BRIDGE_VERSION = 4
SESSION_KEY = "agent:main:main"

# Hetzner bridge transforms — keep behavior identical so the UI doesn't
# regress between providers.
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


def _call_gateway(method: str, params: dict, timeout: int = 45):
    """Run `openclaw gateway call <method> --json --params <json>` and return
    the parsed result (or raise CalledProcessError)."""
    args = [
        "openclaw", "gateway", "call", method,
        "--token", GATEWAY_TOKEN,
        "--json",
        "--params", json.dumps(params),
    ]
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _filter_messages(messages):
    """Drop heartbeats / empty messages, rewrite known error strings. Returns
    [{role, content, timestamp}, ...]."""
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

    def log_message(self, *a):
        # Default impl logs every request to stderr; Fly captures stdout/stderr
        # so we mute the noisy access log. Errors still surface via stderr in
        # the route handlers themselves.
        pass

    # ---- routing -------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            return self._json({"ok": True, "version": BRIDGE_VERSION})
        if self.path == "/capabilities":
            return self._json({"version": BRIDGE_VERSION, "streaming": True})
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
    def _hist_raw(self):
        try:
            r = _call_gateway("chat.history", {"sessionKey": SESSION_KEY})
            if r.returncode != 0:
                return None, r.stderr.strip()
            data = json.loads(r.stdout)
            return _filter_messages(data.get("messages", [])), None
        except Exception as e:
            return None, str(e)

    def _history(self):
        if not self._auth():
            return
        msgs, err = self._hist_raw()
        if err is not None:
            return self._json({"error": err}, 500)
        self._json({"messages": msgs})

    def _send(self):
        if not self._auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            msg = body.get("message", "")
            fire_and_forget = bool(body.get("fireAndForget"))
            if not msg:
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
                        _call_gateway("chat.send", params, timeout=120)
                    except Exception as e:
                        print(f"[bridge] fire-and-forget send failed: {e}", file=sys.stderr)
                threading.Thread(target=_bg, daemon=True).start()
                return self._json({"sent": True})

            prev, _ = self._hist_raw()
            prev_count = len(prev) if prev else 0

            try:
                _call_gateway("chat.send", params, timeout=45)
            except subprocess.TimeoutExpired:
                pass  # send may finish even after timeout; we poll

            for _ in range(30):
                time.sleep(2)
                h, _ = self._hist_raw()
                if h and len(h) > prev_count:
                    for m in reversed(h):
                        if m["role"] == "assistant":
                            return self._json(m)
            self._json({"error": "Response timeout"}, 504)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _stream(self):
        if not self._auth():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            msg = body.get("message", "")
            if not msg:
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
            prev, _ = self._hist_raw()
            prev_count = len(prev) if prev else 0

            idem = f"av-{uuid.uuid4().hex[:12]}"
            send_params = {
                "sessionKey": SESSION_KEY,
                "message": msg,
                "idempotencyKey": idem,
            }
            send_done = threading.Event()

            def _do_send():
                try:
                    _call_gateway("chat.send", send_params, timeout=120)
                except Exception:
                    pass
                finally:
                    send_done.set()

            threading.Thread(target=_do_send, daemon=True).start()
            _sse("status", {"phase": "thinking", "message": "Agent is thinking..."})

            max_polls = 60  # ~90s
            found = False
            for i in range(max_polls):
                time.sleep(1.5)
                if i > 0 and i % 3 == 0:
                    _sse("heartbeat", {"elapsed": int(i * 1.5)})

                h, _ = self._hist_raw()
                if h and len(h) > prev_count:
                    for m in reversed(h):
                        if m["role"] == "assistant":
                            content = m["content"]
                            chunk = 20
                            for j in range(0, len(content), chunk):
                                _sse("token", {"content": content[j:j + chunk], "done": False})
                                time.sleep(0.015)
                            _sse("token", {"content": "", "done": True})
                            _sse("done", {
                                "role": "assistant",
                                "content": content,
                                "timestamp": m.get("timestamp", int(time.time() * 1000)),
                            })
                            found = True
                            break
                    if found:
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
        out = {}
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
            r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
            out["host_uptime"] = r.stdout.strip()
        except Exception as e:
            out["host_uptime_error"] = str(e)
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
    if not GATEWAY_TOKEN:
        print("[bridge] GATEWAY_TOKEN env var is required", file=sys.stderr)
        sys.exit(1)
    server = ThreadingServer(("0.0.0.0", PORT), Handler)
    print(f"[bridge v{BRIDGE_VERSION}] listening on 0.0.0.0:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
