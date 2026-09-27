# Keel — Status

Last reviewed: 2026-09-27 (end of the first long session)

| Built | Documented | Hosted | Posted |
|---|---|---|---|
| 🟡 Phase 1 code complete; never run on a real GPU | ✅ README, 8 ADRs, `docs/gpu-hour-expectations.md`, `logs/stage-1..11` | ❌ (local k3d + fake runtime only) | ❌ |

## Resume here

**Next action:** Phase 1 — the rented GPU hour. It is the user's call and costs
~$1–2; the user has said they do not want to spend money *at the moment*. Until
they decide, do not write new provisioning code (CLAUDE.md rule). Useful work
that costs nothing:

1. **Kaggle probe** (free, 2× T4): settles whether vLLM's prefix caching is on by
   default, captures a real OOM message for `classify_log()`, and tests tensor
   parallelism. Use `bench/colab/vllm_probe.ipynb` (pins vLLM 0.28.0); select
   **GPU T4 ×2**, not P100 (compute 6.0, vLLM needs 7.0+). A 7B needs
   `--tensor-parallel-size 2` on T4s and `dtype: float16`.
2. Keep `STATUS.md` and `logs/stage-N/notes.txt` current as work lands.

**Open decisions (the user's):**

| Decision | State |
|---|---|
| Rent the GPU hour | Deferred — no spend for now. GCP trial credits can cover an L4 at $0 out of pocket but need a card and a GPU quota request |
| Merge `spike/kserve` (ADR-0008) | **Wait until after the baseline** — adopting KServe is new provisioning code. The spike passed all six criteria; see ADR-0008 for the four conditions adoption would carry |
| Repo visibility | `dipan010/keel` is **public**. `gh repo edit dipan010/keel --visibility private` if not intended |

## Where it stands

- Public repo `dipan010/keel`, CI green on both branches. **186 tests on `main`**, all against a fake vLLM on a node that only *claims* a GPU.
- **`spike/kserve`** (206 tests): Keel rendered as KServe `InferenceService`s instead of raw Deployments. All six ADR-0008 criteria pass, proven on a from-zero cluster and in a Linux CI job (`kserve`, runs on `spike/**`). Found five things a real deployment would have hit, incl. KServe injecting a 2Gi memory limit that would OOM-kill every real vLLM. Stage log: `logs/stage-11/notes.txt` on that branch.
- Fixed on `main` during the spike: the gateway registered `/models/weights` while vLLM served the HuggingFace id — a real vLLM would have rejected every request. The fake runtime now rejects unknown model names, as vLLM does.
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

## Notes for whoever picks this up

- **Read the stage logs.** `logs/README.md` indexes them; each maps concepts to
  `file:line`. The recurring lesson is in stage 10: a fake laxer than the real
  system has hidden a real bug five times. Keep the fake as strict as vLLM.
- **Commits carry no Co-Authored-By or session trailers** — the owner's
  standing rule, even if tooling suggests otherwise.
- `gh` is not logged in on the development machine; `git push` works through
  the macOS keychain. CI logs need auth — reproduce failures in a Linux
  container (`python:3.12-slim`) instead.
