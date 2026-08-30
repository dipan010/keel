# 0003 — LiteLLM as the gateway

**Status:** accepted · **Date:** 2026-08-31

## Decision

Adopt LiteLLM Proxy. Operate it; do not author it. Do not write a proxy.

## Why

Dialect normalization is the single most valuable thing the platform does for a
developer, and it is what makes "use it wherever you need" true. vLLM wants
`/v1/chat/completions` with the model named in the body; every hosted provider
wants something else. LiteLLM already reconciles those shapes across a long
provider list, plus virtual keys, per-key budgets and spend logging -- close to
the API Management feature list being given up in ADR-0001.

## The constraint that makes it safe

LiteLLM **must never make a synchronous call to the control plane or its
database to serve a request.** It can read config from a database and it will be
tempting to point it at ours -- one source of truth, no reload logic, an
afternoon's work. That converts a routine Postgres restart into a total
inference outage.

Push config and reload. Keep the runtime copy in memory. This is invariant I2,
and it is the specific line in the sand that owning a gateway requires.

## Revisit if

Load testing shows it does not hold up. The fallback is Envoy with an ext_proc
filter, which is a materially larger build -- so test this early, before phase 1
ends, not after.
