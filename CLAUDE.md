# Keel — Claude Code context

A control plane for model inference on Kubernetes: request a model, get a
governed, OpenAI-compatible endpoint behind a gateway, metered and torn down.
Python 3.13 control services; Kubernetes, vLLM, LiteLLM and Postgres are adopted.

## Read first

1. `STATUS.md` — current phase and its exit criteria. Work the lowest unfinished phase.
2. `README.md` — layout, invariants, the bar.
3. `docs/adr/` — every decision has a "revisit if"; check it before changing one.
4. For GPU work: `docs/baseline.md` and `docs/gpu-hour-expectations.md`.

## Current phase

**Phase 1 close-out: the rented GPU hour.** No new provisioning code until
`docs/baseline.md` has real numbers. Anything that costs GPU time is the user's
call. Prepare runbooks and fixes, don't rent.

## Commands

```bash
make dev && make db && make migrate && make catalog && make seed
make test          # all tiers; db/cluster suites skip cleanly if absent
make test-unit     # domain only, needs nothing
make ci            # exactly what CI runs
make lint
make cluster rbac monitoring fake-gpu gateway fake   # local control path, no GPU
```

## Rules

- The seven invariants in README are review blockers. 1, 4 and 5 are enforced by
  RBAC and asserted in `control/tests/test_rbac.py`; don't weaken that test.
- `control/domain/` has no infrastructure imports (enforced).
- Keep the fake runtime honest: it exists so the control path is testable without a
  GPU, and it says nothing about whether a model runs on a GPU.
- Predictions in `docs/gpu-hour-expectations.md` are scored after the run, never edited
  beforehand to match what happened.
- New decisions get an ADR in `docs/adr/` with a "revisit if".
- When a phase's exit criteria are met, tick it in `STATUS.md` and update the status table.
