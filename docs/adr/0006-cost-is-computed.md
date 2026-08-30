# 0006 — Cost is computed by us, and it is live

**Status:** accepted · **Date:** 2026-08-31

## Decision

`cost_usd = gpu_seconds x node hourly rate`, computed continuously. Upstream
spend comes from LiteLLM's spend log within seconds.

## Why

The Azure design had to warn that cost could never be live, because Cost
Management lags by hours -- so a real-time spend panel could not be fed, and a
wrong number about money destroys trust faster than an outage does.

That constraint is gone. We placed the pod and we rented the node, so we know
both terms exactly, in real time.

This is an underrated advantage of leaving the provider's billing system. Cost
stops being a lagging report and becomes a live signal you can autoscale on,
alert on, and show a developer *while* they are choosing a lane -- which is the
entire mechanism by which the future estimator changes behaviour rather than
merely reporting on it.

## Caveat

It is a *modelled* cost, not an invoice. Reconcile against the actual bill
monthly and record the drift; if the two diverge, the model is wrong and
someone must be told.
