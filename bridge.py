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
BRIDGE_VERSION = 5
SESSION_KEY = "agent:main:main"
# Cap on request body size for /chat/send and /chat/stream. Defends against
# a malicious Content-Length header allocating arbitrary memory. A chat
# message of 256KB is plenty (~50 pages of plain text).
MAX_BODY_BYTES = 256 * 1024

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

    def _read_body(self):
        """Read and JSON-parse the request body with bounded size + validated
        Content-Length. Returns (body_dict, None) on success or (None, error_response_status)."""
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
        """Returns (messages, error, timed_out). On timeout, callers should
        treat as transient and report 504 (not 500)."""
        try:
            r = _call_gateway("chat.history", {"sessionKey": SESSION_KEY})
            if r.returncode != 0:
                return None, r.stderr.strip(), False
            data = json.loads(r.stdout)
            return _filter_messages(data.get("messages", [])), None, False
        except subprocess.TimeoutExpired as e:
            return None, f"gateway timeout: {e}", True
        except Exception as e:
            return None, str(e), False

    def _history(self):
        if not self._auth():
            return
        msgs, err, timed_out = self._hist_raw()
        if err is not None:
            return self._json({"error": err}, 504 if timed_out else 500)
        self._json({"messages": msgs})

    def _send(self):
        if not self._auth():
            return
        try:
            body, body_err = self._read_body()
            if body_err is not None:
                return  # _read_body already responded
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
                        _call_gateway("chat.send", params, timeout=120)
                    except Exception as e:
                        print(f"[bridge] fire-and-forget send failed: {e}", file=sys.stderr)
                threading.Thread(target=_bg, daemon=True).start()
                return self._json({"sent": True})

            # Track the timestamp of the last assistant message we saw before
            # send — newer is the only reliable "new reply arrived" signal.
            # The previous len-based check missed replies if openclaw produced
            # zero net new history events (e.g. dedup'd send).
            prev, _, _ = self._hist_raw()
            prev_last_asst_ts = max(
                (m.get("timestamp", 0) for m in (prev or []) if m.get("role") == "assistant"),
                default=0,
            )

            try:
                _call_gateway("chat.send", params, timeout=45)
            except subprocess.TimeoutExpired:
                pass  # send may finish even after timeout; we poll

            # Cap at ~50s of polling so we exit before Vercel's 60s timeout.
            # That leaves Vercel 10s to serialize + return the response.
            for _ in range(25):
                time.sleep(2)
                h, _, _ = self._hist_raw()
                if not h:
                    continue
                for m in reversed(h):
                    if m["role"] == "assistant" and m.get("timestamp", 0) > prev_last_asst_ts:
                        return self._json(m)
            self._json({"error": "Response timeout"}, 504)
        except Exception as e:
            self._json({"error": str(e)}, 500)

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
            prev, _, _ = self._hist_raw()
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
                    _call_gateway("chat.send", send_params, timeout=120)
                except Exception:
                    pass
                finally:
                    send_done.set()

            threading.Thread(target=_do_send, daemon=True).start()
            _sse("status", {"phase": "thinking", "message": "Agent is thinking..."})

            # Cap polls so total elapsed (~52s) stays under Vercel's 60s
            # function timeout — leaves 8s margin for the final stream flush.
            max_polls = 35  # 35 * 1.5s = 52.5s
            found = False
            for i in range(max_polls):
                time.sleep(1.5)
                if i > 0 and i % 3 == 0:
                    _sse("heartbeat", {"elapsed": int(i * 1.5)})

                h, _, _ = self._hist_raw()
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
