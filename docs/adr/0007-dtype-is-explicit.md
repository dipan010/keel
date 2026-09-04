# 0007 — dtype is explicit, never inferred

**Status:** accepted · **Date:** 2026-09-01

## Decision

Every self-hosted catalog entry sets `dtype` in `engine_args`. It is never left
to vLLM's `auto`.

## Why

vLLM does **not** fail on an unsupported dtype. On a T4 (compute capability
7.5, no bfloat16) with a model whose config declares bfloat16, it logs:

```
WARNING [model.py:2299] Casting torch.bfloat16 to torch.float16.
```

and carries on. Measured against vLLM 0.28.0; see
`bench/colab/results-2026-09-01.json`.

That is a **silent numerics change, not an error**. In a fleet with mixed
accelerators it means the same catalog entry runs at different precision on
different nodes, with nothing in the deployment record to show for it — and no
way to explain why a model's outputs differ between two replicas of what is
nominally the same deployment.

Being explicit also makes the failure loud where it should be: a T4-only fleet
with `dtype: bfloat16` in the catalog fails at startup with a message the log
classifier recognises (`dtype_unsupported`), rather than quietly downcasting.

## Revisit if

vLLM gains a way to declare "fail rather than downcast", at which point `auto`
plus that flag would be equivalent and less to maintain.
