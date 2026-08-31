-- 0003 allow catalog entries whose image carries its own weights
--
-- catalog_mode_fields required weights_uri on every self_hosted model. That is
-- wrong: ADR-0004's middle path is to bake the two or three most-used models
-- into the image and mount only the long tail, and render/ already omits the
-- fetch-weights init container when weights_uri is absent. The constraint was
-- forbidding a shape the renderer supports.
--
-- accelerator stays required -- placement always has to be decidable.

begin;

alter table catalog_models drop constraint catalog_mode_fields;

alter table catalog_models add constraint catalog_mode_fields check (
    (mode = 'self_hosted' and accelerator is not null)
    or
    (mode = 'upstream' and upstream_model is not null)
);

commit;
