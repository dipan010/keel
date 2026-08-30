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

## Revisit if

An audit requirement demands one immutable artifact per deployment. A middle
path exists: bake the two or three most-used models, mount the long tail.
