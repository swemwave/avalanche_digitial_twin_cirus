#!/usr/bin/env bash
#
# Deploy the Mount Hosmer digital twin to AWS ECS Fargate behind one ALB.
#
#   assess     terrain + tiles + the hazard model   (carries the 188 MB bake)
#   assistant  the AI, calling YOUR laptop's Ollama through a Cloudflare Tunnel
#   frontend   the Next.js single screen
#
# All three sit behind ONE load balancer doing path-based routing, so they share an
# origin and there is no CORS anywhere.
#
# ---------------------------------------------------------------------------
# ACCOUNT SAFETY -- read this first
# ---------------------------------------------------------------------------
# This Mac has an existing AWS profile called `talha` which belongs to a personal
# account that must NOT be used. Every command here passes --profile explicitly and
# defaults to a SEPARATE profile name, and `guard_account` prints the account ID and
# makes you confirm before anything is created. Never set AWS_PROFILE=talha, and
# never add a [default] profile.
#
# ---------------------------------------------------------------------------
# BEFORE YOU RUN THIS -- the parts only you can do
# ---------------------------------------------------------------------------
#   1. Create a NEW AWS account at https://aws.amazon.com using
#      groupavalanche4@gmail.com. (AWS accounts are tied to one email, so this
#      cannot reuse the existing one.) Needs a card; $100 credits apply here.
#   2. In the AWS Console: IAM -> Users -> Create user -> attach
#      AdministratorAccess -> Security credentials -> Create access key -> CLI.
#   3. aws configure --profile avalanche
#        (paste the key + secret, region us-west-2, output json)
#
# Then: bash deploy/aws/deploy.sh <step>
# ---------------------------------------------------------------------------

set -euo pipefail

PROFILE="${PROFILE:-avalanche}"        # NOT `talha` -- see ACCOUNT SAFETY above
STACK="${STACK:-mount-hosmer-twin}"

# Calgary: the closest AWS region to Fernie, BC, and Canadian data residency.
#
# Two things to know about ca-west-1. It is a newer region, so (a) ECS Fargate for
# Linux IS supported, but FARGATE_SPOT is not guaranteed -- if `stack` fails with a
# capacity-provider error, re-run it as:
#
#     CAPACITY=FARGATE bash deploy/aws/deploy.sh 3
#
# and expect ~$39/month of Fargate instead of ~$12. And (b) Canadian regions price
# slightly above us-west-2, so the monthly estimates run maybe 5-10% higher.
REGION="${REGION:-ca-west-1}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

AWS=(aws --profile "$PROFILE" --region "$REGION")
PLATFORM=linux/amd64                   # Fargate is x86_64; MUST cross-compile on Apple Silicon

bake_tag() {
  python3 -c "import json;print(json.load(open('runtime/baked/meta.json'))['generated_at_utc'][:19].replace(':','').replace('-','').replace('T','-'))"
}

account_id() { "${AWS[@]}" sts get-caller-identity --query Account --output text; }
registry()   { echo "$(account_id).dkr.ecr.${REGION}.amazonaws.com"; }

guard_account() {
  echo "--- deploying as ---"
  "${AWS[@]}" sts get-caller-identity --output table
  echo
  echo "profile: $PROFILE   region: $REGION   stack: $STACK"
  read -r -p "Is this the groupavalanche4@gmail.com account (NOT the personal one)? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
}

# --- 1. ECR repositories -----------------------------------------------------
step_1_ecr() {
  guard_account

  # Service-linked roles. On a BRAND NEW account these do not exist, and ECS/ELB
  # normally create them the first time you use the console -- so driving
  # CloudFormation directly on a fresh account fails with a confusing
  # "Unable to assume the service linked role" on the cluster. Creating them up
  # front is idempotent; "has been taken" just means it already exists.
  for svc in ecs.amazonaws.com elasticloadbalancing.amazonaws.com; do
    "${AWS[@]}" iam create-service-linked-role --aws-service-name "$svc" >/dev/null 2>&1 \
      && echo "  iam: service-linked role for $svc created" \
      || echo "  iam: service-linked role for $svc already present"
  done

  for name in assess assistant frontend; do
    "${AWS[@]}" ecr describe-repositories --repository-names "twin/$name" >/dev/null 2>&1 \
      || "${AWS[@]}" ecr create-repository --repository-name "twin/$name" \
           --image-scanning-configuration scanOnPush=false >/dev/null
    echo "  ecr: twin/$name ready"
  done
  "${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$(registry)"
  echo "OK: registries ready and docker authenticated."
}

# --- 2. Build and push all three images --------------------------------------
step_2_push() {
  local reg tag; reg="$(registry)"; tag="bake-$(bake_tag)"

  echo ">>> assess (~900 MB -- this is the slow one)"
  docker build --platform "$PLATFORM" --target assess \
    -t "$reg/twin/assess:$tag" -t "$reg/twin/assess:latest" -f Dockerfile.backend .
  docker push "$reg/twin/assess:$tag"
  docker push "$reg/twin/assess:latest"

  echo ">>> assistant"
  docker build --platform "$PLATFORM" --target assistant \
    -t "$reg/twin/assistant:latest" -f Dockerfile.backend .
  docker push "$reg/twin/assistant:latest"

  echo ">>> frontend"
  # Empty base URLs => the client emits RELATIVE paths (/api/...), which the ALB
  # routes to the right service. Same origin, so no CORS. This is why the frontend
  # image does not need to know the load balancer's hostname at build time -- and
  # therefore does not have to be rebuilt when the stack is recreated.
  docker build --platform "$PLATFORM" \
    --build-arg "NEXT_PUBLIC_API_BASE_URL=" \
    --build-arg "NEXT_PUBLIC_ASSISTANT_BASE_URL=" \
    -t "$reg/twin/frontend:latest" ./frontend
  docker push "$reg/twin/frontend:latest"

  echo "OK: pushed. assess tag = $tag"
}

# --- 3. Create/update the stack ----------------------------------------------
step_3_stack() {
  guard_account
  local reg tag; reg="$(registry)"; tag="bake-$(bake_tag)"
  if ! "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK" \
    --template-file deploy/aws/infra.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      "AssessImage=$reg/twin/assess:$tag" \
      "AssistantImage=$reg/twin/assistant:latest" \
      "FrontendImage=$reg/twin/frontend:latest" \
      "OllamaUrl=${TUNNEL_URL:-}" \
      "CapacityProvider=${CAPACITY:-FARGATE_SPOT}" \
      "DesiredCount=${DESIRED:-1}"
  then
    echo
    echo "=== deploy failed -- most recent failure reasons ==="
    # CloudFormation's own error is usually just "stack rolled back"; the useful
    # detail is in the events.
    "${AWS[@]}" cloudformation describe-stack-events --stack-name "$STACK" \
      --query "StackEvents[?ResourceStatus=='CREATE_FAILED'||ResourceStatus=='UPDATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
      --output table 2>/dev/null | head -30 || true
    echo
    echo "If the reason mentions a capacity provider, FARGATE_SPOT is not available"
    echo "in ${REGION}. Re-run on on-demand Fargate (costs more, always works):"
    echo "    CAPACITY=FARGATE bash deploy/aws/deploy.sh 3"
    return 1
  fi
  step_urls
}

step_urls() {
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
}

# --- 4. Point the deployed assistant at the current tunnel -------------------
# Quick-tunnel hostnames change on every restart, so this is re-run often. It only
# touches the OllamaUrl parameter; the other images stay as they are.
step_4_set_tunnel() {
  local url="${1:?Usage: $0 set-tunnel https://<host>.trycloudflare.com}"
  curl -sf "${url}/api/tags" >/dev/null || { echo "That URL is not serving Ollama. Is the tunnel up?"; exit 1; }
  "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK" --template-file deploy/aws/infra.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "OllamaUrl=$url" \
    --no-fail-on-empty-changeset
  echo "OK: assistant now calls $url"
}

# --- Cost controls -----------------------------------------------------------
# Parking the stack stops ALL Fargate billing. The ALB (~$16/mo) keeps running
# because deleting it would change the URL; use `destroy` to stop that too.
step_stop()  { "${AWS[@]}" cloudformation deploy --stack-name "$STACK" \
                 --template-file deploy/aws/infra.yaml --capabilities CAPABILITY_IAM \
                 --parameter-overrides DesiredCount=0 --no-fail-on-empty-changeset
               echo "Parked: 0 tasks. Only the ALB is still billing."; }

step_start() { "${AWS[@]}" cloudformation deploy --stack-name "$STACK" \
                 --template-file deploy/aws/infra.yaml --capabilities CAPABILITY_IAM \
                 --parameter-overrides DesiredCount=1 --no-fail-on-empty-changeset
               echo "Running: 1 task per service."; }

step_status() {
  "${AWS[@]}" ecs list-services --cluster "${STACK}-cluster" --output text 2>/dev/null | tr '\t' '\n' | grep -o '[^/]*$' || true
  "${AWS[@]}" ecs describe-services --cluster "${STACK}-cluster" \
    --services assess assistant frontend 2>/dev/null \
    --query "services[].[serviceName,runningCount,desiredCount,status]" --output table 2>/dev/null || echo "(stack not up yet)"
}

step_logs() {
  local svc="${1:-assess}"
  "${AWS[@]}" logs tail "/ecs/${STACK}/${svc}" --since 15m --follow
}

# Deletes EVERYTHING in the stack: ALB, tasks, VPC, roles, log groups. ECR repos and
# their images survive on purpose, so a redeploy does not re-upload 900 MB.
step_destroy() {
  guard_account
  read -r -p "Delete stack '$STACK' and all its resources? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "Aborted."; exit 1; }
  "${AWS[@]}" cloudformation delete-stack --stack-name "$STACK"
  echo "Deleting. Watch with: $0 status"
}

case "${1:-}" in
  1|ecr)         step_1_ecr ;;
  2|push)        step_2_push ;;
  3|stack)       step_3_stack ;;
  set-tunnel)    shift; step_4_set_tunnel "$@" ;;
  urls)          step_urls ;;
  status)        step_status ;;
  logs)          shift; step_logs "$@" ;;
  stop)          step_stop ;;
  start)         step_start ;;
  destroy)       step_destroy ;;
  *) sed -n '1,40p' "${BASH_SOURCE[0]}"
     echo "Usage: $0 {1|2|3|set-tunnel <url>|urls|status|logs [svc]|stop|start|destroy}" ;;
esac
