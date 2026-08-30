# 0005 — Postgres as the work queue

**Status:** accepted · **Date:** 2026-08-31

## Decision

`SELECT ... FOR UPDATE SKIP LOCKED` against a `jobs` table. No broker.

## Why

At a few provisioning jobs per team per week, a dedicated broker is a component
to run, monitor and back up in exchange for nothing.

More importantly, putting the queue in the same database makes
insert-row-and-enqueue **a single transaction**, which removes an entire class
of bug where a deployment row exists but no job was ever queued.

An abandoned lock expires via `locked_until` and the job is reclaimed, so there
are no dead letters to manage either.

## Revisit if

Job volume reaches a rate where queue polling is meaningful load on the primary
-- far beyond anything plausible for this workload.
