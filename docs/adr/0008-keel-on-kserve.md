# 0008 — Keel as the platform layer on top of KServe

**Status:** PROPOSED · **Date:** 2026-09-27 · decided by the spike below

## The gap this closes

The very first plan for this project said *"KServe is substrate, not
competitor."* The Azure-native revision dropped it, and the Kubernetes revision
never brought it back: `control/render/` has rendered raw Deployments, Services
and ScaledObjects ever since.

ADR-0002 rejected "a CRD plus an operator" because *writing* one means owning
two reconcile loops that can disagree. **It never considered *adopting* an
existing operator** — and adopting the expensive parts is the principle the
whole design rests on. KServe (v0.21.0 at the time of writing, CNCF) is exactly
that operator, and in the one layer where it overlaps Keel it is far ahead:
canary rollouts, a `ServingRuntime` abstraction, `InferenceGraph`, llm-d,
prefix-cache-aware routing and KV-cache offloading. Keel has none of these.

## Proposal

KServe replaces `control/render/` — the 231 lines that turn a deployment into
pods. Everything above it stays: the request API, quota, the governed catalog,
keys, the LiteLLM gateway, cost and usage, the event log, the reconciler's
intent-versus-reality check. Keel stops competing with KServe and becomes the
thing KServe explicitly does not attempt — letting many teams run models safely.

## Division of reconciliation

The risk ADR-0002 named is real, so it is settled here rather than discovered:

| Loop | Owns | Compares |
|---|---|---|
| KServe controller | InferenceService → Deployment, Service, pods | the CR spec against the pods |
| Keel reconciler | deployment row → InferenceService | the DB row against the **CR spec**, never the Deployment |

Keel stops looking at the Deployment KServe generates. A manual edit to that
Deployment is KServe's to undo; a manual edit to the InferenceService is Keel's
to report.

## The spike

**Scope.** `InferenceService` with a custom predictor container (the fake
runtime), in **`Standard`** mode (v0.21's name for what was `RawDeployment` —
no Knative, no Istio), on the local k3d cluster, on a branch.

**Explicitly not tested:** `LLMInferenceService`, llm-d, KV offloading,
prefill/decode separation. Those need Gateway API components and real GPUs. The
spike validates the **integration shape**, not the production CRD — a pass here
is necessary, not sufficient.

### Success criteria — all must hold

1. **I1 survives.** No `Ingress` exists in any `keel-inf-*` namespace, asserted
   directly — not merely that Services are ClusterIP, which stays green even
   when an Ingress exposes the backend. v0.21 defaults
   `disableIngressCreation: false`, so this fails unless deliberately fixed.
2. **I4 survives, proven as the real ServiceAccount.** `test_rbac.py` shows the
   provisioner can create `inferenceservices.serving.kserve.io` inside a team
   namespace and **nowhere else**. Running as admin locally hid exactly this
   class of bug in stage 5.
3. **Labels reach the pods.** `keel.io/deployment-id`, `keel.io/team` and
   `app.kubernetes.io/managed-by` appear on the generated pods, because
   Prometheus relabeling, `pods_for()`, orphan detection and DCGM attribution
   all key on them.
4. **The upstream is derived, not assumed.** The gateway route points at the
   Service KServe actually created, on the port it actually listens on — not
   the hardcoded `{name}...:8000`.
5. **The existing e2e suite passes against it:** request → record → CR → pod →
   gateway → a completion, plus metrics scraping and teardown.
6. **It fits.** The KServe controller and cert-manager run alongside LiteLLM,
   Postgres and Prometheus in the 8 GB Docker VM without evictions.

### Kill criteria — any one ends it

- Meeting criterion 1 or 2 requires patching KServe rather than configuring it.
- Criterion 3 is impossible in `Standard` mode.
- The resident footprint forces a larger VM than a developer laptop.
- The rewrite reaches beyond `render/`, the status classifier and the
  reconciler's comparison — i.e. the seam turns out not to be a seam.

## Revisit if

`LLMInferenceService` becomes testable without GPUs, or a real deployment needs
something KServe's LLM features provide that the rendered-manifest path cannot.

## If it loses

The branch is deleted, this ADR is marked **Rejected** with the reason, and
ADR-0002's amendment below still stands: the option was evaluated, not missed.
