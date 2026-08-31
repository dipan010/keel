#!/bin/sh
# Stand in for vLLM's entrypoint: accept and ignore the engine flags Keel
# passes (--model, --port, --max-model-len ...) so the fake is a drop-in at the
# manifest level. Without this, `args` in the pod spec replaces the command
# instead of appending to it, and the container crashloops.
echo "fake-runtime: ignoring engine args: $*"
exec uvicorn app:app --host 0.0.0.0 --port 8000
