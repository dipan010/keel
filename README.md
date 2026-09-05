# Keel

A control plane for model inference on Kubernetes. A developer asks for a
model; Keel places it on a GPU, fronts it with a gateway, issues a stable
OpenAI-compatible URL and a key, meters what it costs, and tears it down.

Substrate-neutral: k3s on a laptop, a rented GPU box, or any managed cluster.
No cloud provider in the architecture. `Keel` is a working codename.

**Architecture:** https://claude.ai/code/artifact/fae767c7-1da9-4428-a50f-773f2739392c

## Stack

| Layer      | Choice     | Build or adopt |
|------------|------------|----------------|
| Substrate  | Kubernetes | adopt          |
| Runtime    | vLLM       | adopt          |
| Gateway    | LiteLLM    | adopt          |
| State + queue | PostgreSQL | adopt       |
| Autoscaling | HPA, KEDA | adopt          |
| Control API, Provisioner, Reconciler | Python 3.13 | **build** |

Three services and a schema. Everything expensive is adopted.

## Layout

```
control/
  api/          only writer of desired state; no cluster access at all
  provisioner/  queue worker: render -> server-side apply -> smoke test
  reconciler/   desired vs actual, every 60s; read-only by construction
  metrics/      hourly rollup into deployment_metrics; cost computed here
  render/       deployment row -> manifests. The seam for a second backend.
  domain/       state machine and rules. NO infrastructure imports (enforced).
  migrations/   0001_init.sql ships mode_t from day one
catalog/        model definitions as YAML, added by PR
bench/          load harness + the fake runtime
deploy/         helm chart, local cluster, bootstrap
docs/adr/       decisions, each with a "revisit if"
docs/baseline.md   ** fill this in before writing provisioning code **
```

## Getting started

```bash
make dev        # deps into .venv
make db         # postgres in docker
make migrate    # apply schema
make catalog    # sync catalog/*.yaml into the database
make seed       # create the 'platform' dev team
make test       # unit + integration
make api        # control api on :8080
```

```bash
curl -X POST localhost:8080/v1/deployments \
  -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' \
  -d '{"team":"platform","name":"chat",
       "model":"qwen2.5-0.5b-instruct","allow_preview":true}'
```

Unit tests need nothing. Integration tests skip cleanly without a migrated
database, so `make test` works on a laptop with nothing running.

```bash
make cluster     # local k3d cluster
make rbac        # service accounts and roles
make monitoring  # prometheus, with the relabeling the design depends on
make fake-gpu    # a node that claims a GPU it does not have
make gateway     # litellm + its own database
make fake        # build the fake vLLM and load it in
make gateway-fwd # port-forward :4000, in another shell
make provisioner
```

`make fake` builds a stand-in for vLLM that answers the OpenAI API with canned
responses and plausible metrics, and reports 503 on `/health` for its first
seconds so the `scheduling -> loading -> ready` path is real. Combined with
`gpu_count: 0` (CPU serving), the entire control path runs on a cluster with no
GPU -- without which every integration test costs GPU-hours and nobody runs
them.

`make fake-gpu` advertises `nvidia.com/gpu` on a node through Kubernetes'
extended-resource mechanism. That validates **placement** -- node selector,
toleration, resource limits, quota, and the unschedulable path -- with no
hardware. It says nothing about whether a model runs on a GPU.

`make ci` runs exactly what CI runs. Authentication is OIDC: set
`KEEL_OIDC_ISSUER` (and `KEEL_OIDC_AUDIENCE`) to verify against a real IdP,
or `KEEL_DEV_AUTH=1` locally. With neither, every request gets a 501 that
says so -- a permissive default is how a service ships unauthenticated.

Four test tiers, each skipping cleanly when its dependency is absent:
`test-unit` needs nothing, the database suite needs Postgres, the cluster suite
needs an API server, and `test_full_pipeline.py` needs all three plus a gateway
port-forward. That last one is the phase 1 acceptance test: one request in, one
working endpoint out, verified by a real completion.

## The bar

> A developer gets a working, governed, callable endpoint **faster through Keel
> than by running vLLM themselves.**

Our competitor is not another vendor, it is the developer going around us. They
already have cluster access. Measure the manual baseline first --
`docs/baseline.md` -- and keep measuring ours.

## Invariants

Things a code review should reject a change for violating.

1. The endpoint URL is always the gateway's, never a backend's.
2. The gateway never calls the control plane synchronously to serve a request.
3. The database is desired state; the cluster is actual state.
4. Nothing is created outside `keel-inf-*` and `keel-gateway`.
5. Orphaned objects are alerted on, never auto-deleted.
   (1, 4 and 5 are enforced by the API server, not by convention --
   `deploy/rbac.yaml`, asserted in `control/tests/test_rbac.py`.)
6. Every state transition writes an event. Status is a projection of that log.
7. An issued key's secret exists in exactly one HTTP response and nowhere
   else -- not in the database, not in a log, not recoverable by listing.

## Phase 1

In: one model on one GPU, lane B, end to end. Develop against
`qwen2.5-0.5b-instruct` (~1GB, seconds to load); reserve the 7B for the
`docs/baseline.md` timing run.
Out: UI, lane A, lane C, autoscaling, the reconciler, the estimator, Azure.
