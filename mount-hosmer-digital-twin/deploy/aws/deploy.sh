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
# A machine may have several AWS profiles configured, and deploying to the wrong
# account is easy and annoying to undo. So: every command below passes --profile
# EXPLICITLY, defaults to a dedicated project profile, and `guard_account` prints
# the account ID and requires confirmation before anything is created.
#
# Do not add a `[default]` profile. Without one, a mistyped or unset profile fails
# with "config profile could not be found" instead of silently using someone
# else's account -- that failure mode is a feature, keep it.
#
# ---------------------------------------------------------------------------
# BEFORE YOU RUN THIS -- the parts only you can do
# ---------------------------------------------------------------------------
#   1. An AWS account with billing enabled. (AWS ties an account to one email
#      address, so a separate project account needs a separate address.)
#   2. In the AWS Console: IAM -> Users -> Create user -> attach
#      AdministratorAccess -> Security credentials -> Create access key -> CLI.
#   3. aws configure --profile avalanche
#        (paste the key + secret, region ca-west-1, output json)
#
# Then: bash deploy/aws/deploy.sh <step>
# ---------------------------------------------------------------------------

set -euo pipefail

PROFILE="${PROFILE:-avalanche}"        # NOT `talha` -- see ACCOUNT SAFETY above
STACK="${STACK:-mount-hosmer-twin}"
DOMAIN="${DOMAIN:-avalanche.gotlost.xyz}"   # the QR-code-facing hostname; see `cert`/`dns`

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
PYTHON_BIN="${PYTHON_BIN:-python3}"
RELEASE_STATE_DIR="$ROOT/runtime/deployment"
RELEASE_TAG_FILE="$RELEASE_STATE_DIR/latest-release-tag.txt"

bake_tag() {
  "$PYTHON_BIN" -c "import json;print(json.load(open('runtime/baked/meta.json'))['generated_at_utc'][:19].replace(':','').replace('-','').replace('T','-'))"
}

bake_sha256() {
  "$PYTHON_BIN" -c "import json;print(json.load(open('runtime/baked/meta.json'))['identity']['bake_sha256'])"
}

release_tag() {
  local sha; sha="$(bake_sha256)"
  echo "release-$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:12}"
}

account_id() { "${AWS[@]}" sts get-caller-identity --query Account --output text; }
registry()   { echo "$(account_id).dkr.ecr.${REGION}.amazonaws.com"; }

image_ref() {
  local service="$1" tag="$2" reg digest
  reg="$(registry)"
  digest=$("${AWS[@]}" ecr describe-images --repository-name "twin/$service" \
    --image-ids "imageTag=$tag" --query "imageDetails[0].imageDigest" --output text)
  [[ "$digest" == sha256:* ]] || { echo "No ECR digest for twin/$service:$tag" >&2; return 1; }
  echo "$reg/twin/$service@$digest"
}

stack_parameter() {
  local name="$1"
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='$name']|[0].ParameterValue" --output text
}

stack_resource_id() {
  local logical_id="$1"
  "${AWS[@]}" cloudformation describe-stack-resource --stack-name "$STACK" \
    --logical-resource-id "$logical_id" --query "StackResourceDetail.PhysicalResourceId" --output text
}

wait_services_stable() {
  local assess assistant frontend
  assess="$(stack_resource_id AssessService)"
  assistant="$(stack_resource_id AssistantService)"
  frontend="$(stack_resource_id FrontendService)"
  "${AWS[@]}" ecs wait services-stable --cluster "${STACK}-cluster" \
    --services "$assess" "$assistant" "$frontend"
}

# ACM certificates are region-scoped like everything else here, and requested once
# outside CloudFormation so destroying/recreating the stack never re-requests or
# re-validates one. Empty/"None" output means no certificate exists yet for DOMAIN.
cert_arn() {
  "${AWS[@]}" acm list-certificates --certificate-statuses PENDING_VALIDATION ISSUED \
    --query "CertificateSummaryList[?DomainName=='${DOMAIN}'].CertificateArn | [0]" \
    --output text
}

guard_account() {
  echo "--- deploying as ---"
  "${AWS[@]}" sts get-caller-identity --output table
  echo
  echo "profile: $PROFILE   region: $REGION   stack: $STACK"
  read -r -p "Is that the correct project account (NOT a personal one)? [y/N] " reply
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

# Validate the exact locally built containers before anything is pushed. The
# browser gate runs again after the ALB rollout, where routing matches production.
# The QA container names are globals on purpose. A RETURN trap stays installed for
# the CALLER too, so a cleanup function closing over the gate's `local` variables
# fires a second time in a scope where they no longer exist -- which under `set -u`
# printed "assess_name: unbound variable" after every otherwise-successful build.
QA_CONTAINER_NAMES=()

cleanup_release_containers() {
  [[ ${#QA_CONTAINER_NAMES[@]} -gt 0 ]] || return 0
  docker rm -f "${QA_CONTAINER_NAMES[@]}" >/dev/null 2>&1 || true
}

local_container_gate() {
  local assess_image="$1" assistant_image="$2" frontend_image="$3" expected_sha="$4"
  local assess_name="${STACK}-qa-assess" assistant_name="${STACK}-qa-assistant" frontend_name="${STACK}-qa-frontend"
  local assess_port="${QA_ASSESS_PORT:-18080}" assistant_port="${QA_ASSISTANT_PORT:-18081}" frontend_port="${QA_FRONTEND_PORT:-13000}"

  QA_CONTAINER_NAMES=("$assess_name" "$assistant_name" "$frontend_name")
  trap cleanup_release_containers RETURN
  cleanup_release_containers

  docker run --rm -d --name "$assess_name" -e PORT=8080 -p "127.0.0.1:${assess_port}:8080" "$assess_image" >/dev/null
  docker run --rm -d --name "$assistant_name" -e PORT=8080 -p "127.0.0.1:${assistant_port}:8080" "$assistant_image" >/dev/null
  docker run --rm -d --name "$frontend_name" -p "127.0.0.1:${frontend_port}:3000" "$frontend_image" >/dev/null

  local url attempt
  for url in "http://127.0.0.1:${assess_port}/api/health" \
             "http://127.0.0.1:${assistant_port}/api/health" \
             "http://127.0.0.1:${frontend_port}/"; do
    for attempt in $(seq 1 60); do
      curl -fsS "$url" >/dev/null 2>&1 && break
      sleep 1
    done
    curl -fsS "$url" >/dev/null
  done

  "$PYTHON_BIN" - "http://127.0.0.1:${assess_port}" "$expected_sha" <<'PY'
import json, sys, urllib.request
base, expected = sys.argv[1:]
def get(path):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        assert response.status == 200
        return json.load(response)
health = get('/api/health')
meta = get('/api/twin/meta')
assert health['baked'] is True and health['bake_sha256'] == expected
assert meta['schema'] == 'stage3-baked-v2'
assert meta['identity']['bake_sha256'] == expected
assert meta['imagery']['visual_context_only'] is True
request = urllib.request.Request(
    base + '/api/assess',
    data=json.dumps({'new_snow_cm': 40, 'wind_speed_kmh': 45, 'wind_direction_deg': 225,
                     'release_size': 'medium', 'simulation_mode': 'fast'}).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
with urllib.request.urlopen(request, timeout=120) as response:
    assessment = json.load(response)
assert assessment['model']['bake_sha256'] == expected
assert assessment['conditions']['provenance'] in {
    'user_supplied', 'user_supplied_simple_scenario', 'structured_observation_scenario'}
assert assessment['is_probability'] is False and assessment['is_operational_forecast'] is False
assert 'NOT an operational avalanche forecast' in assessment['disclaimer']

# The composite index and its decomposition ship with this exact image.
area = assessment['area_hazard_index']
assert area is None or 0 <= area <= 100, area
components = assessment['hazard_components']
assert components['is_probability'] is False and components['is_calibrated'] is False
for zone in assessment['zones']:
    parts = zone['hazard_components']
    # Exposure may only ever raise a zone's index; it can never lower one.
    assert zone['hazard_index'] >= parts['terrain_and_reach_index'] - 1e-9, zone['zone_id']
    assert parts['exposure_uplift_points'] >= 0, zone['zone_id']

# Exposure is optional, but when the bake carries it the served vector must be
# there with its ODbL attribution -- an image that lost the layer must not ship.
exposure_meta = meta.get('exposure')
if exposure_meta is not None:
    assert exposure_meta['used_in_release_model'] is False
    assert exposure_meta['used_in_runout_model'] is False
    collection = get('/api/twin/exposure')
    assert collection['type'] == 'FeatureCollection'
    assert 'OpenStreetMap' in collection['attribution']
    assert 'ODbL' in collection['licence']
    assert collection['features'], 'exposure layer served no features'
PY
  echo "OK: exact local release containers passed health, identity, imagery, and assessment gates."
}

# --- 2. Build, validate, and push all three immutable release images ----------
step_2_push() {
  local reg tag sha assess_image assistant_image frontend_image
  reg="$(registry)"
  tag="${RELEASE_TAG:-$(release_tag)}"
  sha="$(bake_sha256)"
  [[ "$tag" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Invalid release tag: $tag" >&2; exit 1; }
  "$PYTHON_BIN" -m app.check_bake
  "${AWS[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$reg"

  assess_image="$reg/twin/assess:$tag"
  assistant_image="$reg/twin/assistant:$tag"
  frontend_image="$reg/twin/frontend:$tag"

  echo ">>> assess (~900 MB -- this is the slow one)"
  docker build --platform "$PLATFORM" --target assess \
    -t "$assess_image" -f Dockerfile.backend .

  echo ">>> assistant"
  docker build --platform "$PLATFORM" --target assistant \
    -t "$assistant_image" -f Dockerfile.backend .

  echo ">>> frontend"
  # Empty base URLs => the client emits RELATIVE paths (/api/...), which the ALB
  # routes to the right service. Same origin, so no CORS. This is why the frontend
  # image does not need to know the load balancer's hostname at build time -- and
  # therefore does not have to be rebuilt when the stack is recreated.
  docker build --platform "$PLATFORM" \
    --build-arg "NEXT_PUBLIC_API_BASE_URL=" \
    --build-arg "NEXT_PUBLIC_ASSISTANT_BASE_URL=" \
    -t "$frontend_image" ./frontend

  local_container_gate "$assess_image" "$assistant_image" "$frontend_image" "$sha"

  docker push "$assess_image"
  docker push "$assistant_image"
  docker push "$frontend_image"

  mkdir -p "$RELEASE_STATE_DIR"
  printf '%s\n' "$tag" > "$RELEASE_TAG_FILE.tmp"
  mv "$RELEASE_TAG_FILE.tmp" "$RELEASE_TAG_FILE"
  echo "OK: validated and pushed immutable release tag $tag (bake $sha)."
}

# --- HTTPS certificate (one-time, decoupled from the stack) ------------------
# Requested via the CLI instead of CloudFormation so destroying/recreating the
# stack never re-requests or re-validates it. Idempotent: reuses whatever already
# exists for DOMAIN.
step_cert() {
  local arn; arn="$(cert_arn)"
  if [[ -z "$arn" || "$arn" == "None" ]]; then
    arn=$("${AWS[@]}" acm request-certificate --domain-name "$DOMAIN" \
            --validation-method DNS --query CertificateArn --output text)
    echo "  acm: requested a certificate for $DOMAIN"
    echo "  waiting a few seconds for AWS to generate the validation record..."
    sleep 10
  else
    echo "  acm: a certificate for $DOMAIN already exists ($arn)"
  fi

  local name value root host
  name=$("${AWS[@]}" acm describe-certificate --certificate-arn "$arn" \
           --query "Certificate.DomainValidationOptions[0].ResourceRecord.Name" --output text)
  value=$("${AWS[@]}" acm describe-certificate --certificate-arn "$arn" \
            --query "Certificate.DomainValidationOptions[0].ResourceRecord.Value" --output text)
  if [[ -z "$name" || "$name" == "None" ]]; then
    echo "  !! validation record not generated yet -- re-run: $0 cert"
    exit 1
  fi
  # ACM returns the full FQDN; Namecheap's "Host" field wants it relative to the
  # registered zone (gotlost.xyz), e.g. "_ab12cd34.avalanche" not the full name.
  root="${DOMAIN#*.}"
  host="${name%.}"; host="${host%."$root"}"

  echo
  echo "=== add this record in Namecheap (gotlost.xyz -> Advanced DNS), then run: $0 cert-wait ==="
  echo "  Type:  CNAME"
  echo "  Host:  $host"
  echo "  Value: $value"
  echo "  TTL:   Automatic"
}

step_cert_wait() {
  local arn; arn="$(cert_arn)"
  [[ -n "$arn" && "$arn" != "None" ]] || { echo "No certificate requested yet. Run: $0 cert"; exit 1; }
  echo "Waiting for ACM to validate $DOMAIN (returns once the CNAME above resolves; can take a few minutes)..."
  "${AWS[@]}" acm wait certificate-validated --certificate-arn "$arn"
  echo "OK: certificate issued. $arn"
}

# The one command to re-run after any full stack rebuild: prints the current ALB
# hostname and the exact CNAME to (re)point at it in Namecheap.
step_dns() {
  local alb
  alb=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='AlbDnsName']|[0].OutputValue" --output text 2>/dev/null)
  [[ -n "$alb" && "$alb" != "None" ]] || { echo "Stack has no AlbDnsName output yet -- deploy it first: $0 3"; exit 1; }
  echo "=== add/update this record in Namecheap (gotlost.xyz -> Advanced DNS) ==="
  echo "  Type:  CNAME"
  echo "  Host:  ${DOMAIN%%.*}"
  echo "  Value: $alb"
  echo "  TTL:   Automatic"
}

# --- 3. Create/update the stack ----------------------------------------------
step_3_stack() {
  guard_account
  local tag cert sha assess_image assistant_image frontend_image
  local previous_assess previous_assistant previous_frontend ollama_url
  [[ -f "$RELEASE_TAG_FILE" ]] || {
    echo "No validated release state. Run '$0 2' before rolling the live services." >&2
    exit 1
  }
  tag="${RELEASE_TAG:-$(<"$RELEASE_TAG_FILE")}"
  [[ "$tag" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "Invalid release tag: $tag" >&2; exit 1; }
  sha="$(bake_sha256)"
  [[ "$tag" == *"${sha:0:12}"* ]] || {
    echo "Validated release tag $tag is not bound to the current bake $sha." >&2
    exit 1
  }
  assess_image="$(image_ref assess "$tag")"
  assistant_image="$(image_ref assistant "$tag")"
  frontend_image="$(image_ref frontend "$tag")"
  previous_assess="$(stack_parameter AssessImage)"
  previous_assistant="$(stack_parameter AssistantImage)"
  previous_frontend="$(stack_parameter FrontendImage)"
  previous_expected_bake="$(stack_parameter ExpectedBakeSha256)"
  if [[ ! "$previous_expected_bake" =~ ^[0-9a-f]{64}$ ]]; then
    previous_expected_bake="$sha"
  fi
  ollama_url="${TUNNEL_URL:-$(stack_parameter OllamaUrl)}"
  cert="$(cert_arn)"
  if [[ -z "$cert" || "$cert" == "None" ]]; then
    echo "No ACM certificate found for $DOMAIN."
    echo "Run '$0 cert', add the printed Namecheap record, then '$0 cert-wait'."
    exit 1
  fi
  if ! "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK" \
    --template-file deploy/aws/infra.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      "AssessImage=$assess_image" \
      "AssistantImage=$assistant_image" \
      "FrontendImage=$frontend_image" \
      "OllamaUrl=$ollama_url" \
      "CapacityProvider=${CAPACITY:-FARGATE}" \
      "DesiredCount=${DESIRED:-1}" \
      "ExpectedBakeSha256=$sha" \
      "Domain=$DOMAIN" \
      "CertificateArn=$cert"
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

  # CloudFormation waits for the update, the ECS circuit breaker rolls back task
  # startup failures, and this explicit waiter verifies all three generated
  # service names rather than assuming short names that do not exist.
  wait_services_stable

  if ! "$PYTHON_BIN" deploy/verify_live.py "https://$DOMAIN" --expected-bake-sha256 "$sha" \
     || ! PLAYWRIGHT_BASE_URL="https://$DOMAIN" npm --prefix frontend run smoke
  then
    echo "Post-rollout health gate failed; restoring the three previous image identities." >&2
    "${AWS[@]}" cloudformation deploy \
      --stack-name "$STACK" --template-file deploy/aws/infra.yaml \
      --capabilities CAPABILITY_IAM \
      --parameter-overrides \
        "AssessImage=$previous_assess" \
        "AssistantImage=$previous_assistant" \
        "FrontendImage=$previous_frontend" \
        "ExpectedBakeSha256=$previous_expected_bake" \
      --no-fail-on-empty-changeset
    wait_services_stable
    return 1
  fi
  echo "OK: immutable release $tag is stable and passed HTTPS/API/imagery/assessment/browser gates."
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
  local assess assistant frontend
  assess="$(stack_resource_id AssessService 2>/dev/null)" || { echo "(stack not up yet)"; return; }
  assistant="$(stack_resource_id AssistantService)"
  frontend="$(stack_resource_id FrontendService)"
  "${AWS[@]}" ecs describe-services --cluster "${STACK}-cluster" \
    --services "$assess" "$assistant" "$frontend" \
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
  cert)          step_cert ;;
  cert-wait)     step_cert_wait ;;
  dns)           step_dns ;;
  urls)          step_urls ;;
  status)        step_status ;;
  logs)          shift; step_logs "$@" ;;
  stop)          step_stop ;;
  start)         step_start ;;
  destroy)       step_destroy ;;
  *) sed -n '1,40p' "${BASH_SOURCE[0]}"
     echo "Usage: $0 {1|2|3|set-tunnel <url>|cert|cert-wait|dns|urls|status|logs [svc]|stop|start|destroy}" ;;
esac
