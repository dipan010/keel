-- 0007 record what the prefix cache served
--
-- tokens_in comes from vllm:prompt_tokens_total, which counts cached and
-- computed prompt tokens identically. For any workload with a shared system
-- prompt -- which is most agent workloads -- that attributes full compute cost
-- to tokens served from KV cache.
--
-- It does not break billing, because cost is derived from GPU-seconds rather
-- than tokens. It does mean the corpus the estimator will be built from
-- overstates the work done per token, and prefix-cache hit rate is exactly
-- what a sizing model needs to reason about shared prefixes.
--
-- tokens_in keeps its meaning: ALL prompt tokens. tokens_in_cached is the
-- subset that was not recomputed, so computed = tokens_in - tokens_in_cached
-- and no existing reader changes behaviour.

begin;

alter table deployment_metrics add column tokens_in_cached bigint;
alter table deployment_metrics add column prefix_cache_hit_pct numeric(5,2);

commit;
