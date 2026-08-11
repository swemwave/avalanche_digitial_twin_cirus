#!/usr/bin/env bash
#
# The two halves of the deployed system, controlled separately.
#
#   WEB  the AWS half: assess + assistant + frontend on ECS behind one ALB.
#        Costs money while it exists (~$0.09/hour). This is the part to turn off.
#
#   AI   the local half: Ollama on THIS Mac plus the Cloudflare tunnel that lets
#        the deployed assistant reach it. Free, but the tunnel is public while up.
#
#   bash deploy/twin.sh web start | status | stop
#   bash deploy/twin.sh ai  start | status | stop
#   bash deploy/twin.sh status                      # both halves at once
#
# They are separate because they fail separately: the app can be perfectly healthy
# while the AI is unreachable, and that is the single most common way a demo goes
# wrong. `status` on either half tells you which one is broken.
#
# ORDER MATTERS on a cold start. `web start` bakes the current tunnel hostname into
# the assistant's config, so bring the AI up FIRST and the wiring is automatic:
#
#     bash deploy/twin.sh ai start
#     bash deploy/twin.sh web start
#
# If you start them the other way round, or the tunnel restarts later (its hostname
# is random and changes every time), re-wire without redeploying anything else:
#
#     bash deploy/ollama-tunnel.sh set-url
#
# For a one-shot "everything up, everything down" demo, deploy/session.sh still
# does both halves in a single command. This script is for when you want to stop
# paying for AWS but leave the AI running, or restart the AI without touching AWS.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI="$HERE/ollama-tunnel.sh"
AWS_SH="$HERE/aws/deploy.sh"

# --- web (AWS) ---------------------------------------------------------------
web_start() {
  # Hand the assistant the tunnel hostname at create time if the AI half is
  # already up. Empty is fine -- the stack deploys with the assistant unwired and
  # `ollama-tunnel.sh set-url` can point it later.
  local tunnel; tunnel="$(bash "$AI" url || true)"
  if [[ -n "$tunnel" ]]; then
    echo "  wiring assistant to $tunnel"
  else
    echo "  no tunnel running -- deploying with the assistant UNWIRED."
    echo "  after 'twin.sh ai start', run: bash deploy/ollama-tunnel.sh set-url"
  fi
  # deploy.sh 3 prompts to confirm the AWS account before creating anything. That
  # guard is deliberate; it is not bypassed here.
  TUNNEL_URL="$tunnel" bash "$AWS_SH" 3
}

web_status() {
  local url; url="$(bash "$AWS_SH" app-url)"
  if [[ -z "$url" ]]; then
    echo "web:    not deployed  (costing \$0/hour)"
    return 0
  fi
  echo "web:    $url"
  echo "        ~\$0.09/hour while this exists -- 'twin.sh web stop' when finished"
  curl -sf --max-time 15 "$url/api/health" >/dev/null 2>&1 \
    && echo "        assess:    healthy" \
    || echo "        assess:    NOT answering (task may still be starting)"
  # The assistant reports whether it knows an Ollama URL. True here plus a healthy
  # `ai status` is the combination that means the AI will actually work.
  local a; a="$(curl -sf --max-time 15 "$url/api/assistant/health" 2>/dev/null || true)"
  if [[ -z "$a" ]]; then
    echo "        assistant: NOT answering"
  elif echo "$a" | grep -q '"ollama_configured": *true'; then
    echo "        assistant: wired to a tunnel"
  else
    echo "        assistant: answering but NOT wired -- run: bash deploy/ollama-tunnel.sh set-url"
  fi
}

web_stop() { bash "$AWS_SH" destroy; }

# --- ai (this Mac) -----------------------------------------------------------
ai_start()  { bash "$AI" up; }
ai_status() { bash "$AI" status; }
ai_stop()   { bash "$AI" down; }

both_status() { web_status; echo; ai_status; }

usage() { sed -n '1,30p' "${BASH_SOURCE[0]}"; echo "Usage: $0 {web|ai} {start|status|stop}   |   $0 status"; }

case "${1:-}" in
  web)
    case "${2:-}" in
      start)  web_start ;;
      status) web_status ;;
      stop)   web_stop ;;
      *) usage; exit 1 ;;
    esac ;;
  ai)
    case "${2:-}" in
      start)  ai_start ;;
      status) ai_status ;;
      stop)   ai_stop ;;
      *) usage; exit 1 ;;
    esac ;;
  status) both_status ;;
  *) usage; exit 1 ;;
esac
