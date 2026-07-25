#!/usr/bin/env bash
#
# One command up, one command down -- for a temporary demo of the whole system.
#
#   bash deploy/session.sh up     # AWS stack + Ollama + tunnel, wired together
#   bash deploy/session.sh down   # tear it ALL down, billing stops
#   bash deploy/session.sh status # what is running and what it is costing
#
# `up` takes ~10 minutes (most of it AWS creating the load balancer).
# `down` takes ~5 minutes. Your container images stay in ECR either way, so
# bringing it back up never re-uploads ~500 MB.
#
# ---------------------------------------------------------------------------
# WHAT COSTS MONEY
# ---------------------------------------------------------------------------
# Only the AWS half. Roughly $0.09/hour with everything running -- a 4-hour demo
# is about $0.36. Ollama runs on this Mac and the Cloudflare quick tunnel is free.
#
# Nothing here bills while it is down, so ALWAYS run `down` when you finish.
# If you forget, the budget alarm emails groupavalanche4@gmail.com at $10.
# ---------------------------------------------------------------------------

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
TUNNEL_LOG=/tmp/mh-cloudflared.log
TUNNEL_PID=/tmp/mh-cloudflared.pid

# --- the AI half (this machine) ---------------------------------------------
ollama_up() {
  command -v ollama >/dev/null || { echo "Ollama is not installed: https://ollama.com/download"; exit 1; }
  if curl -sf "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    echo "  ollama: already running"
  else
    echo "  ollama: starting..."
    OLLAMA_ORIGINS='*' ollama serve >/tmp/mh-ollama.log 2>&1 &
    until curl -sf "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; do sleep 1; done
    echo "  ollama: up"
  fi
  # Fail now, with a clear message, rather than as a 503 mid-demo.
  curl -sf "http://${OLLAMA_HOST}/api/tags" | grep -q "llama3.1:8b" \
    || { echo "  !! llama3.1:8b is not pulled. Run: ollama pull llama3.1:8b"; exit 1; }
  echo "  ollama: llama3.1:8b present"
}

tunnel_up() {
  if [[ -f $TUNNEL_PID ]] && kill -0 "$(cat $TUNNEL_PID)" 2>/dev/null; then
    echo "  tunnel: already running"
  else
    command -v cloudflared >/dev/null || { echo "cloudflared missing. Run: brew install cloudflared"; exit 1; }
    : > "$TUNNEL_LOG"
    cloudflared tunnel --url "http://${OLLAMA_HOST}" >"$TUNNEL_LOG" 2>&1 &
    echo $! > "$TUNNEL_PID"
    echo "  tunnel: starting..."
  fi
  # cloudflared prints the hostname a few seconds after start.
  for _ in $(seq 1 30); do
    TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1) && [[ -n "$TUNNEL_URL" ]] && break
    sleep 2
  done
  [[ -n "${TUNNEL_URL:-}" ]] || { echo "  !! tunnel did not report a URL. See $TUNNEL_LOG"; exit 1; }
  export TUNNEL_URL
  echo "  tunnel: $TUNNEL_URL"
}

tunnel_down() {
  if [[ -f $TUNNEL_PID ]] && kill -0 "$(cat $TUNNEL_PID)" 2>/dev/null; then
    kill "$(cat $TUNNEL_PID)" && echo "  tunnel: stopped"
  else
    echo "  tunnel: not running"
  fi
  rm -f "$TUNNEL_PID"
}

# --- the whole system --------------------------------------------------------
cmd_up() {
  echo "=== 1/3  AI on this machine ==="
  ollama_up
  tunnel_up

  echo "=== 2/3  AWS (this is the slow part, ~8 min) ==="
  # TUNNEL_URL is exported above, so the assistant task is created already knowing
  # where to find Ollama -- no second deploy needed.
  echo y | CAPACITY="${CAPACITY:-FARGATE}" bash "$HERE/aws/deploy.sh" 3

  echo "=== 3/3  checking it actually works ==="
  local url; url=$(app_url)
  until curl -sf "$url/api/health" >/dev/null 2>&1; do echo "  waiting for the load balancer..."; sleep 15; done
  curl -s "$url/api/assistant/health" | grep -q '"ollama_configured": *true' \
    && echo "  assistant: wired to the tunnel" || echo "  !! assistant is not seeing the tunnel"
  echo
  echo "  OPEN:  $url"
  echo "  When you are finished:  bash deploy/session.sh down"
}

cmd_down() {
  echo "=== tearing down AWS (billing stops when this finishes) ==="
  bash "$HERE/aws/deploy.sh" destroy
  echo "=== stopping the tunnel ==="
  tunnel_down
  echo
  echo "Down. Ollama is left running on this Mac (it costs nothing);"
  echo "stop it with:  pkill ollama"
}

app_url() {
  aws --profile "${PROFILE:-avalanche}" --region "${REGION:-ca-west-1}" \
    cloudformation describe-stacks --stack-name "${STACK:-mount-hosmer-twin}" \
    --query "Stacks[0].Outputs[?OutputKey=='AppUrl'].OutputValue" --output text 2>/dev/null
}

cmd_status() {
  local url; url=$(app_url)
  if [[ -z "$url" ]]; then
    echo "AWS:    not deployed  (costing $0/hour)"
  else
    echo "AWS:    $url"
    echo "        ~\$0.09/hour while this exists -- run 'down' when finished"
  fi
  if [[ -f $TUNNEL_PID ]] && kill -0 "$(cat $TUNNEL_PID)" 2>/dev/null; then
    echo "tunnel: $(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)"
  else
    echo "tunnel: not running"
  fi
  curl -sf "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1 \
    && echo "ollama: running" || echo "ollama: not running"
}

case "${1:-}" in
  up)     cmd_up ;;
  down)   cmd_down ;;
  status) cmd_status ;;
  *) sed -n '1,25p' "${BASH_SOURCE[0]}"; echo "Usage: $0 {up|down|status}" ;;
esac
