# Baseline

> **Still empty. This is the last open item in phase 1, and it needs one rented
> GPU hour and no code.**

The acceptance criterion is:

> A developer gets a working, governed, callable endpoint **faster through Keel
> than by running vLLM themselves.**

Unmeasured, that claim is unfalsifiable — which is a worse position than being
slower. Our competitor is the developer going around us, and they already have
cluster access.

## Order matters

Do the manual run **first**, on a fresh box. Once Keel has provisioned
anything, the container image and the weights are cached on the node and a
clean cold number cannot be recovered without rebuilding the machine.

---

## 0 · Prepare the box (~10 min, not timed)

Rent one L4 or A10 (24 GB is ample for both models). Then, on the box:

```bash
ACCELERATOR=l4 ./deploy/gpu-node/setup.sh
```

It installs k3s, the NVIDIA container toolkit and device plugin, labels and
taints the node, and refuses to finish unless `nvidia.com/gpu` is genuinely
allocatable. Copy the kubeconfig back as it instructs.

**Record what it prints about compute capability.** Below 8.0 there is no
bfloat16 and vLLM downcasts *silently* (ADR-0007), so the catalog entry must
say `dtype: float16` or you are benchmarking different numerics than you think.

## 1 · The manual run — start the stopwatch

Deploy `Qwen/Qwen2.5-7B-Instruct` by hand: write the manifests, apply them,
expose it, and call it. Stop when `curl` returns a completion.

**Use the 7B, not the 0.5B fixture.** Weight-load time dominates time-to-ready,
so a 0.5B produces a real number that says nothing about whether the
node-local cache (ADR-0004) earns its keep. Stage 7 measured a 0.5B at 130s
total, of which only 24.7s was weights — the rest was fixed engine startup.

Then delete it, clear the image and weight caches, and do it again warm.

| Step | Cold | Warm |
|------|------|------|
| Find the right image and engine flags | | |
| Write the manifests | | |
| Weights download | | |
| Scheduling | | |
| Weight load into VRAM | | |
| Expose and authenticate it | | |
| **Total wall clock** | | |

## 2 · The Keel run

```bash
export KUBECONFIG=$PWD/gpu.kubeconfig
make rbac monitoring gateway
make catalog seed
make provisioner &          # in another shell

curl -X POST localhost:8080/v1/deployments \
  -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' \
  -d '{"team":"platform","name":"bench","model":"qwen2.5-7b-instruct","allow_preview":true}'
```

Time from that request to a `ready` status with a working completion.

| | Cold | Warm |
|---|------|------|
| Manual | | |
| Keel | | |
| **Difference** | | |

## 3 · The decision inventory

Every question answered by hand in step 1 is either a schema column or a
hardcoded default in the paved road. This is the more valuable half of the
exercise — list them as you hit them.

- [ ] which image and tag
- [ ] which `--max-model-len` for this card
- [ ] which `--gpu-memory-utilization`
- [ ] `--dtype`, and whether the card forced it
- [ ] how weights reached the node
- [ ] how the endpoint was exposed and secured
- [ ] …

## 4 · What else this hour is worth capturing

Only real hardware can answer these, and all three are still open:

- **A real OOM.** Stage 7's attempt never reached one — vLLM rejected the
  config first. Find the message by asking for a context that fits on disk but
  not in VRAM, and check it against `classify_log()`.
- **DCGM.** `deployment_metrics.gpu_util_pct` is deliberately `null` because
  the exporter needs a real GPU. Deploy `deploy/monitoring/dcgm.yaml` and
  confirm the series appears.
- **Whether the timings resemble stage 7's.** That was a T4 in Colab, on
  Colab's disk and network. If a real L4 differs sharply, the estimator's
  eventual model has to account for the environment, not just the accelerator.

## Honesty note

Nothing in Keel has run against a real vLLM inside a cluster. Every green test
uses a fake runtime, and a node that only *claims* to have a GPU. Four separate
times now, something has worked here and broken on a clean machine. **Expect
this hour to find things.** That is what it is for.
