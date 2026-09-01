# Run 1 — failed before vLLM started

**Result:** no metrics, no smoke test, no OOM signature. The four questions
remain open.

## What happened

```
Tesla T4, 15360 MiB, 7.5          <- GPU was fine
pip install -q vllm               <- succeeded
vllm serve ...                    <- exited during import

  File ".../transformers/audio_utils.py", line 58, in <module>
    import torchaudio
  File ".../torchaudio/_extension/utils.py", line 124, in _check_cuda_version
RuntimeError: Detected that PyTorch and torchaudio were compiled with
              different CUDA versions
```

Installing vLLM upgraded torch. Colab ships a preinstalled torchaudio built
against the older CUDA, `transformers` imports torchaudio unconditionally, and
the version check raises at import time — so vLLM never reached its own code.

Cells 4 and 5 failed as consequences: `ConnectionRefused` on `/metrics` simply
means no server was listening.

## Fix applied

Install into an isolated `uv` venv rather than Colab's environment. The
notebook kernel only talks to the server over HTTP, so it needs none of vLLM's
dependencies itself.

## Worth keeping

Nothing about this was a vLLM or T4 problem — it was a packaging conflict with
the host environment. That is itself a small argument for the design: Keel runs
vLLM in a container with pinned dependencies, which is exactly the class of
failure containers exist to prevent.
