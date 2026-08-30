-- Keel 0001_init
--
-- mode_t and lane_t ship in the FIRST migration on purpose. The mode
-- discriminator changes the shape of every row rather than adding a column,
-- which makes it the one schema decision that is expensive to retrofit and
-- cheap to include now. Phase 1 only implements 'self_hosted'.

begin;

create type mode_t as enum ('upstream', 'self_hosted');
create type lane_t as enum ('a', 'b', 'c');
create type status_t as enum (
    'requested', 'validating', 'scheduling', 'loading',
    'ready', 'updating', 'degraded', 'scaled_to_zero',
    'deleting', 'deleted', 'failed'
);

create table teams (
    id            uuid primary key,
    slug          text unique not null,            -- becomes namespace keel-inf-{slug}
    oidc_group    text not null,                   -- membership lives in the IdP, not here
    gpu_quota     int  not null default 2,         -- mirrored into the ns ResourceQuota
    budget_usd_mo numeric(10,2),
    created_at    timestamptz not null default now(),

    constraint slug_shape check (slug ~ '^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$')
);

create table catalog_models (
    id             text primary key,               -- 'llama-3.3-70b-instruct'
    mode           mode_t not null,
    default_lane   lane_t not null,
    weights_uri    text,                           -- s3://...      null for upstream
    accelerator    text,                           -- 'a100-80g'    null for upstream
    gpu_count      int,
    context_length int not null,
    quantization   text,
    engine_args    jsonb not null default '{}',    -- vLLM flags
    upstream_model text,                           -- litellm provider/model
    license        text not null,                  -- open weights are not uniformly open
    status         text not null default 'preview',-- validated | preview | deprecated
    validated_at   timestamptz,

    constraint catalog_mode_fields check (
        (mode = 'self_hosted' and weights_uri is not null and accelerator is not null)
        or
        (mode = 'upstream'    and upstream_model is not null)
    )
);

create table deployments (
    id                 uuid primary key,
    team_id            uuid not null references teams,
    name               text not null,              -- the "model" string clients send
    mode               mode_t not null,
    lane               lane_t not null,
    model_id           text not null references catalog_models,
    status             status_t not null default 'requested',

    -- issued identity
    route_name         text unique,                -- {team}/{name}, registered in LiteLLM
    k8s_object_name    text,                       -- vllm-{id}, deterministic. See ADR-0002.

    -- self_hosted only
    accelerator        text,
    gpu_count          int,
    replicas_min       int,
    replicas_max       int,
    engine_args        jsonb,

    -- upstream only
    upstream_model     text,
    fallback_id        uuid references deployments,

    -- provenance
    created_by         text not null,
    idempotency_key    text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    last_reconciled_at timestamptz,

    -- This is what makes the discriminator real rather than aspirational.
    -- Without it the mode column is a comment, and within months there are
    -- rows that are neither one thing nor the other.
    constraint mode_fields check (
        (mode = 'self_hosted' and accelerator is not null and replicas_min is not null)
        or
        (mode = 'upstream'    and accelerator is null     and upstream_model is not null)
    ),
    -- lane a IS upstream, and only upstream. Closes off a nonsense state early.
    constraint lane_mode check ((lane = 'a') = (mode = 'upstream')),
    constraint replica_bounds check (
        replicas_min is null or (replicas_min >= 0 and replicas_max >= replicas_min)
    ),
    constraint name_shape check (name ~ '^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$'),
    constraint uniq_name unique (team_id, name),
    constraint uniq_idem unique (team_id, idempotency_key)
);

-- The work queue. Postgres rather than a broker: at a few jobs per team per
-- week a broker is a component to run, monitor and back up in exchange for
-- nothing -- and putting the queue here makes insert-row-and-enqueue a single
-- transaction, which removes the class of bug where a row exists but no job
-- was ever queued. See ADR-0005.
create table jobs (
    id            bigserial primary key,
    deployment_id uuid not null references deployments,
    kind          text not null,                   -- provision | update | delete
    attempts      int  not null default 0,
    locked_until  timestamptz,
    last_error    text,
    created_at    timestamptz not null default now(),

    constraint kind_valid check (kind in ('provision', 'update', 'delete'))
);

create index jobs_claimable on jobs (created_at)
    where locked_until is null;

-- Status is a projection of this log, never a field somebody just sets.
create table deployment_events (
    id            bigserial primary key,
    deployment_id uuid not null references deployments,
    from_status   status_t,
    to_status     status_t not null,
    actor         text not null,                   -- user sub | 'provisioner' | 'reconciler'
    reason_code   text,                            -- unschedulable | oom | weights_fetch_failed
    detail        jsonb,
    at            timestamptz not null default now()
);

create index events_by_deployment on deployment_events (deployment_id, at desc);

-- TTFT/TPOT rather than whole-request latency: for streaming generation a long
-- response is legitimately slow, so p50 of total duration measures the prompt,
-- not the service.
create table deployment_metrics (
    deployment_id uuid not null references deployments,
    window_start  timestamptz not null,            -- hourly
    requests      bigint,
    tokens_in     bigint,
    tokens_out    bigint,
    ttft_p50_ms   int,
    ttft_p95_ms   int,
    tpot_p50_ms   int,
    gpu_util_pct  numeric(5,2),
    gpu_seconds   int,
    cost_usd      numeric(12,4),                   -- computed by us, live. See ADR-0006.

    primary key (deployment_id, window_start)
);

commit;
