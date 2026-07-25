#!/usr/bin/env bash
#
# Expose the Ollama running on THIS Mac to the Cloud Run assistant service.
#
# Cloud Run cannot dial into your laptop -- it is behind NAT with no public IP. So
# the connection is made the other way: cloudflared opens an OUTBOUND tunnel and
# Cloudflare gives back a public HTTPS hostname that forwards to localhost:11434.
# No port forwarding, no router config, no firewall changes, no GPU bill.
#
#   bash deploy/ollama-tunnel.sh setup    # one-time: install deps, pull the model
#   bash deploy/ollama-tunnel.sh up       # start ollama + the tunnel, print the URL
#
# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU LEAVE A TUNNEL RUNNING
# ---------------------------------------------------------------------------
# A quick tunnel is PUBLIC AND UNAUTHENTICATED. The hostname is random and
# unguessable, but anyone who learns it can send prompts to your laptop's model
# and burn your CPU until you stop the tunnel. That is an acceptable trade for a
# demo you start and stop; it is not something to leave running for days.
#
# Bring it up for a demo, Ctrl-C when done. If you later want it always-on, use a
# NAMED tunnel with Cloudflare Access in front (needs a free Cloudflare account
# and a domain) rather than leaving a quick tunnel open.
#
# Also note: the hostname CHANGES every time you restart a quick tunnel, so
# re-run `set-url` afterwards or the assistant will 503.
# ---------------------------------------------------------------------------

set -euo pipefail

MODEL="${MODEL:-llama3.1:8b}"

cmd_setup() {
  command -v ollama >/dev/null || { echo "Install Ollama first: https://ollama.com/download"; exit 1; }
  command -v cloudflared >/dev/null || brew install cloudflared
  echo "Pulling ${MODEL} (~4.7 GB, one time)..."
  ollama pull "$MODEL"
  echo "OK. Next: bash deploy/ollama-tunnel.sh up"
}

cmd_up() {
  # OLLAMA_HOST binds beyond loopback so cloudflared can reach it; OLLAMA_ORIGINS
  # stops Ollama rejecting the tunnel's Host header as a cross-origin request.
  export OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
  export OLLAMA_ORIGINS="${OLLAMA_ORIGINS:-*}"

  if ! curl -sf "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    echo "Starting ollama serve..."
    ollama serve >/tmp/ollama.log 2>&1 &
    until curl -sf "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; do sleep 1; done
  fi
  echo "Ollama is up on ${OLLAMA_HOST}."

  echo "Opening tunnel. The https://<random>.trycloudflare.com line below is your URL."
  echo "Then, in another terminal:"
  echo "    bash deploy/ollama-tunnel.sh set-url https://<that-host>"
  echo
  cloudflared tunnel --url "http://${OLLAMA_HOST}"
}

# Point the deployed assistant at the current tunnel hostname.
#
# Delegates to the AWS deploy script rather than duplicating its account guards --
# there is exactly one place that knows which AWS profile is safe to use.
cmd_set_url() {
  local url="${1:?Usage: $0 set-url https://<host>.trycloudflare.com}"
  # Fail fast on a URL that is not actually serving Ollama -- otherwise the only
  # symptom is a 503 from the assistant with no obvious cause.
  curl -sf "${url}/api/tags" >/dev/null || { echo "That URL is not serving Ollama. Is the tunnel up?"; exit 1; }
  bash "$(dirname "${BASH_SOURCE[0]}")/aws/deploy.sh" set-tunnel "$url"
}

case "${1:-}" in
  setup)   cmd_setup ;;
  up)      cmd_up ;;
  set-url) shift; cmd_set_url "$@" ;;
  *) sed -n '1,30p' "${BASH_SOURCE[0]}"; echo "Usage: $0 {setup|up|set-url <url>}" ;;
esac
