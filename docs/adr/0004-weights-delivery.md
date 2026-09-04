# 0004 — Weights mounted, not baked

**Status:** accepted · **Date:** 2026-08-31

## Decision

Weights live in an object store, are fetched by an init container into a
node-local cache keyed by content digest, and are mounted. The vLLM image stays
generic -- one small image per engine version.

## Why

Baking weights into images produces 20-80 GB artifacts, slow pulls, and a
rebuild for every model-and-quantization combination. Mounting makes adding a
model a registry upload rather than a build.

Provisioning time is literally the acceptance criterion, so this decides itself.
The node-local cache is what turns a repeat deployment from minutes into
seconds.

## Amended 2026-09-01 — measured, and the claim was too broad

"The single biggest lever on time-to-ready" is **only true for large models.**
Measured on a T4 with Qwen2.5-0.5B (vLLM 0.28.0, full output in
`bench/colab/results-2026-09-01.json`):

| Phase                                   | Seconds |
|-----------------------------------------|--------:|
| Model loading (0.93 GiB)                |    24.7 |
| Engine init: profile, KV cache, warmup  |    46.1 |
| — of which torch.compile                |    20.3 |
| CUDA graph capture                      |     9.0 |
| **Total to ready**                      | **130.1** |

Weight handling is under a fifth of it. The rest is **fixed engine startup cost
that does not shrink with the model** — compilation, profiling and graph
capture cost roughly the same for 0.5B as for 70B.

So the cache still matters, but the scoping is: it dominates when weights are
tens of gigabytes, and is close to irrelevant below a few. A 0.5B on a warm
node still takes around 100 seconds to serve traffic.

**Consequence for lane C.** Scale-to-zero cold start is dominated by
compilation and graph capture, not by weights. `--enforce-eager` removes both
(~30s here) at some inference cost, which makes it a real lever for lane C and
the wrong default for lane B. That belongs in the catalog per lane rather than
being a global flag.

## Revisit if

An audit requirement demands one immutable artifact per deployment. A middle
path exists: bake the two or three most-used models, mount the long tail.
