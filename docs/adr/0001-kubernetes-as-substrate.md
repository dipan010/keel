# 0001 — Kubernetes as the substrate

**Status:** accepted · **Date:** 2026-08-31 · supersedes the Azure-native Rev A

## Decision

Target Kubernetes and nothing else. No cloud provider in the architecture.

## Why

- **It removes the only irreducible schedule risk.** The Azure design's long
  pole was GPU quota: days of waiting, per region, per family. Renting an H100
  by the hour needs no quota process.
- **It inverts the build order onto the interesting half.** On Azure, hosted
  models were easy and self-hosted was phase 2+. Here the first thing built is
  the hard, differentiated thing -- and the thing that generates the data the
  estimator needs.
- **It makes Azure additive rather than a rewrite.** AKS becomes a kubeconfig,
  not a redesign. Committing to AML would have meant the opposite.

## What it costs

API Management was carrying auth, token limits, metering, key issuance,
routing and dialect normalization. All of it is now ours to operate, and one
component moves into the hot path. See ADR-0003.

## Revisit if

Someone specifically wants managed serving with no cluster to operate, at which
point AML plugs in behind `control/render/` as a second backend.
