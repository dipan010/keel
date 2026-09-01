# Colab probe

Answers the vLLM questions the local build cannot, on a free T4, without a
cluster and without spending anything.

## Run it

1. https://colab.research.google.com → **File → Upload notebook** →
   `vllm_probe.ipynb`
2. **Runtime → Change runtime type → T4 GPU**
3. Run all. Roughly 10–15 minutes, most of it installing vLLM.
4. Copy the JSON from the last cell into `bench/colab/results-<date>.json`.

## What it answers, and what changes as a result

| Question | Consumes the answer |
|---|---|
| vLLM's real Prometheus metric names | the KEDA query in `render/manifests.py:_scaled_object`, and `deployment_metrics` |
| Do our catalog engine args work | `catalog/*.yaml` |
| What an OOM actually says | the advice text in `provisioner/status.py` |
| Does a chat template apply cleanly | the smoke test in `provisioner/main.py` |

## The T4 constraint, which is real and not an artefact of being free

A T4 is compute capability 7.5, so it has **no bfloat16**. Qwen2.5 declares
bf16 in its config, so `--dtype half` is required. Any Ampere card or newer
(A10, L4, A100, H100) takes bf16 natively.

This is worth knowing regardless: it means **dtype cannot be left to `auto`**
in a fleet with mixed accelerators, and probably belongs in the catalog next to
the accelerator rather than being discovered at load time.

## What this does NOT answer

Wall-clock numbers for `docs/baseline.md`. A T4 is not what you would run, and
Colab's disk and network are not your cluster's. The baseline is a measurement
on hardware you would actually use, and still needs one rented hour.
