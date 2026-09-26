# 0002 — Server-side apply, not Helm, not an operator

**Status:** accepted · **Date:** 2026-08-31

## Decision

The Provisioner renders manifests itself and server-side-applies them under a
fixed field manager (`keel-provisioner`), with object names derived
deterministically from `deployment_id` (`vllm-{id}`).

## Why

Three options existed:

- **CRD plus an operator** is the idiomatic Kubernetes answer, but it means
  writing a controller and then owning two reconcile loops that can disagree
  with each other.
- **Helm releases** add a release-state store that becomes a third source of
  truth alongside our database and the cluster.
- **Server-side apply** gives idempotent convergence for free. A redelivered
  job re-applies the same objects under the same name and converges rather than
  duplicating, and the API server reports exactly which fields we own.

The deterministic object name is the whole idempotency mechanism, and it is why
the queue can redeliver safely.

## Revisit if

Teams want to declare deployments in their own GitOps repos, at which point a
CRD becomes the natural interface and the Control API becomes a second writer.

## Amended 2026-09-27 — an option this ADR never weighed

The three options above were: write a CRD and operator, use Helm, or
server-side apply. The rejection of the first rests on *writing* an operator
and owning two reconcile loops that can disagree.

**Adopting an existing operator was never considered**, and that objection does
not hold with the same force when the operator is someone else's, mature and
maintained — adopting the expensive parts is the principle the whole design
rests on. KServe is that operator. ADR-0008 evaluates it.

Server-side apply remains how Keel writes whatever `render/` produces; the open
question is only what `render/` should produce.
