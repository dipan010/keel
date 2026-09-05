-- 0005 issued gateway keys
--
-- There is deliberately NO column for the key itself. LiteLLM returns the
-- secret once at generation; we hand it to the caller in that one response and
-- never persist it. What is stored is enough to identify, display and revoke a
-- key, and useless to anyone who reads the table:
--
--   alias       what we revoke by; unique so revocation cannot be ambiguous
--   token_hash  LiteLLM's own hash of the key -- a stable id, not a secret
--   masked      LiteLLM's key_name, e.g. "sk-...v4Kw", safe to show a human
--
-- Rows are kept after revocation. "Who had access to this endpoint, and when"
-- is the question the table exists to answer, and deleting rows destroys it.

begin;

create table deployment_keys (
    id             uuid primary key,
    deployment_id  uuid not null references deployments,
    alias          text unique not null,
    token_hash     text not null,
    masked         text not null,
    max_budget_usd numeric(10,2),
    expires_at     timestamptz,
    created_by     text not null,
    created_at     timestamptz not null default now(),
    revoked_at     timestamptz,
    revoked_by     text
);

create index deployment_keys_by_deployment
    on deployment_keys (deployment_id)
    where revoked_at is null;

commit;
