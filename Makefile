.PHONY: help dev db migrate api provisioner reconciler test lint cluster fake clean

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

dev:          ## install deps into .venv
	uv sync --extra dev

db:           ## run postgres in docker
	docker run -d --name keel-db -p 5432:5432 \
		-e POSTGRES_PASSWORD=keel -e POSTGRES_USER=keel -e POSTGRES_DB=keel \
		postgres:17-alpine

migrate:      ## apply migrations in order
	@for f in control/migrations/*.sql; do \
		echo "applying $$f"; \
		docker exec -i keel-db psql -U keel -d keel < $$f; \
	done

catalog:      ## sync catalog/*.yaml into the database
	uv run python -m control.catalog_sync

seed:         ## create a development team
	docker exec -i keel-db psql -q -U keel -d keel -c \
	  "insert into teams (id, slug, oidc_group, gpu_quota, budget_usd_mo) \
	   values (gen_random_uuid(), 'platform', 'keel-dev', 2, 500) \
	   on conflict (slug) do nothing;"

api:          ## run the control api (KEEL_DEV_AUTH=1 bypasses OIDC)
	KEEL_DEV_AUTH=1 uv run uvicorn control.api.main:app --reload --port 8080

provisioner:  ## run the queue worker
	uv run python -m control.provisioner.main

reconciler:   ## run the reconcile loop
	uv run python -m control.reconciler.main

test:         ## all tests; db and cluster suites skip if absent
	uv run pytest -q

test-unit:    ## domain only -- needs nothing at all
	uv run pytest -q control/tests/test_states.py control/tests/test_rules.py \
	  control/tests/test_status.py control/tests/test_no_infra_imports.py

lint:
	uv run ruff check . && uv run ruff format --check .

cluster:      ## local k3d cluster, no gpu
	k3d cluster create keel --agents 1 --wait || true
	kubectl apply -f deploy/kind/namespaces.yaml

cluster-rm:   ## tear the local cluster down
	k3d cluster delete keel

rbac:         ## service accounts and roles that enforce I4 and I5
	kubectl apply -f deploy/kind/namespaces.yaml
	kubectl apply -f deploy/rbac.yaml

gateway:      ## deploy litellm + its own database into the cluster
	kubectl apply -f deploy/kind/namespaces.yaml
	kubectl apply -f deploy/gateway/
	kubectl rollout status deploy/litellm -n keel-gateway --timeout=300s

gateway-fwd:  ## port-forward the gateway to localhost:4000
	kubectl port-forward -n keel-gateway svc/litellm 4000:4000

fake:         ## build the fake vllm runtime and load it into the cluster
	docker build -t keel/fake-runtime:dev bench/fake-runtime
	k3d image import keel/fake-runtime:dev -c keel

clean:
	docker rm -f keel-db 2>/dev/null || true
