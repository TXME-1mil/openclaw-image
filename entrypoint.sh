#!/bin/bash
# AgentVolt entrypoint for the bundled OpenClaw + bridge image (v7).
#
# Boot sequence:
#   1. Apply env-driven config (gateway token, providers, allowed origins)
#   2. Write per-agent auth-profiles.json so the agent has API keys on boot
#   3. Seed the agent workspace (AGENTS.md / SOUL.md) so the persona is set
#      before the first chat turn — otherwise the agent introduces itself
#      as "Hey, I just came online. Who am I?"
#   4. Start the gateway on an internal-only port (18789)
#   5. Start the HTTP bridge on the public port (18790) — the bridge keeps
#      a persistent WebSocket open to the gateway (v7 change)
#   6. Supervise both — if either dies, exit so Fly restarts the machine
#
# Env contract (all set by Fly secrets or machine config):
#   GATEWAY_TOKEN              required — Bearer token shared with Next.js
#   OPENAI_API_KEY             optional — primary provider
#   ANTHROPIC_API_KEY          optional — fallback provider
#   GOOGLE_API_KEY             optional — last-resort fallback
#   OPENCLAW_ALLOWED_ORIGINS   optional — comma-separated CORS origins
#   AGENT_NAME                 optional — display name for the persona
#   AGENT_PERSONA_AGENTS_MD    optional — full AGENTS.md contents (overrides default)
#   AGENT_PERSONA_SOUL_MD      optional — full SOUL.md contents (overrides default)
#   GATEWAY_PORT               default 18789 (internal)
#   BRIDGE_PORT                default 18790 (public)

set -eu -o pipefail

# Helper: run a non-fatal openclaw command. Failures are logged loudly to
# stderr so they show up in Fly logs (we missed a config-set failure for
# days because everything was `|| true`).
warn_run() {
  local label="$1"; shift
  if ! "$@" >/dev/null 2>&1; then
    echo "[entrypoint] WARN: ${label} failed (exit $?): $*" >&2
  fi
}

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
warn_run "config gateway.mode" openclaw config set gateway.mode local
warn_run "config gateway.auth.token" openclaw config set gateway.auth.token "$GATEWAY_TOKEN"
# Bonjour mDNS crashes in container environments (unhandled CIAO promise
# rejection). It's only used for LAN gateway discovery, which we don't need.
warn_run "disable bonjour" openclaw config set plugins.entries.bonjour.enabled false

# Allowed origins for the gateway controlUi (browser-side CORS).
if [ -n "${OPENCLAW_ALLOWED_ORIGINS:-}" ]; then
  if ORIGINS_JSON=$(printf '%s' "$OPENCLAW_ALLOWED_ORIGINS" | python3 -c "
import sys, json
items = [x.strip() for x in sys.stdin.read().split(',') if x.strip()]
print(json.dumps({'path':'gateway.controlUi.allowedOrigins','value':items}))
"); then
    echo "[$ORIGINS_JSON]" > /tmp/av-origins.json
    warn_run "set allowed origins" openclaw config set --batch-file /tmp/av-origins.json
    rm -f /tmp/av-origins.json
  else
    echo "[entrypoint] WARN: failed to build origins JSON; skipping" >&2
  fi
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
  warn_run "set providers" openclaw config set --batch-file /tmp/av-providers.json
  rm -f /tmp/av-providers.json
else
  echo "[entrypoint] WARN: no AI provider keys in env; agent will fail every chat turn" >&2
fi

if [ -n "$DEFAULT_MODEL" ]; then
  warn_run "set default model $DEFAULT_MODEL" openclaw models set "$DEFAULT_MODEL"
fi

# Validate that AGENT_PROFILES parses as JSON before writing — the previous
# bash-parameter-expansion bug produced trailing-garbage strings that
# silently broke OpenClaw's strict parser. A python json.loads round-trip
# is cheap insurance.
if [ "$AGENT_PROFILES" != '{"version":1,"profiles":{},"order":{},"lastGood":{}}' ]; then
  if printf '%s' "$AGENT_PROFILES" | python3 -c "import json,sys; json.load(sys.stdin)"; then
    printf '%s' "$AGENT_PROFILES" > "$OPENCLAW_STATE_DIR/agents/main/agent/auth-profiles.json"
    chmod 600 "$OPENCLAW_STATE_DIR/agents/main/agent/auth-profiles.json"
  else
    echo "[entrypoint] ERROR: AGENT_PROFILES is not valid JSON; refusing to write auth-profiles.json" >&2
    echo "[entrypoint] value was: $AGENT_PROFILES" >&2
    exit 1
  fi
fi

# ---- Persona seeding -----------------------------------------------------
# The agent loads AGENTS.md (operating guidance) and SOUL.md (persona/tone)
# from its workspace directory on every chat turn. Without these the agent
# starts every conversation with "Hey, I just came online. Who am I?",
# which makes the bot feel broken even when chat itself is working.
#
# Workspace dir = $HOME/.openclaw/workspace = /data/.openclaw/workspace
#
# We write *_missing_*: if a file already exists (e.g. user mounted in a
# custom one or persisted state from a previous run), we leave it alone.
WORKSPACE_DIR="$HOME/.openclaw/workspace"
mkdir -p "$WORKSPACE_DIR"

: "${AGENT_NAME:=AgentVolt}"

write_if_missing() {
  local path="$1"
  local content="$2"
  if [ ! -s "$path" ]; then
    printf '%s' "$content" > "$path"
    echo "[entrypoint] seeded $(basename "$path")" >&2
  fi
}

# Default AGENTS.md establishes the operating contract. Kept short so it
# doesn't dominate the system prompt budget; user can override via env.
DEFAULT_AGENTS_MD="# AGENTS.md

You are ${AGENT_NAME}, a personal AI assistant deployed via AgentVolt.

## Identity
- Your name is ${AGENT_NAME}.
- You run as a long-lived assistant: each chat continues the same session — there is no \"who am I\" reset between turns.
- When greeted, respond as ${AGENT_NAME} and be helpful immediately. Do NOT ask \"who am I?\" or describe yourself as just-booted.

## Style
- Be concise and direct. Match the user's tone.
- When the user asks for code, write the code. When they ask a question, answer it.
- Don't narrate what you're about to do — just do it.

## Capabilities
- You have tool access for code execution, file edits, and web search when needed.
- Use tools when they materially help; otherwise answer from context.
"

DEFAULT_SOUL_MD="# SOUL.md

I am ${AGENT_NAME}.

I am calm, direct, and warm. I treat the user as a capable peer.

I do not pretend to be discovering my identity — I know who I am, and I get to work.

When I make a mistake, I name it and correct it without melodrama.
"

if [ -n "${AGENT_PERSONA_AGENTS_MD:-}" ]; then
  printf '%s' "$AGENT_PERSONA_AGENTS_MD" > "$WORKSPACE_DIR/AGENTS.md"
  echo "[entrypoint] AGENTS.md set from env" >&2
else
  write_if_missing "$WORKSPACE_DIR/AGENTS.md" "$DEFAULT_AGENTS_MD"
fi

if [ -n "${AGENT_PERSONA_SOUL_MD:-}" ]; then
  printf '%s' "$AGENT_PERSONA_SOUL_MD" > "$WORKSPACE_DIR/SOUL.md"
  echo "[entrypoint] SOUL.md set from env" >&2
else
  write_if_missing "$WORKSPACE_DIR/SOUL.md" "$DEFAULT_SOUL_MD"
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
