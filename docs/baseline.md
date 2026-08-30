# Baseline

> **This file is the first commit that matters and it is currently empty.**
> Fill it in before writing provisioning code.

The acceptance criterion for phase 1 is:

> A developer gets a working, governed, callable endpoint **faster through Keel
> than by running vLLM themselves.**

That claim is unmeasurable without a baseline, and the baseline has to be
measured, not estimated. Do it by hand, with a stopwatch, before building the
thing that has to beat it.

## Procedure

Deploy one small model manually onto a GPU node -- `kubectl`, a vLLM container,
a Service, and whatever it takes to actually call it from outside. Time it end
to end, from "I want this model" to "curl returns a completion".

Do it twice: once cold (no cached weights) and once warm.

## What to record

| Step                              | Cold | Warm |
|-----------------------------------|------|------|
| Find the right image and flags     |      |      |
| Write the manifests                |      |      |
| Weights download                   |      |      |
| Scheduling                         |      |      |
| Weight load into VRAM              |      |      |
| Expose and authenticate it         |      |      |
| **Total wall clock**               |      |      |

## The decision inventory

Every question you had to answer by hand is either a column in the schema or a
hardcoded default in the paved road. List them here as you hit them -- this is
the more valuable half of the exercise.

- [ ] which image and tag
- [ ] which `--max-model-len`
- [ ] which `--gpu-memory-utilization`
- [ ] how weights get to the node
- [ ] how the endpoint is exposed and secured
- [ ] ...

## Why this is worth an afternoon

Our competitor is not another vendor -- it is the developer going around us.
They already have cluster access. An internal platform that is more governed
but slower gets bypassed, and then we own shelfware plus a compliance story
nobody can enforce.
