#!/usr/bin/env bash
#
# Expose the Ollama running on THIS Mac to the deployed assistant service (AWS ECS).
#
# ECS cannot dial into your laptop -- it is behind NAT with no public IP. So the
# connection is made the other way: cloudflared opens an OUTBOUND tunnel and
# Cloudflare gives back a public HTTPS hostname that forwards to localhost:11434.
# No port forwarding, no router config, no firewall changes, no GPU bill.
#
#   bash deploy/ollama-tunnel.sh setup    # one-time: install deps, pull the model
#   bash deploy/ollama-tunnel.sh up       # start ollama + the tunnel (returns; does not block)
#   bash deploy/ollama-tunnel.sh status   # is the AI half actually reachable?
#   bash deploy/ollama-tunnel.sh down     # stop the tunnel and ollama
#   bash deploy/ollama-tunnel.sh url      # print the current tunnel hostname
#
# ---------------------------------------------------------------------------
# READ THIS BEFORE YOU LEAVE A TUNNEL RUNNING
# ---------------------------------------------------------------------------
# A quick tunnel is PUBLIC AND UNAUTHENTICATED. The hostname is random and
# unguessable, but anyone who learns it can send prompts to your laptop's model
# and burn your CPU until you stop the tunnel. That is an acceptable trade for a
# demo you start and stop; it is not something to leave running for days.
#
# Bring it up for a demo, `down` when done. If you later want it always-on, use a
# NAMED tunnel with Cloudflare Access in front (needs a free Cloudflare account
# and a domain) rather than leaving a quick tunnel open.
#
# Also note: the hostname CHANGES every time you restart a quick tunnel, so
# re-run `set-url` afterwards or the assistant will 503.
# ---------------------------------------------------------------------------

set -euo pipefail

MODEL="${MODEL:-llama3.1:8b}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

# Shared with deploy/session.sh ON PURPOSE. Both scripts start the same tunnel, so
# they must agree on where its PID and log live -- otherwise `status` after a
# `session.sh up` reports "not running" for a tunnel that is plainly running.
TUNNEL_LOG=/tmp/mh-cloudflared.log
TUNNEL_PID=/tmp/mh-cloudflared.pid

cmd_setup() {
  command -v ollama >/dev/null || { echo "Install Ollama first: https://ollama.com/download"; exit 1; }
  command -v cloudflared >/dev/null || brew install cloudflared
  echo "Pulling ${MODEL} (~4.7 GB, one time)..."
  ollama pull "$MODEL"
  echo "OK. Next: bash deploy/ollama-tunnel.sh up"
}

ollama_running() { curl -sf "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; }

tunnel_running() { [[ -f $TUNNEL_PID ]] && kill -0 "$(cat $TUNNEL_PID)" 2>/dev/null; }

# `|| true` is load-bearing: `set -o pipefail` is on, so a grep that matches nothing
# fails the whole pipeline, and under `set -e` that would kill the caller on the very
# first poll -- before the tunnel has had a chance to print its hostname. "No URL yet"
# is a normal state here, not an error.
tunnel_url() { grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true; }

start_ollama() {
  command -v ollama >/dev/null || { echo "Ollama is not installed: https://ollama.com/download"; exit 1; }
  if ollama_running; then
    # It is already up -- but we did not start it, so we cannot know whether it has
    # OLLAMA_ORIGINS set. An Ollama started by the menu-bar app or a launchd agent
    # does not, and it then REJECTS the tunnel on the Host header. The symptom is a
    # 503 from the assistant with a tunnel that looks perfectly healthy, so say so
    # here rather than letting it surprise you mid-demo. `verify` below catches it
    # for real by calling through the tunnel.
    echo "  ollama: already running (not started by this script)"
  else
    echo "  ollama: starting..."
    # OLLAMA_ORIGINS stops Ollama rejecting the tunnel's Host header as cross-origin.
    OLLAMA_ORIGINS='*' ollama serve >/tmp/mh-ollama.log 2>&1 &
    until ollama_running; do sleep 1; done
    echo "  ollama: up"
  fi
  curl -sf "http://${OLLAMA_HOST}/api/tags" | grep -q "$MODEL" \
    || { echo "  !! ${MODEL} is not pulled. Run: ollama pull ${MODEL}"; exit 1; }
  echo "  ollama: ${MODEL} present"
}

start_tunnel() {
  if tunnel_running; then
    echo "  tunnel: already running"
  else
    command -v cloudflared >/dev/null || { echo "cloudflared missing. Run: brew install cloudflared"; exit 1; }
    : > "$TUNNEL_LOG"
    # --http-host-header is REQUIRED, not a nicety. cloudflared forwards the
    # original Host (<random>.trycloudflare.com), and Ollama >=0.28 rejects any
    # Host it does not recognise with a bare 403 as DNS-rebinding protection.
    # OLLAMA_ORIGINS does NOT cover this -- that governs the Origin header, which
    # is a different check. Without this flag every tunneled request 403s while
    # localhost keeps answering 200, so the tunnel looks healthy from here and the
    # assistant 503s with no useful error anywhere.
    cloudflared tunnel --url "http://${OLLAMA_HOST}" --http-host-header localhost >"$TUNNEL_LOG" 2>&1 &
    echo $! > "$TUNNEL_PID"
    echo "  tunnel: starting..."
  fi
  local url
  for _ in $(seq 1 30); do
    url="$(tunnel_url)"; [[ -n "$url" ]] && break
    sleep 2
  done
  [[ -n "${url:-}" ]] || { echo "  !! tunnel did not report a URL. See $TUNNEL_LOG"; exit 1; }
  echo "  tunnel: $url"
}

# The check that matters. Everything can look up locally and still be unreachable
# from outside, which is precisely the failure this whole script exists to avoid.
#
# Two distinct failures hide behind "it does not answer", and they need opposite
# responses, so this reads the HTTP code rather than just curl's success:
#
#   000  no connection -- almost always the fresh hostname not yet in DNS. A quick
#        tunnel's name is NXDOMAIN for a few tens of seconds after creation, so
#        checking once and giving up reports a healthy tunnel as broken. Retry.
#   403  Ollama rejecting the Host header. Retrying cannot fix it; say so at once.
verify() {
  local url code; url="$(tunnel_url)"
  [[ -n "$url" ]] || { echo "  !! no tunnel URL to verify"; return 1; }
  # Wait before the FIRST probe. Querying the hostname before Cloudflare has
  # published its DNS record makes macOS negatively-cache the NXDOMAIN, and the
  # tunnel then looks dead from this Mac for the whole negative TTL even though it
  # is serving fine -- the check breaks the thing it is checking. Give the record
  # time to exist first.
  sleep 12
  for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${url}/api/tags" 2>/dev/null)" || code=000
    code="${code:-000}"
    case "$code" in
      200)
        echo "  verified: ${url}/api/tags answers from outside"
        return 0 ;;
      403)
        echo "  !! ${url} returns 403 -- Ollama is rejecting the tunnel's Host header."
        echo "     The tunnel must run with --http-host-header localhost. OLLAMA_ORIGINS"
        echo "     does NOT fix this; it governs the Origin header, a different check."
        echo "     Restart the tunnel:  bash deploy/ollama-tunnel.sh down && bash deploy/ollama-tunnel.sh up"
        return 1 ;;
    esac
    sleep 5
  done
  echo "  !! ${url} still not answering after ~100s (last HTTP code: ${code})."
  if [[ "$code" == "000" ]]; then
    echo "     000 = no connection. If 'host ${url#https://} 1.1.1.1' resolves but a plain"
    echo "     'host ${url#https://}' says NXDOMAIN, this Mac has cached the negative"
    echo "     lookup; flush it with:"
    echo "         sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder"
    echo "     Otherwise see $TUNNEL_LOG."
  fi
  return 1
}

cmd_up() {
  start_ollama
  start_tunnel
  verify || exit 1
  echo
  echo "AI half is up. Point the deployed assistant at it with:"
  echo "    bash deploy/ollama-tunnel.sh set-url $(tunnel_url)"
}

cmd_status() {
  ollama_running && echo "ollama: running" || echo "ollama: not running"
  if tunnel_running; then
    echo "tunnel: $(tunnel_url)"
    verify || true
  else
    echo "tunnel: not running"
  fi
}

cmd_down() {
  if tunnel_running; then
    # `|| true` so that losing a race with an already-dying process does not abort
    # the function under `set -e` and leave ollama below still running.
    kill "$(cat $TUNNEL_PID)" 2>/dev/null || true
    echo "tunnel: stopped"
  else
    echo "tunnel: not running"
  fi
  rm -f "$TUNNEL_PID"
  if ollama_running; then
    pkill -f 'ollama serve' 2>/dev/null || pkill ollama 2>/dev/null || true
    echo "ollama: stopped"
  else
    echo "ollama: not running"
  fi
}

# Point the deployed assistant at the current tunnel hostname.
#
# Delegates to the AWS deploy script rather than duplicating its account guards --
# there is exactly one place that knows which AWS profile is safe to use.
cmd_set_url() {
  local url="${1:-$(tunnel_url)}"
  [[ -n "$url" ]] || { echo "Usage: $0 set-url https://<host>.trycloudflare.com  (no tunnel running to infer from)"; exit 1; }
  # Fail fast on a URL that is not actually serving Ollama -- otherwise the only
  # symptom is a 503 from the assistant with no obvious cause. The usual mistake is
  # pasting the APP's load-balancer URL here; that is the opposite direction. This
  # argument is your Mac, not the deployment.
  curl -sf "${url}/api/tags" >/dev/null || {
    echo "That URL is not serving Ollama: $url"
    echo "Expected a https://<random>.trycloudflare.com hostname -- the tunnel to THIS Mac,"
    echo "not the app's *.elb.amazonaws.com address. Is the tunnel up? (\`$0 status\`)"
    exit 1
  }
  bash "$(dirname "${BASH_SOURCE[0]}")/aws/deploy.sh" set-tunnel "$url"
}

case "${1:-}" in
  setup)   cmd_setup ;;
  up)      cmd_up ;;
  status)  cmd_status ;;
  down)    cmd_down ;;
  url)     tunnel_url ;;
  set-url) shift; cmd_set_url "$@" ;;
  *) sed -n '1,32p' "${BASH_SOURCE[0]}"; echo "Usage: $0 {setup|up|status|down|url|set-url [url]}" ;;
esac
