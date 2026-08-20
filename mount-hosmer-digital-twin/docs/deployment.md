# Deployment — the microservice split and AWS ECS

**Read this before changing anything under `mount-hosmer-digital-twin/deploy/`, or
before deploying.** It is written for someone (or some agent) arriving cold.

The app runs **two ways from the same code**. Neither is a fork; which processes
answer which requests is a deployment decision.

| Shape | Entrypoint(s) | Used by |
|---|---|---|
| **One process** | `app.main` | Local dev, optional launcher, `docker compose` |
| **Three services** | `app.main_assess`, `app.main_assistant`, frontend | AWS ECS Fargate |

Local development instructions live in the repository `README.md`. If you only
work locally, you can ignore this document entirely.

---

## 1. The services

```
                        internet
                            │
              ┌─────────────▼──────────────┐
              │  Application Load Balancer │   one hostname, path-routed
              └──┬──────────┬──────────┬───┘
      /          │   /api/assistant/*  │   /api/*
   ┌─────────────▼──┐  ┌────▼────────┐ │ ┌─▼──────────────┐
   │   frontend     │  │  assistant  │ │ │    assess      │
   └────────────────┘  └──────┬──────┘ └─┴────────▲───────┘
                              └───── HTTP ─────────┘
                              │
                    Cloudflare Tunnel
                              │
                  an operator's machine: Ollama
```

| Service | Entrypoint | Routes | Fargate size | Image |
|---|---|---|---|---|
| **assess** | `app.main_assess` | `/api/assess`, `/api/twin/meta`, `/api/twin/tiles/…`, `/api/twin/imagery/…`, `/api/health` | 1 vCPU / 4 GB | ~550 MB (carries the bake) |
| **assistant** | `app.main_assistant` | `/api/assistant/{chat,explain,health}`, `/api/health` | 0.25 vCPU / 512 MB | ~260 MB |
| **frontend** | Next.js standalone | everything not `/api/*` | 0.25 vCPU / 512 MB | ~200 MB |
| **bakeworker** | `app.main_bakeworker` | `/health`, `/probe`, `/bake/{id}` | 1 vCPU / 4 GB | `FROM bake` — geospatial stack only, no baked-terrain layer |

**bakeworker is not like the other three.** Its routes carry **no `/api/` prefix**
(`/health`, not `/api/health`), and unlike assess/assistant/frontend it is **never
registered with the ALB at all** — no target group, no listener rule, no public
route. The only caller is `assess`, over ECS Service Connect, entirely inside the
VPC. See "On-demand mountain uploads" below and I-F.

**Ollama is not deployed.** It runs on an operator's own machine and is reached
through a Cloudflare Tunnel. That is what removes the GPU from the bill.

### Load balancer routing

| Priority | Path pattern | Target |
|---|---|---|
| 10 | `/api/assistant/*` | assistant |
| 20 | `/api/*` | assess |
| default | everything else | frontend |

**Rule order is load-bearing.** `/api/assistant/*` must be evaluated before
`/api/*`, or every assistant call routes to assess. Priorities are set explicitly
in `infra.yaml`; do not renumber them casually.

Because all three sit on one hostname, the browser makes **same-origin** requests
and there is **no CORS anywhere**. The frontend is built with *empty*
`NEXT_PUBLIC_API_BASE_URL` / `NEXT_PUBLIC_ASSISTANT_BASE_URL` so its client emits
relative paths (`/api/assess`). This also means the frontend image does not embed
the load balancer hostname, and so does not need rebuilding when the stack is
recreated.

### On-demand mountain uploads (bakeworker)

`assess` ships from the always-slim `runtime` Docker target and deliberately
carries no geospatial stack — `import app.main` must not pull in rasterio, and
`Dockerfile.backend`'s `runtime` target asserts exactly that at build time (I-B).
Validating and baking a user-uploaded mountain needs rasterio and pyproj, so that
work cannot run inside `assess` the way it does for the local launcher.
`backend/app/mountain_jobs.py` resolves this by branching on
`AVALANCHE_BAKE_WORKER_URL`: unset, `upload_available()` / `run_probe()` /
`run_bake()` run a local subprocess exactly as before; set, they dispatch the
identical work over HTTP to **bakeworker** — a 4th service built from the `bake`
Docker target, the only image in the fleet with rasterio, pyproj, PIL and pyyaml.
There is one implementation of "run the probe" and "run the bake"; the HTTP path
is a thin network front door onto it, never a second copy.

**Sizing.** bakeworker runs `app.bake`'s identical computation a real bake runs,
so `infra.yaml`'s `BakeWorkerTaskDefinition` gives it the same measured 1 vCPU /
4 GB as `assess` (I-C) — not `assistant`'s much smaller 0.25 vCPU / 512 MB, which
is sized for a thin HTTP relay to Ollama, not a raster computation.

**Shared storage.** `runtime/mountains/` (the upload registry, plus each
mountain's `source/`, `pack.json`, and `baked/` — see `backend/app/mountains.py`)
moved off `assess`'s ephemeral task disk onto an EFS filesystem
(`MountainsFileSystem` in `infra.yaml`), mounted at the same `/runtime/mountains`
path in both the `assess` and `bakeworker` task definitions. This also
incidentally fixes a latent bug: if `AssessService`'s `DesiredCount` is ever
raised above 1, each task today would have its own disconnected
registry/uploads; EFS gives every task, of either service, the same view. Mount
Hosmer's own reviewed bake is unaffected — it still ships baked into the assess
image at `/runtime/baked` (I-D), which is deliberately *not* on EFS.

**Turning it on for a demo.** bakeworker's task count is a separate stack
parameter from the other three services' shared `DesiredCount`, because live
mountain uploads are a rarely-used demo feature, not core traffic. `deploy.sh`'s
`step_3_stack` reads it from the `BAKEWORKER_DESIRED` environment variable
(default `0`) into the `BakeWorkerDesiredCount` parameter, the same pattern
`DESIRED` already uses for the other three:

```bash
bash deploy/aws/deploy.sh 3                        # BakeWorkerDesiredCount=0 (default): parked
BAKEWORKER_DESIRED=1 bash deploy/aws/deploy.sh 3   # brings up a live task; uploads become available
BAKEWORKER_DESIRED=0 bash deploy/aws/deploy.sh 3   # park it again when the demo is over
```

`GET /api/mountains` answers either way: it health-checks bakeworker on every
call (`upload_available()`, 5 s timeout) and reports `upload_available: false`
with a human-readable reason while parked, rather than hiding the upload feature
or hanging. See I-F for the reachability invariant this all rests on.

---

## 2. Invariants — break these and you break the design

**I-A. `assess` is the only service that computes a hazard number.**
Therefore it is the only place `DISCLAIMER` has to be attached (in `app/assess.py`).
The assistant must never grow its own terrain or its own model. When a what-if
question needs an assessment, the assistant *calls assess* through
`app/assess_client.py`. This is what guarantees the language model can only narrate
numbers the deterministic model produced.

**I-B. `backend/app/api/__init__.py` must stay import-free.**
It used to `from app.api import errors, middleware, stage3`, which meant importing
*any* route module pulled in *all* of them — silently dragging the terrain reader
and runout engine into the assistant image. There is a build-time assertion in
`Dockerfile.backend` (the `assistant` target) **and** a test
(`tests/test_service_split.py::test_assistant_service_does_not_import_the_runout_engine`).
If either fails, something re-introduced an eager import.

**I-C. Size the assess task for the peak, not the average.**
One assessment peaks at **~1477 MB** resident on the 2400×2400 (5.8M cell) grid,
in both `fast` and `advanced` modes. At a 2 GB task the container was OOM-killed
mid-request (exit 137) and ECS restarted it — which presents to a user as
"assess just fails", with no error anywhere obvious. CPython does not return freed
arrays to the OS, so a warm container sits near its peak and the *second* request
starts high. It runs 1 vCPU / 4 GB.

**I-D. The baked terrain ships inside the assess image.**
`runtime/` is gitignored, so the bake exists only on the machine that ran it.
`.dockerignore` excludes `runtime/` with one scoped exception, `!runtime/baked/`.
The assess image remains tagged with the bake's `generated_at_utc`, while runtime
loading validates the bake schema, processing/configuration hash, layer checksums
and overall `bake_sha256`. `/api/health` reports both the timestamp and identity.
Consequence: **build the assess image on a machine that has run the current bake**.
A source-based cloud build would produce an image that reports `baked: false`.

**I-E. Build `--platform linux/amd64`.**
Fargate is x86_64. On Apple Silicon this cross-compiles, which also means a local
container runs under emulation — an assessment takes ~82 s locally versus ~9 s on
Fargate. That is emulation overhead, not a regression.

**I-F. bakeworker is the only service with the geospatial stack, and it is never
internet-reachable.**
rasterio, pyproj, PIL and pyyaml live in exactly one image (`Dockerfile.backend`'s
`bake`/`bakeworker` target); `assess`, `assistant` and `frontend` do not carry
them, and `assess` cannot fall back to running a bake itself if bakeworker is
unreachable — `mountain_jobs.upload_available()` reports that as "not available"
rather than attempting a local subprocess it has no interpreter for. bakeworker
has no ALB target group and no listener rule (see §1); the only path to it is
`assess`, over ECS Service Connect, entirely inside the VPC.

---

## 3. Prerequisites

| Tool | Install | Notes |
|---|---|---|
| Docker | Docker Desktop | Must be running |
| AWS CLI v2 | `brew install awscli` | |
| Ollama | https://ollama.com/download | Plus `ollama pull llama3.1:8b` (~4.7 GB) |
| cloudflared | `brew install cloudflared` | |

**AWS account setup** (once):

1. An AWS account with billing enabled.
2. An IAM user with `AdministratorAccess`, and an access key of type *CLI*.
3. Configure a **named profile** — never rely on a `[default]` profile:

   ```bash
   aws configure --profile avalanche      # region: ca-west-1, output: json
   ```

The scripts default to `PROFILE=avalanche` and `REGION=ca-west-1`, and pass
`--profile` explicitly on every call. Override with environment variables:

```bash
PROFILE=other REGION=us-west-2 STACK=my-stack bash deploy/aws/deploy.sh status
```

> If the named profile does not exist, the AWS CLI fails with
> *"The config profile could not be found"* rather than silently falling back to
> another profile. That is deliberate — keep it that way, and do not add a
> `[default]` profile.

---

## 4. Everyday use

All commands run from `mount-hosmer-digital-twin/`.

```bash
bash deploy/session.sh up       # Ollama + tunnel + AWS, wired together (~10 min)
bash deploy/session.sh status   # what is running, and what it costs
bash deploy/session.sh down     # tear it all down; billing stops (~5 min)
```

`up` does four things in order: starts Ollama, opens the Cloudflare tunnel,
captures its public URL, and deploys the AWS stack **with that URL already set** —
so the assistant is wired on first creation. It then waits for all three services
to answer before printing the app URL.

`down` deletes the CloudFormation stack (load balancer, tasks, VPC, IAM, logs) and
kills the tunnel. **Container images in ECR survive on purpose**, so bringing it
back up never re-uploads ~1 GB.

### Controlling the two halves separately

`session.sh` moves both halves at once. When you want them independent — stop
paying for AWS but leave the AI up, restart a dead tunnel without touching the
stack — use `twin.sh`, which delegates to the same scripts:

```bash
bash deploy/twin.sh web start | status | stop     # the AWS half (the one that costs money)
bash deploy/twin.sh ai  start | status | stop     # Ollama + tunnel on this Mac (free)
bash deploy/twin.sh status                        # both at once
```

Order matters on a cold start: `web start` bakes the current tunnel hostname into
the assistant, so start `ai` first and the wiring is automatic. Start them the
other way round — or restart the tunnel later, which always changes its random
hostname — and re-wire without redeploying anything else:

```bash
bash deploy/ollama-tunnel.sh set-url        # infers the running tunnel's URL
```

The two halves are separated because they **fail** separately: the app can be
perfectly healthy while the AI is unreachable. `ai status` does not just check that
processes are alive — it calls the tunnel from outside, which is the only check
that actually proves the deployed assistant can reach Ollama.

> The argument to `set-url` is the **`https://<random>.trycloudflare.com`** hostname —
> your Mac, exposed. It is not the app's `*.elb.amazonaws.com` URL; that is the
> deployment, and pointing the assistant at it tells it to look for Ollama inside
> the load balancer it already lives behind.

### The app URL changes on every `up`

The load balancer's DNS name is generated per-ALB. `session.sh status` prints the
current one. If a fixed URL is needed, park the stack instead of destroying it —
the ALB (and its hostname) survives, and only compute stops:

```bash
bash deploy/aws/deploy.sh stop     # DesiredCount=0; Fargate billing stops, ALB stays
bash deploy/aws/deploy.sh start    # DesiredCount=1; same URL, ~2 min to be ready
```

---

## 5. Custom domain & HTTPS

The load balancer's own hostname (`*.ca-west-1.elb.amazonaws.com`) is regenerated
— and changes — every time the stack is destroyed and rebuilt (`session.sh down`,
`deploy.sh destroy`). That is fine for development, but a QR code printed on a
physical poster needs a URL that **never changes**, even across a full teardown.

The fix is one Namecheap CNAME in front of the ALB: the QR code encodes
**`https://avalanche.gotlost.xyz`**, never the raw ALB hostname. If the stack is
ever destroyed and recreated, only that one record needs to be repointed — the
QR code itself never has to change.

HTTPS is **additive**, not a redirect: port 80 keeps working exactly as before
(including the assistant's internal `http://${LoadBalancer.DNSName}` call back
into assess — see `AVALANCHE_ASSESS_URL` in `infra.yaml`), and port 443 is a
second, independent listener serving the public domain.

The ACM certificate's lifecycle is decoupled from the CloudFormation stack, the
same way ECR images already are — requested once via the CLI, validated by one
DNS record, and its ARN is looked up dynamically by `deploy.sh` on every deploy.
Destroying and recreating the stack never re-requests or re-validates it.

### One-time setup

```bash
bash deploy/aws/deploy.sh cert         # requests the ACM certificate (idempotent)
#   -> add the printed CNAME (validation record) in Namecheap
bash deploy/aws/deploy.sh cert-wait    # blocks until ACM shows the cert ISSUED
bash deploy/session.sh up              # (or: bash deploy/aws/deploy.sh 3)
bash deploy/aws/deploy.sh dns          # prints the current ALB hostname
#   -> add/update the "avalanche" CNAME in Namecheap to point at it
```

Two separate Namecheap records are involved — do not confuse them:

| Record | Purpose | Changes when? |
|---|---|---|
| `_xxxx.avalanche` → ACM-provided value | Proves domain ownership to ACM | Once, ever (same cert survives stack rebuilds) |
| `avalanche` → the ALB hostname | Routes real traffic to the ALB | Every time the stack is destroyed and recreated |

### Recovery after a full stack rebuild

The certificate survives a rebuild untouched (it lives outside CloudFormation).
Only the second CNAME needs attention:

```bash
bash deploy/aws/deploy.sh dns    # prints the new ALB hostname
#   -> update the "avalanche" CNAME in Namecheap to the new value
```

DNS propagation is typically a few minutes; `curl -I https://avalanche.gotlost.xyz`
to confirm.

> **Do not run `session.sh down` / `deploy.sh destroy` while this is the live
> poster deployment.** `session.sh down` requires typing `DESTROY` to proceed for
> exactly this reason — it is the direct safeguard against an accidental teardown
> during the exhibit window.

---

## 6. Updating a deployed service

### After changing backend code

```bash
bash deploy/aws/deploy.sh 2     # rebuild + push all four images
bash deploy/aws/deploy.sh 3     # roll the services onto the new images
```

Step 2 validates the bake, builds all four images under one unique release tag
(assess, assistant, frontend, and bakeworker — a local-container gate runs
against all four, bakeworker's own `/health`), and only then pushes them. It
stores the validated tag under generated `runtime/deployment/`; mutable `latest`
tags are not used for a rollout. Docker layer caching keeps unchanged work fast,
and the assess image's baked-data layer is copied last.

Step 3 resolves every ECR tag to an immutable `repository@sha256:...` identity,
updates the existing stack, waits for the real CloudFormation-generated ECS
service names to stabilize (including bakeworker's, even while it is parked at
`BakeWorkerDesiredCount=0` — "stable" there just means the service itself
reconciled, not that a task is running), and runs the public
HTTPS/API/imagery/assessment and Playwright browser gates. ECS keeps the
previous task healthy during replacement and has its deployment circuit breaker
configured to roll back startup failures. If a post-rollout functional or
browser gate fails, the script restores all four prior image identities and
waits for them to become stable before returning failure.

### After changing the frontend

Same two steps. Remember `NEXT_PUBLIC_*` is inlined at **build** time — changing
those values means rebuilding the image, not restarting the task.

### After re-running the bake

A release tag includes the first 12 hexadecimal characters of the validated bake
identity. `deploy.sh 3` refuses to roll it if the current local bake no longer
matches. Verify independently:

```bash
curl -s "$(bash deploy/session.sh status | awk '/^AWS:/{print $2}')/api/health"
# -> bake_generated_at and bake_sha256 should match the new bake
```

### After changing infrastructure (`infra.yaml`)

```bash
aws --profile avalanche --region ca-west-1 \
  cloudformation validate-template --template-body file://deploy/aws/infra.yaml
bash deploy/aws/deploy.sh 3
```

CloudFormation computes a changeset and updates only what differs. Changing a task
definition (CPU, memory, environment) rolls the service onto a new task revision
with no downtime. The three target groups also have inexpensive CloudWatch
`UnHealthyHostCount` alarms. They intentionally have no notification action, so
adding an email/SMS destination remains an explicit operator decision.

The stack's 128 MiB Lambda probe runs once per hour and checks the public HTTPS
frontend safety copy, exact `ExpectedBakeSha256`, distinct centre terrain/imagery
PNG bytes, and a real deterministic assessment with its identity and disclaimer.
Its log retention is seven days and a CloudWatch `Errors` alarm records failures;
there is no unapproved notification destination. `deploy.sh 3` updates the expected
bake automatically and restores the prior value when it performs a functional
rollback.

The scheduled `.github/workflows/live-smoke.yml` check adds the browser-level
test: both imagery tiles must return 200 and rendered satellite and hillshade
canvas screenshots must differ. It becomes active when this workflow is present
on the repository's default branch. Update its `EXPECTED_BAKE_SHA256` whenever a
reviewed bake is deliberately rolled.

### Changing the tunnel URL only

Quick-tunnel hostnames change on every restart. This updates just that parameter:

```bash
bash deploy/aws/deploy.sh set-tunnel https://<host>.trycloudflare.com
```

### Adding an API endpoint

1. Add the function (`app/assess.py`, `avycore.hazard`, or `avycore.assistant`, as appropriate).
2. Wire the route into the **correct** router — `api/terrain.py`, `api/assess.py`,
   or `api/assistant.py`. It is served by whichever service mounts that router.
3. If the path does not already fall under an existing ALB rule, add a listener
   rule in `infra.yaml` (watch the priority ordering).
4. Add the type + fetch helper in `frontend/src/lib/twin.ts`. Assistant calls use
   `ASSISTANT_API`; everything else uses `API`.
5. Add a test.

> `/api/health` matches `/api/*`, so it routes to **assess**. That is why the
> assistant additionally serves `/api/assistant/health` — otherwise its health
> would only be visible to the load balancer's internal check.

### Debugging a deployed service

```bash
bash deploy/aws/deploy.sh logs assess       # or assistant / frontend
bash deploy/aws/deploy.sh status

# why did a task die?
aws --profile avalanche --region ca-west-1 ecs describe-tasks \
  --cluster mount-hosmer-twin-cluster --tasks <task-arn> \
  --query "tasks[].{stopped:stoppedReason,reason:containers[0].reason,exit:containers[0].exitCode}"
```

`exitCode: 137` with `OutOfMemoryError` means the task ran out of memory — raise
`Memory` on that task definition in `infra.yaml` (see I-C).

---

## 7. Gotchas actually hit during setup

| Symptom | Cause | Fix |
|---|---|---|
| `Template format error: 'Description' length is greater than 1024` | CloudFormation caps `Description` at 1024 chars | Keep prose in YAML comments |
| `Unable to assume the service linked role` on the ECS cluster | A brand-new AWS account has no ECS/ELB service-linked roles | `deploy.sh 1` now creates them idempotently |
| Stack cannot be recreated after a failure | A `ROLLBACK_COMPLETE` stack must be deleted before its name is reused | `aws cloudformation delete-stack …`, then redeploy |
| Assessment "just fails" in the browser | Task OOM-killed and silently restarted | See I-C |
| `advanced` simulation returns 504 while logs show success | ALB idle timeout defaults to 60 s | Set to 300 s via `idle_timeout.timeout_seconds` |
| Map tiles 404 | Tiles outside the 12×12 km AOI legitimately do not exist | Not a bug — MapLibre renders them empty. Centre tiles return 200 |
| `status` reports a deleted stack as live | `describe-stacks` returns `DELETE_COMPLETE` stacks with their old outputs | Gate on `StackStatus`, not just outputs |
| Task fails to start with an image-pull timeout | No public IP and no NAT gateway | `AssignPublicIp: ENABLED` is required (see §7) |
| Assistant 503s, or `set-url` says "That URL is not serving Ollama" — while `localhost:11434` answers 200 | Ollama ≥0.28 rejects an unrecognised **`Host`** header with a bare 403 (DNS-rebinding protection). cloudflared forwards the original `Host` (`<random>.trycloudflare.com`). `OLLAMA_ORIGINS` does **not** cover this — that governs `Origin`, a different check | Run the tunnel with `--http-host-header localhost` (both tunnel scripts now do). Verify with `bash deploy/twin.sh ai status` |

---

## 8. Cost model

Two deliberate decisions in `infra.yaml`:

**Public subnets, no NAT gateway.** Fargate tasks need outbound internet to pull
from ECR and to reach the Ollama tunnel. The conventional pattern (private subnets
+ NAT gateway) costs ~$32/month on its own — more than every task combined. Instead
the tasks sit in public subnets with public IPs, protected by a security group that
accepts inbound traffic **only** from the load balancer. Nothing reaches a task
directly from the internet.

**`CapacityProvider` is a parameter, and defaults to `FARGATE` (on-demand).**
`FARGATE_SPOT` is ~70% cheaper but interruptible (ECS restarts the task
automatically, typically within a minute or two) — fine for local dev, not for
the poster deployment sitting behind a QR code that must stay up unattended.
Deploy with `CAPACITY=FARGATE_SPOT` for cheap, interruption-tolerant dev/testing.

Rough figures (ca-west-1, on-demand):

| | |
|---|---|
| Everything running, always-on (the poster deployment) | ~$0.09/hour, **~$65–70/month** |
| Parked (`deploy.sh stop`) — ALB only | ~$0.025/hour |
| Torn down (`session.sh down`) | **~$0.10/month** (ECR images only) — **do not do this to the live poster deployment**, see §5 |

**bakeworker adds ~$0 at the parked baseline above.** `BakeWorkerDesiredCount`
defaults to `0`, so no bakeworker task exists — no vCPU, no memory, nothing
billed by ECS/Fargate for it. (The `MountainsFileSystem` EFS volume it shares
with `assess` is provisioned either way, but at this app's data volumes — a
handful of small rasters, `MAX_UPLOADED_MOUNTAINS = 3` — that is cents/month on
EFS Standard, the same "negligible" call `infra.yaml`'s own comment makes for
not bothering with an Infrequent-Access lifecycle policy.)

**Turning it on for a demo** (`BAKEWORKER_DESIRED=1`) adds one 1 vCPU / 4 GB
Fargate task — the same size as `assess`, on the same on-demand basis the
~$0.09/hour figure above is built from. ca-west-1 on-demand Fargate (Linux/x86)
is $0.04456 per vCPU-hour and $0.004865 per GB-hour, so:

```
1 vCPU × $0.04456/vCPU-hr  = $0.04456/hr
4 GB   × $0.004865/GB-hr   = $0.01946/hr
                              --------
                              $0.064/hr  (~$47/month if left running continuously)
```

In practice it should not run continuously — start it for the demo window and
park it again (`BAKEWORKER_DESIRED=0 bash deploy/aws/deploy.sh 3`) the same way
`deploy.sh stop`/`start` already parks the other three.

Set a budget alarm. It is free and it is the real safety net:

```bash
aws --profile avalanche --region us-east-1 budgets create-budget \
  --account-id <your-account-id> \
  --budget file://budget.json --notifications-with-subscribers file://notifications.json
```

(Budgets is a global service and only answers on the `us-east-1` endpoint.)

---

## 9. Verifying a teardown

`session.sh status` should report `not deployed`. To check independently:

```bash
A=(aws --profile avalanche --region ca-west-1)
"${A[@]}" cloudformation list-stacks --query "StackSummaries[?StackName=='mount-hosmer-twin'].[StackStatus]"
"${A[@]}" elbv2 describe-load-balancers --query "LoadBalancers[].LoadBalancerName"
"${A[@]}" ecs list-clusters --query "clusterArns"
"${A[@]}" ec2 describe-nat-gateways --filter Name=state,Values=available --query "NatGateways[].NatGatewayId"
```

All should be empty except a `DELETE_COMPLETE` stack entry. ECR repositories
remaining is expected and intentional.

In the AWS console, **set the region to Canada West (Calgary) `ca-west-1`** —
everything is region-scoped and the wrong region shows nothing. Relevant consoles:
CloudFormation (tick *View deleted stacks*), ECS, EC2 → Load Balancers, ECR, VPC,
CloudWatch → Log groups. Billing → Budgets is global, not regional. Console cost
data lags roughly 24 hours.

---

## 10. Files

| Path | What it is |
|---|---|
| `deploy/session.sh` | `up` / `down` / `status` — the whole system, both halves at once |
| `deploy/twin.sh` | `web`/`ai` × `start`/`status`/`stop` — the two halves, separately |
| `deploy/aws/deploy.sh` | ECR + build/push + stack; also `check`, `cert`, `cert-wait`, `dns`, `stop`, `start`, `logs`, `destroy`, `app-url` |
| `deploy/aws/infra.yaml` | The entire AWS stack (VPC, ALB, ECS, IAM, logs) |
| `deploy/verify_live.py` | Dependency-free public health gate for HTTPS, bake identity, imagery and assessment success |
| `deploy/ollama-tunnel.sh` | Ollama + Cloudflare Tunnel on the local machine; `up`/`status`/`down`/`set-url` |
| `backend/app/assess_client.py` | How the assistant reaches assess (in-process or HTTP) |
| `backend/app/service.py` | Shared FastAPI app factory |
| `backend/app/main{,_assess,_assistant,_bakeworker}.py` | The four entrypoints |
| `backend/app/api/{terrain,assess,assistant,deps}.py` | Routes, one module per service |
| `backend/app/mountain_jobs.py` | Local-subprocess vs. bakeworker-HTTP dispatch for the upload probe/bake (I-F) |
| `tests/test_service_split.py` | Pins the split: route surfaces, import isolation, the assess client |
| `tests/test_bakeworker_dispatch.py` | bakeworker's own routes, the HTTP-dispatch helpers against a real bakeworker process, and an upload→bake→assess integration smoke test |

---

## 11. Known limitations

- **Assessment memory.** ~1477 MB peak for a 12×12 km AOI is large; the model
  holds several full-grid float64 arrays simultaneously. Reducing it is an open
  optimisation. See `docs/limitations.md`.
- **HTTPS only on the custom domain.** `https://avalanche.gotlost.xyz` (§5) has a
  valid cert. The raw ALB hostname still only serves plain HTTP on port 80 (kept
  working deliberately, for the assistant's internal call into assess).
- **The tunnel is public and unauthenticated.** The hostname is random and
  unguessable, but anyone who learns it can send prompts to the operator's machine.
  Bring it up for a session and take it down afterwards; do not leave it running
  for days. For always-on use, a *named* tunnel with Cloudflare Access is the
  correct mechanism.
- **The assistant depends on an operator's machine being awake.** The map and
  hazard model are unaffected and stay up; only the AI degrades, to a clean 503.
