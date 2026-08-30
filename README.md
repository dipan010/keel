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

`make cluster` brings up a local k3d cluster and `make fake` builds a fake vLLM
that answers the OpenAI API with canned responses and plausible metrics. That
pair makes the whole control path testable in CI without a GPU -- without it
every integration test costs GPU-hours and nobody runs them.

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
6. Every state transition writes an event. Status is a projection of that log.

## Phase 1

In: one model on one GPU, lane B, end to end. Develop against
`qwen2.5-0.5b-instruct` (~1GB, seconds to load); reserve the 7B for the
`docs/baseline.md` timing run.
Out: UI, lane A, lane C, autoscaling, the reconciler, the estimator, Azure.
