FROM ghcr.io/openclaw/openclaw:latest
USER root
# The base image (Debian bookworm + Node 24) ships without Python; the
# bridge.py HTTP shim and the entrypoint's inline JSON helpers both need
# it. python3-minimal is ~10MB and gives us http.server / json. We also
# need python3-websockets (v7+) so the bridge can hold a long-lived WS
# to the gateway instead of forking the openclaw CLI per request.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3-minimal \
      python3-websockets \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /usr/local/bin/av-entrypoint.sh
COPY bridge.py /usr/local/bin/av-bridge.py
RUN chmod +x /usr/local/bin/av-entrypoint.sh /usr/local/bin/av-bridge.py \
 && chown node:node /usr/local/bin/av-entrypoint.sh /usr/local/bin/av-bridge.py
USER node
ENTRYPOINT ["/usr/local/bin/av-entrypoint.sh"]
