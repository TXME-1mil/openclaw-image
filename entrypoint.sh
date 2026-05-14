#!/bin/bash
# AgentVolt entrypoint for the bundled OpenClaw + bridge image.
#
# Boot sequence:
#   1. Apply env-driven config (gateway token, providers, allowed origins)
#   2. Write per-agent auth-profiles.json so the agent has API keys on boot
#   3. Start the gateway on an internal-only port (18789)
#   4. Start the HTTP bridge on the public port (18790)
#   5. Supervise both — if either dies, exit so Fly restarts the machine
#
# Env contract (all set by Fly secrets or machine config):
#   GATEWAY_TOKEN              required — Bearer token shared with Next.js
#   OPENAI_API_KEY             optional — primary provider
#   ANTHROPIC_API_KEY          optional — fallback provider
#   GOOGLE_API_KEY             optional — last-resort fallback
#   OPENCLAW_ALLOWED_ORIGINS   optional — comma-separated CORS origins
#   GATEWAY_PORT               default 18789 (internal)
#   BRIDGE_PORT                default 18790 (public)

set -eu

: "${GATEWAY_PORT:=18789}"
: "${BRIDGE_PORT:=18790}"
: "${OPENCLAW_STATE_DIR:=/data}"
: "${HOME:=/data}"
export GATEWAY_PORT BRIDGE_PORT OPENCLAW_STATE_DIR HOME
export OPENCLAW_NO_RESPAWN=1

if [ -z "${GATEWAY_TOKEN:-}" ]; then
  echo "[entrypoint] GATEWAY_TOKEN env var is required" >&2
  exit 1
fi

mkdir -p "$OPENCLAW_STATE_DIR/agents/main/agent"

# ---- Gateway config -------------------------------------------------------
openclaw config set gateway.mode local >/dev/null 2>&1 || true
openclaw config set gateway.auth.token "$GATEWAY_TOKEN" >/dev/null 2>&1 || true
# Bonjour mDNS crashes in container environments (unhandled CIAO promise
# rejection). It's only used for LAN gateway discovery, which we don't need.
openclaw config set plugins.entries.bonjour.enabled false >/dev/null 2>&1 || true

# Allowed origins for the gateway controlUi (browser-side CORS).
if [ -n "${OPENCLAW_ALLOWED_ORIGINS:-}" ]; then
  ORIGINS_JSON=$(printf '%s' "$OPENCLAW_ALLOWED_ORIGINS" | python3 -c "
import sys, json
items = [x.strip() for x in sys.stdin.read().split(',') if x.strip()]
print(json.dumps({'path':'gateway.controlUi.allowedOrigins','value':items}))
")
  echo "[$ORIGINS_JSON]" > /tmp/av-origins.json
  openclaw config set --batch-file /tmp/av-origins.json >/dev/null 2>&1 || true
  rm -f /tmp/av-origins.json
fi

# ---- Provider config + per-agent auth ------------------------------------
PROVIDERS_JSON='[]'
DEFAULT_MODEL=""
AGENT_PROFILES='{"version":1,"profiles":{},"order":{},"lastGood":{}}'

# Build the providers batch + agent auth profiles using python3 (already
# installed for the origins step). One JSON-aware pass beats shell heredoc
# escaping when keys contain special chars.
#
# Capture via `mapfile -t`, NOT `read` — `read X Y Z` only consumes one line
# and splits it on IFS, which clobbers JSON that contains spaces. mapfile
# reads each printed line into one array slot, preserving whitespace.
# Belt-and-suspenders: json.dumps uses compact separators, so the output
# also stays single-line and space-free regardless.
mapfile -t _AV_LINES < <(python3 <<'PY'
import json, os

providers = []
profiles = {}
order = {}
last_good = {}
fallback = []
default_model = ""

oa = os.environ.get("OPENAI_API_KEY", "").strip()
an = os.environ.get("ANTHROPIC_API_KEY", "").strip()
go = os.environ.get("GOOGLE_API_KEY", "").strip()

if oa:
    providers.append({"path":"models.providers.openai","value":{
        "baseUrl":"https://api.openai.com/v1","apiKey":oa,
        "models":[{"id":"gpt-4o","name":"GPT-4o"}]}})
    profiles["openai"] = {"type":"api_key","provider":"openai","key":oa}
    order["openai"] = ["openai"]
    last_good["openai"] = "openai"
    fallback.append("openai/gpt-4o")
    if not default_model:
        default_model = "openai/gpt-4o"
if an:
    providers.append({"path":"models.providers.anthropic","value":{
        "baseUrl":"https://api.anthropic.com","apiKey":an,
        "models":[{"id":"claude-sonnet-4-20250514","name":"Claude Sonnet"}]}})
    profiles["anthropic"] = {"type":"api_key","provider":"anthropic","key":an}
    order["anthropic"] = ["anthropic"]
    last_good["anthropic"] = "anthropic"
    fallback.append("anthropic/claude-sonnet-4-20250514")
    if not default_model:
        default_model = "anthropic/claude-sonnet-4-20250514"
if go:
    providers.append({"path":"models.providers.google","value":{
        "baseUrl":"https://generativelanguage.googleapis.com/v1beta","apiKey":go,
        "models":[{"id":"gemini-2.0-flash","name":"Gemini 2.0 Flash"}]}})
    profiles["google"] = {"type":"api_key","provider":"google","key":go}
    order["google"] = ["google"]
    last_good["google"] = "google"
    fallback.append("google/gemini-2.0-flash")
    if not default_model:
        default_model = "google/gemini-2.0-flash"

if len(fallback) > 1:
    providers.append({"path":"models.fallback","value":fallback})

_C = (",", ":")  # compact JSON — no whitespace inside the values
print(json.dumps(providers, separators=_C))
print(default_model)
print(json.dumps({"version":1,"profiles":profiles,"order":order,"lastGood":last_good}, separators=_C))
PY
)
# Plain `${var:-default}` with curly-brace-containing defaults is fragile:
# bash's brace-counting in parameter expansion was leaking the default into
# the value even when _AV_LINES[2] was non-empty, producing an
# auth-profiles.json with trailing garbage that broke OpenClaw's parser.
# Explicit ifs are bulletproof.
if [ -n "${_AV_LINES[0]:-}" ]; then PROVIDERS_JSON="${_AV_LINES[0]}"; else PROVIDERS_JSON='[]'; fi
DEFAULT_MODEL="${_AV_LINES[1]:-}"
if [ -n "${_AV_LINES[2]:-}" ]; then
  AGENT_PROFILES="${_AV_LINES[2]}"
else
  AGENT_PROFILES='{"version":1,"profiles":{},"order":{},"lastGood":{}}'
fi
unset _AV_LINES

if [ "$PROVIDERS_JSON" != "[]" ]; then
  echo "$PROVIDERS_JSON" > /tmp/av-providers.json
  openclaw config set --batch-file /tmp/av-providers.json >/dev/null 2>&1 || true
  rm -f /tmp/av-providers.json
fi

if [ -n "$DEFAULT_MODEL" ]; then
  openclaw models set "$DEFAULT_MODEL" >/dev/null 2>&1 || true
fi

if [ "$AGENT_PROFILES" != '{"version":1,"profiles":{},"order":{},"lastGood":{}}' ]; then
  echo "$AGENT_PROFILES" > "$OPENCLAW_STATE_DIR/agents/main/agent/auth-profiles.json"
  chmod 600 "$OPENCLAW_STATE_DIR/agents/main/agent/auth-profiles.json"
fi

# ---- Process supervision -------------------------------------------------
# Trap so we always kill children on shutdown signal from Fly.
shutdown() {
  jobs -p | xargs -r kill 2>/dev/null || true
  exit 0
}
trap shutdown TERM INT

# Start gateway in background, bound to all interfaces so the bridge can
# reach it on localhost. Fly only routes the public port (BRIDGE_PORT), so
# the gateway port is not reachable from outside the machine.
node openclaw.mjs gateway \
  --allow-unconfigured \
  --port "$GATEWAY_PORT" \
  --bind lan &
GATEWAY_PID=$!

# Wait up to 30s for the gateway to start listening before launching the
# bridge. Bash /dev/tcp probes — no curl dependency needed.
for i in $(seq 1 30); do
  if (echo > "/dev/tcp/127.0.0.1/$GATEWAY_PORT") >/dev/null 2>&1; then
    echo "[entrypoint] gateway ready after ${i}s"
    break
  fi
  sleep 1
done

python3 /usr/local/bin/av-bridge.py &
BRIDGE_PID=$!

# Exit (and let Fly restart the machine) if either process dies.
while kill -0 "$GATEWAY_PID" 2>/dev/null && kill -0 "$BRIDGE_PID" 2>/dev/null; do
  sleep 5
done

echo "[entrypoint] supervised process exited — gateway=$GATEWAY_PID bridge=$BRIDGE_PID" >&2
jobs -p | xargs -r kill 2>/dev/null || true
exit 1
