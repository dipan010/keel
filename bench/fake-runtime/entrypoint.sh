#!/bin/sh
# Stand in for vLLM's entrypoint: accept the engine flags Keel passes so the
# fake is a drop-in at the manifest level. Without an entrypoint, `args` in the
# pod spec replaces the command instead of appending to it.
#
# It honours exactly one of them, because vLLM does: the model is served under
# --served-model-name if given, otherwise under --model. An earlier version
# ignored both and answered to any name, which hid a real mismatch between what
# the gateway sent and what vLLM would have accepted.
served=""; model=""
while [ $# -gt 0 ]; do
  case "$1" in
    --served-model-name) served="$2"; shift 2 ;;
    --model)             model="$2";  shift 2 ;;
    *)                   shift ;;
  esac
done
export FAKE_MODEL="${served:-${model:-fake-7b}}"
echo "fake-runtime: serving as '$FAKE_MODEL'"
exec uvicorn app:app --host 0.0.0.0 --port 8000
