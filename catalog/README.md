# Catalog

One YAML file per model. Synced into `catalog_models` by CI.

`status` starts at `preview`. Promotion to `validated` requires:

1. It deploys and passes a real smoke test on the declared accelerator.
2. A benchmark run exists in `bench/` for that model and accelerator.
3. **Someone has read the licence.** Open weights are not uniformly open --
   some forbid commercial use, some forbid training on outputs, Llama carries
   acceptable-use terms. The `license` field is not decoration.
