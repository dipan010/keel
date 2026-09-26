# Keel — Status

Last reviewed: 2026-09-27

| Built | Documented | Hosted | Posted |
|---|---|---|---|
| 🟡 Phase 1 code complete; never run on a real GPU | ✅ README, 7 ADRs, `docs/gpu-hour-expectations.md` | ❌ (local k3d + fake runtime only) | ❌ |

## Where it stands

- Public repo `dipan010/keel`, CI green (`make ci`, three test tiers). 174 tests, all against a fake vLLM on a node that only *claims* a GPU.
- Phase 1 scope: one model, one GPU, lane B, end to end. Everything except the baseline measurement is done.
- `docs/baseline.md` is **empty** and is the last open item of phase 1. It needs one rented L4/A10 hour (~$1–2).
- Predictions for that hour are written down in advance (`docs/gpu-hour-expectations.md`). P1 (missing `runtimeClassName`) was mitigated on 2026-09-26 via `KEEL_RUNTIME_CLASS`; P2–P5 are still open.
- Architecture artifact: linked from README.

## Phases to completion

### Phase 1 — Close phase 1: the GPU hour
- [ ] Rent an L4/A10 box and run `ACCELERATOR=l4 ./deploy/gpu-node/setup.sh`. Record compute capability (ADR-0007: set `dtype: float16` below 8.0).
- [ ] **Manual run first**, on the fresh box (cold numbers cannot be recovered afterwards), then the Keel run. Follow `docs/baseline.md` step by step.
- [ ] Fill in `docs/baseline.md`: manual vs Keel, cold and warm, plus the decision inventory.
- [ ] Score every prediction P1–P5 in `docs/gpu-hour-expectations.md` right/wrong.
- [ ] Run `test_full_pipeline.py` against the real GPU: one request in, one working endpoint out, verified by a real completion.
- [ ] Fix anything the hour breaks; capture a real OOM message and check it against `control/provisioner/status.py`.
- **Exit:** baseline filled in, predictions scored, full-pipeline test passes on real hardware. If Keel loses to the manual path, write that down (§3 of the expectations doc); it is a result, not a failure.

### Phase 2 — Phase 2 scope (the "Out" list)
Pick in this order unless the baseline says otherwise:
- [ ] Reconciler loop against real clusters (desired vs actual, alert on orphans, never delete).
- [ ] Autoscaling (HPA/KEDA) on real vLLM metrics.
- [ ] Hourly metrics rollup with real `gpu_util_pct` (resolve P2: DCGM attribution to a deployment).
- [ ] Lanes A and C.
- [ ] Each item gets an ADR if it makes a choice, with a "revisit if".
- **Exit:** a deployment survives a node restart and a scale-up/scale-down cycle with correct cost rows.

### Phase 3 — Hosted demo
- [ ] Decide what "hosted" means for a GPU control plane: a recorded end-to-end run (asciinema/video) plus the architecture artifact is the realistic public form; a permanently running GPU is not.
- [ ] Optional: a time-boxed live instance on a rented box for a demo window.
- [ ] README "Results" section with the baseline numbers.
- **Exit:** someone who never runs it can see it work.

### Phase 4 — Publish
- [ ] Write-up: the bar ("faster than running vLLM yourself"), the predictions-before-the-run discipline, and how they scored.
- [ ] Post on LinkedIn / blog; link it here.

## Definition of done
Phase 1 closed on real hardware · docs current with measured numbers · a recorded/live demo · write-up published.
