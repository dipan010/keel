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

api:          ## run the control api
	uv run uvicorn control.api.main:app --reload --port 8080

provisioner:  ## run the queue worker
	uv run python -m control.provisioner.main

reconciler:   ## run the reconcile loop
	uv run python -m control.reconciler.main

test:         ## unit tests (no cluster, no gpu)
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

cluster:      ## local k3d cluster, no gpu
	k3d cluster create keel --agents 1 || true
	kubectl apply -f deploy/kind/namespaces.yaml

fake:         ## build the fake vllm runtime image
	docker build -t keel/fake-runtime:dev bench/fake-runtime

clean:
	docker rm -f keel-db 2>/dev/null || true
