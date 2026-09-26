"""The fake runtime must be at least as strict as vLLM wherever Keel depends on
vLLM's behaviour. Four times now a fake laxer than the real thing hid a real
bug: metric names, histogram shape, a key hash, and the served model name.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from fastapi.testclient import TestClient

APP = pathlib.Path(__file__).resolve().parents[2] / "bench/fake-runtime/app.py"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FAKE_MODEL", "qwen2.5-0.5b-instruct")
    monkeypatch.setenv("FAKE_LOAD_SECONDS", "0")
    spec = importlib.util.spec_from_file_location("fake_runtime_app", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return TestClient(mod.app)


def _chat(client, model):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_it_answers_to_the_name_it_serves(client):
    assert _chat(client, "qwen2.5-0.5b-instruct").status_code == 200


def test_it_rejects_any_other_name_like_vllm_does(client):
    r = _chat(client, "/models/weights")
    assert r.status_code == 404
    assert "does not exist" in r.json()["message"]


def test_models_endpoint_reports_the_served_name(client):
    assert client.get("/v1/models").json()["data"][0]["id"] == "qwen2.5-0.5b-instruct"
