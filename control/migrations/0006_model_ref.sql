-- 0006 what to actually load
--
-- render/ hardcoded "--model /models/weights", a path that exists only if the
-- fetch-weights init container ran. Meanwhile every catalog entry set
-- weights_uri, so that container was always rendered -- pointing at an image
-- that has never been built. A real model could not have started.
--
-- model_ref is what vLLM is told to load. It takes two shapes:
--
--   weights_uri IS NULL      model_ref is a HuggingFace repo id and vLLM
--                            fetches it itself. Simplest, one fewer moving
--                            part, and the right first step on real hardware.
--   weights_uri IS NOT NULL  the init container fetches into the node-local
--                            cache and model_ref is ignored in favour of the
--                            mount path (ADR-0004).
--
-- Keeping them separate is what lets the same catalog entry move between those
-- without changing what it identifies.

begin;

alter table catalog_models add column model_ref text;

-- Backfill: both existing entries are HuggingFace ids derived from their name.
update catalog_models set model_ref = 'Qwen/Qwen2.5-0.5B-Instruct'
    where id = 'qwen2.5-0.5b-instruct' and model_ref is null;
update catalog_models set model_ref = 'Qwen/Qwen2.5-7B-Instruct'
    where id = 'qwen2.5-7b-instruct' and model_ref is null;
-- Anything else self-hosted: fall back to the catalog id so the constraint can
-- be added. A wrong-but-present value fails loudly at load; a null would fail
-- silently at render.
update catalog_models set model_ref = id
    where mode = 'self_hosted' and model_ref is null;

alter table catalog_models add constraint model_ref_required check (
    mode <> 'self_hosted' or model_ref is not null
);

commit;
