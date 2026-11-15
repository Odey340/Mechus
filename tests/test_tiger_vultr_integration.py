"""Tiger Data + Vultr integration tests.

Unit tests run without any cloud credentials. The live Tiger Data round-trip runs
only when TIGER_DATABASE_URL is set (e.g. pytest tests/test_tiger_vultr_integration.py with .env loaded).
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from mechus import openai_compat, tiger_store
from mechus.meeting_store import MeetingStore


@pytest.fixture
def clean_env(monkeypatch):
    for k in (
        "TIGER_DATABASE_URL", "MECHUS_TIGER_DATABASE_URL", "TIMESCALE_SERVICE_URL", "MECHUS_TIGER_ENABLED",
        "MECHUS_LLM_PROVIDER", "MECHUS_OPENAI_BASE_URL", "MECHUS_OPENAI_API_KEY", "VULTR_INFERENCE_API_KEY",
        "VULTR_INFERENCE_MODEL", "MECHUS_OPENAI_CHAT_MODEL", "MECHUS_OPENAI_RESPONSE_MODEL",
        "MECHUS_OPENAI_THINKING_MODEL", "MECHUS_LLM_BACKEND", "MECHUS_LLM_JSON_MODE",
    ):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# --- Tiger Data -------------------------------------------------------------

def test_tiger_disabled_without_url(clean_env):
    assert tiger_store.is_enabled() is False
    assert tiger_store.get_sink() is None
    assert tiger_store.health() == {"enabled": False, "configured": False}


def test_tiger_can_be_switched_off(clean_env):
    clean_env.setenv("TIGER_DATABASE_URL", "postgres://u:p@localhost:1/tsdb")
    clean_env.setenv("MECHUS_TIGER_ENABLED", "0")
    assert tiger_store.is_enabled() is False


def test_meeting_store_works_without_tiger(clean_env, tmp_path):
    store = MeetingStore("m1", base_dir=str(tmp_path))
    store.append_chunk("alex", "hello team")
    store.append_enriched_chunk("sam", "ship it", chunk_id=1, topic="deploy")
    assert [c.text for c in store.load_chunks_enriched()] == ["hello team", "ship it"]


def test_meeting_store_mirrors_to_sink(clean_env, tmp_path, monkeypatch):
    calls = []

    class FakeSink:
        def record_chunk(self, *a, **kw):
            calls.append(("chunk", a, kw))

        def record_snapshot(self, *a, **kw):
            calls.append(("snapshot", a, kw))

    monkeypatch.setattr(tiger_store, "get_sink", lambda: FakeSink())
    store = MeetingStore("m2", base_dir=str(tmp_path))
    store.append_enriched_chunk("sam", "deploy to vultr", chunk_id=7, topic="deploy", speaker_display="Sam")
    assert calls[0][0] == "chunk"
    assert calls[0][1][:3] == ("m2", "sam", "deploy to vultr")
    assert calls[0][2]["topic"] == "deploy" and calls[0][2]["chunk_id"] == 7


def test_analytics_routes_report_disabled(clean_env):
    try:
        from mechus.api_server import app
    except OSError as exc:  # e.g. PortAudio missing on a headless CI box
        pytest.skip(f"api_server needs audio libs here: {exc}")

    client = app.test_client()
    assert client.get("/api/tiger/status").get_json()["enabled"] is False
    assert client.get("/api/meetings/abc/analytics").status_code == 503
    assert client.get("/api/analytics/overview").status_code == 503


@pytest.mark.skipif(not os.environ.get("TIGER_DATABASE_URL"), reason="needs a Tiger Cloud service")
def test_tiger_round_trip():
    tiger_store.ensure_schema()
    sink = tiger_store.get_sink()
    mid = f"pytest-{uuid.uuid4().hex[:8]}"
    now = time.time()
    sink.record_chunk(mid, "alex", "we decided to deploy on vultr", now - 60, topic="deploy")
    sink.record_chunk(mid, "sam", "great, tiger data stores every line", now - 30, topic="data")
    sink.record_event(mid, "mechus_response", 812.0, {"intent": "test"})
    sink.flush()
    out = tiger_store.meeting_analytics(mid)
    assert out["totals"]["lines"] == 2
    assert {s["speaker_id"] for s in out["speakers"]} == {"alex", "sam"}
    assert out["answer_latency_ms"]["answers"] == 1
    assert tiger_store.search("vultr", meeting_id=mid)[0]["speaker_id"] == "alex"


# --- Vultr Serverless Inference ---------------------------------------------

def test_default_provider_is_openai(clean_env):
    assert openai_compat.chat_completions_url() == "https://api.openai.com/v1/chat/completions"
    assert openai_compat.supports_json_mode() is True


def test_vultr_provider_preset(clean_env):
    clean_env.setenv("MECHUS_LLM_PROVIDER", "vultr")
    clean_env.setenv("VULTR_INFERENCE_API_KEY", "vk-test")
    clean_env.setenv("VULTR_INFERENCE_MODEL", "some-model")
    openai_compat.apply_provider_env()
    assert openai_compat.chat_completions_url() == "https://api.vultrinference.com/v1/chat/completions"
    assert os.environ["MECHUS_OPENAI_API_KEY"] == "vk-test"
    assert os.environ["MECHUS_OPENAI_CHAT_MODEL"] == "some-model"
    assert os.environ["MECHUS_LLM_BACKEND"] == "openai"
    assert openai_compat.supports_json_mode() is False
    assert openai_compat.origin() == "https://api.vultrinference.com/"


def test_explicit_openai_key_wins(clean_env):
    clean_env.setenv("MECHUS_LLM_PROVIDER", "vultr")
    clean_env.setenv("VULTR_INFERENCE_API_KEY", "vk-test")
    clean_env.setenv("MECHUS_OPENAI_API_KEY", "already-set")
    openai_compat.apply_provider_env()
    assert os.environ["MECHUS_OPENAI_API_KEY"] == "already-set"


def test_custom_base_url_for_vultr_gpu(clean_env):
    clean_env.setenv("MECHUS_OPENAI_BASE_URL", "http://10.0.0.5:8000/v1/")
    assert openai_compat.chat_completions_url() == "http://10.0.0.5:8000/v1/chat/completions"


def test_config_picks_up_vultr_preset(clean_env):
    from mechus.config import MechusConfig

    clean_env.setenv("MECHUS_LLM_PROVIDER", "vultr")
    clean_env.setenv("VULTR_INFERENCE_API_KEY", "vk-test")
    clean_env.setenv("VULTR_INFERENCE_MODEL", "some-model")
    cfg = MechusConfig.from_env()
    assert cfg.openai_api_key == "vk-test"
    assert cfg.llm_backend == "openai"
