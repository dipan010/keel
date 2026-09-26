# 0008 — Keel as the platform layer on top of KServe

**Status:** PROPOSED — spike passed all six criteria, awaiting a decision · **Date:** 2026-09-27

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

---

## Spike results — 2026-09-27

Graded against the criteria above, which were written before the spike began.
KServe v0.21.0, cert-manager v1.21.2, Standard mode, fake runtime, local k3d.
Setup is reproducible: `deploy/kserve/install.sh`.

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | No Ingress in `keel-inf-*` | **PASS**, by configuration | v0.21 defaults `disableIngressCreation: false`; set to true. Asserted directly — no Ingress, no HTTPRoute |
| 2 | Provisioner may create InferenceServices only in team namespaces | **PASS**, after a fix | Before the RBAC change the real ServiceAccount got **no** in a team namespace. As admin, everything had worked. Now yes-in-team, no-elsewhere, pinned in `test_rbac.py` |
| 3 | Keel labels reach the pods | **PASS** | All three labels propagate to the generated Deployment and pods; `pods_for()` selects by them |
| 4 | Upstream derived, not assumed | **PASS**, after a fix | KServe creates `{name}-predictor` on port 80 → 8000. The hardcoded `{name}...:8000` would have pointed the gateway at nothing. Now read from `status.address.url` |
| 5 | Existing e2e suite passes | **PASS** | KServe backend: 200 passed, 3 skipped (manifest-internals tests, each with a KServe equivalent). Manifests backend: 203 passed. Includes the full pipeline to a real completion through LiteLLM, keys and revocation |
| 6 | Fits in the 8 GB VM | **PASS** | Peak 4.62 GiB of 7.65 across nodes during the full suite. Zero evictions, zero OOM kills. KServe + cert-manager cost ~1.3 GiB |

### Kill criteria

- *Required patching KServe rather than configuring it* — **no.** Every fix was
  configuration: the mode, the ingress switch, the domain template, RBAC.
- *Labels impossible in Standard mode* — **no.** They propagate.
- *Needs a larger VM* — **no.** ~3 GiB headroom.
- *Rewrite reaches beyond `render/`, the status classifier and the reconciler's
  comparison* — **not triggered, but the seam is wider than this ADR claimed.**
  The status classifier needed no change at all (labels propagate, so pod
  classification works as before). But three Provisioner call sites also depend
  on the backend — where the upstream address comes from, what teardown
  deletes, and a fail-fast check — about a dozen lines, now isolated in
  `control/render/backend.py`. Recorded rather than graded generously.

### The division of reconciliation, observed

The ADR asserted *"a manual edit to KServe's Deployment is KServe's to undo."*
Tested, not assumed:

- A manual edit to the generated Deployment's **container args** was reverted
  by KServe's controller within 3 seconds. Args were chosen deliberately: an
  HPA can undo a replica change, but only the controller rewrites the pod
  template.
- A manual edit to the **InferenceService** is faithfully applied by KServe and
  reported by Keel's reconciler as drift — without writing to the cluster (I5).
- The reconciler reports **no false drift** against a live InferenceService, so
  KServe's webhook defaulting does not trip `material()`.

### Found by the spike, beyond the criteria

1. **KServe computes a hostname even with ingress disabled**, as a single DNS
   label `{name}-predictor-{namespace}`. Keel's full-UUID names overflow 63
   characters and KServe refuses the resource outright. Fixed by configuring
   `domainTemplate` to put name and namespace in separate labels — which keeps
   ADR-0002's deterministic naming intact.
2. **A rejected resource produces no pods**, which the pod classifier reads as
   `scheduling` for the full 30-minute timeout. The Provisioner now reads
   KServe's `ReconcileFailed` condition and fails in seconds.
3. **v0.21.0's own release ships an `LLMInferenceServiceConfig` its webhook
   rejects** — a template field the type lacks. LLM-only; out of scope here,
   but worth knowing before adopting that CRD.
4. **v0.21.0's `kserve.yaml` references a `kserve` namespace it never
   creates.** Handled in the install script.
5. **Availability is coarser.** In Standard mode the InferenceService offers a
   Ready condition, not a ready-replica count, so the reconciler treats it as
   all-or-none.
6. **Found on the way, fixed on `main`:** the gateway registered
   `/models/weights` while vLLM served the HuggingFace id, so a real vLLM would
   have rejected every request. Not KServe-specific; the fake runtime answered
   to any name, which is why nothing noticed.

### Not validated — do not read the pass as covering these

- `LLMInferenceService`, llm-d, KV offloading, prefill/decode — need real GPUs
- mounted weights (`weights_uri`): the KServe renderer **refuses** rather than
  guess how the storage initializer attaches to a custom container
- lane C / scale-to-zero in Standard mode
- anything on a real GPU

### Migration requirement if accepted

Under the KServe backend the reconciler lists **InferenceServices**. Raw
Deployments left by the manifest backend would become invisible to it — silent
orphans, the one outcome I5 exists to prevent. Adoption therefore needs either
a migration that re-provisions every existing deployment, or a transition
period in which the reconciler lists both kinds. Nothing is deployed for real
yet, so today the cost is zero; it only grows.

## Recommendation

**Accept.** All six criteria pass without patching KServe; the seam held, if
slightly wider than claimed. On acceptance the manifest renderer should be
deleted rather than kept beside KServe — parallel paths rot.
