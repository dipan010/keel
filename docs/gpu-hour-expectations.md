# What to expect from the GPU hour

Written **before** the run, on 2026-09-23, so it can be checked afterwards
rather than rationalised. Every prediction below is falsifiable. Mark each one
right or wrong when you come back — a prediction nobody scores is just a
feeling.

Context: at the time of writing, **nothing in Keel has ever run against a real
vLLM on a real GPU.** All 174 tests use a fake runtime on a node that only
*claims* to have a GPU (`deploy/kind/fake-gpu.sh`). Four separate times in this
project, something worked on the development machine and broke on a clean one.

Cost: an L4 or A10 at roughly $0.40–0.80/hour, about 90 minutes of work.
Call it **$1–2**.

---

## 1 · Deliverables — what you should have afterwards

| Output | Where it lands |
|---|---|
| Manual vs Keel timings, cold and warm | `docs/baseline.md` |
| The decision inventory | `docs/baseline.md` §3 |
| Real `gpu_util_pct` instead of `NULL` | `deployment_metrics` |
| A real OOM message, checked against the classifier | `control/provisioner/status.py` |
| Whether vLLM's prefix caching is on by default | settles an open assumption |
| First real vLLM startup breakdown | compare against stage 7's T4 numbers |

---

## 2 · Predictions, ranked by confidence

### P1 — Very likely: the pod gets no GPU because nothing sets `runtimeClassName`

On k3s the NVIDIA container runtime is exposed as a **RuntimeClass that pods
must opt into**. `control/render/manifests.py` does not set
`runtimeClassName`, so the pod will schedule happily — `nvidia.com/gpu` is
allocatable, the device plugin is content — and vLLM will then start, find no
CUDA device, and die.

**The symptom looks nothing like a scheduling problem**, which is what makes it
expensive. Expect a CUDA error from inside vLLM, not a Pending pod.

- **Right if:** the first lane-B deployment fails with a CUDA/no-device error.
- **Wrong if:** it starts and serves. (Possible — some distributions set the
  nvidia runtime as the default rather than as an opt-in class.)

### P2 — Likely: DCGM utilisation does not attribute to a deployment

`deploy/monitoring/prometheus.yaml` relabels DCGM's `exported_pod` into
`deployment_id` with the regex `vllm-(.+?)-[a-z0-9]+-[a-z0-9]+`. That assumes a
pod-name shape **never observed** — DCGM's label has never been seen in this
project.

- **Right if:** `gpu_util_pct` stays NULL while DCGM metrics clearly exist in
  Prometheus.
- **Wrong if:** the column populates on the first rollup.

### P3 — Possible: `setup.sh` configures a containerd that k3s ignores

The script runs `nvidia-ctk runtime configure --runtime=containerd`, which
writes `/etc/containerd/config.toml`. **k3s does not read that file** — it
generates its own config from a template under
`/var/lib/rancher/k3s/agent/etc/containerd/`.

- **Right if:** the device plugin never advertises a GPU and `setup.sh` exits
  at its allocatable check. (That check exists precisely so this fails loudly
  rather than three steps later.)
- **Wrong if:** k3s auto-detects the runtime, which recent versions do.

### P4 — Probably fine: 7B fits on a 24 GB card

Qwen2.5-7B at bf16 is roughly 15 GB of weights. At
`gpu-memory-utilization: 0.90` on 24 GB that is a ~21.6 GB budget, leaving
~6 GB of KV cache — with GQA, on the order of **3 concurrent requests at the
full 32k context**. Tight but workable. An L4 is Ada (8.9) and an A10 is
Ampere (8.6), so the entry's `dtype: bfloat16` is genuinely supported on both.

- **Wrong if:** it OOMs at load, in which case lower `max-model-len` first,
  not `gpu-memory-utilization`.
- **A T4 would fail this entry** — compute 7.5, no bf16, and vLLM downcasts
  silently (ADR-0007) rather than erroring.

### P5 — Startup will be dominated by engine init, not weights

Stage 7 measured a 0.5B on a T4: 130 s total, of which only **24.7 s** was
weight loading — 46 s was engine init (20 s of it compilation) and 9 s CUDA
graph capture. That fixed cost does not shrink with the model.

- **Right if:** the 7B's engine-init and graph-capture times are within roughly
  the same range as the 0.5B's, and weight loading is the only part that grows.
- **Wrong if:** total time scales with model size, which would mean ADR-0004's
  amendment is still too generous to the weights cache.

---

## 3 · The uncomfortable possibility: Keel may lose

The manual path is one `kubectl apply` of a manifest an engineer already has.
Keel adds an API round trip, a queue, a job claim, a smoke test and a gateway
registration — seconds the manual path never pays.

**If Keel is slower, that is information, not failure.** It would mean the
value is in the decisions a developer does not make — accelerator, engine
flags, networking, quota, key issuance, teardown — rather than in elapsed
seconds, and that the acceptance criterion in the architecture doc is measuring
the wrong thing.

Recorded here in advance so that outcome gets reported rather than explained
away. If it happens, the honest response is to restate the criterion, not to
tune the stopwatch.

---

## 4 · What this hour cannot settle

- multi-GPU and tensor parallelism
- behaviour under concurrent load — every test so far is single-request
- whether lane C's scale-to-zero cold start is tolerable in practice
- anything about a second team: every run has used exactly one

---

## 5 · Scoring

Fill this in afterwards.

| # | Prediction | Right? | What actually happened |
|---|------------|--------|------------------------|
| P1 | No GPU without `runtimeClassName` | | |
| P2 | DCGM does not attribute | | |
| P3 | k3s ignores the containerd config | | |
| P4 | 7B fits at 0.90 / 32k | | |
| P5 | Engine init dominates | | |
| — | Keel beats manual on wall clock | | |
