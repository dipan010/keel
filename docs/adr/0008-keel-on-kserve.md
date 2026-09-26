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
| 5 | Existing e2e suite passes | **PASS** | *Corrected wording:* most of the suite is backend-independent or calls the manifest renderer directly, so "200 passed" overstated it. The tests that actually exercise the KServe path are `test_full_pipeline` (request → InferenceService → pod → LiteLLM → completion, plus keys and revocation), the reconciler e2e tests under `KEEL_RENDER_BACKEND=kserve`, and the 14 tests in `test_kserve_spike.py`. All pass. Manifests backend unaffected: 203 passed |
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

## Review checks — run after the first grading

A review of the first grading found the PASSes rested on narrower evidence than
claimed. Six further checks, all on live clusters:

1. **KServe injects `cpu: 1` / `memory: 2Gi` into any resource field left
   unset** — as both request and limit. A real vLLM streams weights through host
   memory; **every real deployment would have been OOMKilled during weight
   load.** The fake uses ~100 MB, so no test could notice; only inspecting the
   pod found it. KServe *does* honour explicit values — but fills each omitted
   field individually (asking for a CPU request alone still got a `cpu: 1`
   limit injected, which would throttle vLLM's API server). **So every field
   must be explicit.** Pinned in `test_explicit_resources_survive...`.
2. **GPU placement through KServe** had never met a scheduler — every spike test
   used `gpu_count: 0`. Verified: the pod lands on the GPU node with selector,
   toleration and `nvidia.com/gpu: 1` intact, counted against the team quota.
3. **Standard mode creates an HPA scaling on CPU at 80%.** The spike only
   looked inert because every test used `replicas_max: 1`. For a GPU-bound
   engine CPU is the wrong signal. The manifest backend never autoscaled lane B;
   the KServe renderer silently added it. Now pinned min == max for lane B.
   KServe's `scaleMetric` offers cpu, memory, concurrency and rps — no queue
   depth — so real autoscaling is a separate decision (likely KEDA on
   `vllm:num_requests_waiting`).
4. **Metrics attribution on a KServe pod**, previously inferred from labels, is
   now observed: scrape target up, `vllm:request_success_total` carrying
   `deployment_id`.
5. **One spike test could not fail** the way it claimed: it accepted UPDATING
   *or* DEGRADED, so broken drift detection would pass. It now waits for the
   edit to settle and pins UPDATING with reason `drift`.
6. **From zero, `install.sh` was broken — and dishonest about it.** It waited
   for one of KServe's three webhook servers. On a clean cluster all twelve
   `LLMInferenceServiceConfig`s failed because the LLM webhook was not up yet,
   and the script labelled those thirteen failures with the "known defect"
   message written for a *different* single error. Only a clean-cluster run
   found it. Fixed: waits for all three, retries, and tolerates only the known
   error by its exact text. Proven on a scratch cluster from nothing: 35/35
   spike and RBAC tests. A CI job now repeats this on Linux for every push to
   `spike/**`.

**Also found, not KServe-specific:** a newly started client pod is refused by
the target namespace's NetworkPolicy for about a second, until k3s's policy
controller syncs its IP (proved with a control run without the policy: 8/8
first-attempt successes). It explains stage 8's unexplained partial success
rates. The gateway is protected in practice by LiteLLM's 20-second readiness
delay — worth knowing before anyone shortens it.

## Recommendation

**Accept — with four requirements that did not exist before the review:**

1. **Explicit resources in the catalog.** Host CPU and memory, request and
   limit, for every model — a schema change. Without it the first real model
   on KServe is OOMKilled.
2. **An autoscaling decision.** Lane B stays fixed-replica until one is made
   on a real signal.
3. **Migrate existing manifest-rendered deployments** — they are invisible to a
   reconciler that lists InferenceServices. Free today, since nothing real is
   deployed.
4. **Delete the manifest renderer** rather than keep both paths.

All six criteria pass without patching KServe, and the seam held — slightly
wider than claimed. The review checks mostly moved risk from "unknown" to
"named requirement", which is what a spike is for.
