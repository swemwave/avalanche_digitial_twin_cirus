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
# If you forget, the AWS budget alarm emails its configured address at $10.
# (Set one up if you have not -- see docs/deployment.md section 7.)
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
  echo "=== 1/4  AI on this machine ==="
  ollama_up
  tunnel_up

  # `up` deploys the stack; it does NOT build images. That is deliberate -- pushing
  # ~900 MB of assess image every time you start a demo would be absurd. But it means
  # `up` alone silently ships whatever is already in ECR, so if you have changed code
  # since the last push you would demo the OLD build and have no way to tell. Check
  # first: 10 seconds here against 10 minutes of deploying the wrong thing.
  echo "=== 2/4  checking the images this deploy will use ==="
  echo y | bash "$HERE/aws/deploy.sh" 1 >/dev/null 2>&1 || true   # ECR repos; idempotent
  if ! bash "$HERE/aws/deploy.sh" check; then
    echo
    echo "Stopping before the slow part. Ollama and the tunnel are up and stay up."
    exit 1
  fi

  echo "=== 3/4  AWS (this is the slow part, ~8 min) ==="
  # TUNNEL_URL is exported above, so the assistant task is created already knowing
  # where to find Ollama -- no second deploy needed.
  echo y | CAPACITY="${CAPACITY:-FARGATE}" bash "$HERE/aws/deploy.sh" 3

  echo "=== 4/4  checking it actually works ==="
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
  # Ask for the STATUS as well as the URL. `describe-stacks` will happily return a
  # DELETE_COMPLETE stack along with its old outputs, so querying the URL alone
  # reports a torn-down stack as live -- which is exactly the wrong direction for
  # something whose job is to tell you whether you are still being billed.
  local out status url
  out=$(aws --profile "${PROFILE:-avalanche}" --region "${REGION:-ca-west-1}" \
        cloudformation describe-stacks --stack-name "${STACK:-mount-hosmer-twin}" \
        --query "Stacks[0].[StackStatus,Outputs[?OutputKey=='AppUrl']|[0].OutputValue]" \
        --output text 2>/dev/null) || return 0
  status=$(echo "$out" | awk '{print $1}')
  url=$(echo "$out" | awk '{print $2}')
  case "$status" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) echo "$url" ;;
    *) return 0 ;;   # DELETE_*, ROLLBACK_COMPLETE, *_IN_PROGRESS: not usable
  esac
}

cmd_status() {
  local url; url=$(app_url)
  if [[ -z "$url" ]]; then
    # \$0 escaped: inside double quotes a bare $0 expands to the script name.
    echo "AWS:    not deployed  (costing \$0/hour)"
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
