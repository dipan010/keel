"""catalog/*.yaml -> catalog_models.

Adding a model is a pull request with an approval; this is what applies the
merged result. Run from CI on main.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import yaml

from control import db

CATALOG = pathlib.Path(__file__).resolve().parents[1] / "catalog"

UPSERT = """
insert into catalog_models
  (id, mode, default_lane, weights_uri, accelerator, gpu_count, context_length,
   quantization, engine_args, upstream_model, license, status, validated_at)
values
  (%(id)s, %(mode)s, %(default_lane)s, %(weights_uri)s, %(accelerator)s,
   %(gpu_count)s, %(context_length)s, %(quantization)s, %(engine_args)s,
   %(upstream_model)s, %(license)s, %(status)s,
   case when %(status)s = 'validated' then now() else null end)
on conflict (id) do update set
  mode = excluded.mode,
  default_lane = excluded.default_lane,
  weights_uri = excluded.weights_uri,
  accelerator = excluded.accelerator,
  gpu_count = excluded.gpu_count,
  context_length = excluded.context_length,
  quantization = excluded.quantization,
  engine_args = excluded.engine_args,
  upstream_model = excluded.upstream_model,
  license = excluded.license,
  status = excluded.status,
  validated_at = case
    when excluded.status = 'validated' and catalog_models.validated_at is null
    then now() else catalog_models.validated_at end
"""

REQUIRED = ("id", "mode", "default_lane", "context_length", "license", "status")


def load() -> list[dict]:
    entries = []
    for path in sorted(CATALOG.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        missing = [k for k in REQUIRED if raw.get(k) is None]
        if missing:
            raise SystemExit(f"{path.name}: missing required field(s) {missing}")
        raw.setdefault("engine_args", {})
        raw["engine_args"] = json.dumps(raw["engine_args"])
        for optional in (
            "weights_uri",
            "accelerator",
            "gpu_count",
            "quantization",
            "upstream_model",
        ):
            raw.setdefault(optional, None)
        entries.append(raw)
    return entries


async def sync() -> int:
    entries = load()
    await db.open_pool()
    try:
        async with db.transaction() as conn:
            for e in entries:
                await conn.execute(UPSERT, e)
    finally:
        await db.close_pool()
    for e in entries:
        print(f"  {e['id']:<32} {e['mode']:<12} lane {e['default_lane']}  {e['status']}")
    return len(entries)


if __name__ == "__main__":
    n = asyncio.run(sync())
    print(f"synced {n} model(s)", file=sys.stderr)
