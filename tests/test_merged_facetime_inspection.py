"""Regression coverage for the merged Telemachus FaceTime inspection backend."""

from __future__ import annotations

import json
from unittest.mock import patch

from mechus.chert_client import ChertConfig, classify_destination, mint_livekit_token
from mechus.inspection import Finding, LocalInspectionStore, list_findings, parse_findings


def test_chert_config_accepts_existing_dashboard_env_names() -> None:
    env = {
        "MECHUS_CHERT_FACETIME_API_KEY": "api-key",
        "MECHUS_CHERT_PROJECT_ID": "project-1",
        "MECHUS_CHERT_FACETIME_LINE_ID": "line-1",
        "MECHUS_CHERT_FACETIME_BASE_URL": "https://chert.example/api/",
        "MECHUS_LIVEKIT_URL": "wss://livekit.example",
        "MECHUS_LIVEKIT_API_KEY": "lk-key",
        "MECHUS_LIVEKIT_API_SECRET": "lk-secret",
    }
    with patch.dict("os.environ", env, clear=True):
        config = ChertConfig.from_env()
    assert config.missing() == []
    assert config.api_key == "api-key"
    assert config.line_id == "line-1"
    assert config.api_base == "https://chert.example/api"


def test_destination_and_livekit_token_are_valid_without_network() -> None:
    assert classify_destination("(512) 555-1212") == ("phone", "+15125551212")
    assert classify_destination("Tenant@Example.com") == ("email", "tenant@example.com")
    token = mint_livekit_token(
        api_key="key", api_secret="secret", identity="mechus", room="inspection-1"
    )
    header, payload, signature = token.split(".")
    assert header and payload and signature


def test_inspection_parser_and_local_store_round_trip(tmp_path) -> None:
    scene, matched, findings = parse_findings(json.dumps({
        "scene": "kitchen wall",
        "matched_move_in": 2,
        "findings": [{
            "surface": "wall",
            "issue": "crack above sink",
            "severity": "high",
            "box": [-4, 100, 1050, 900],
            "say": "There is a crack above the sink.",
        }],
    }))
    assert (scene, matched) == ("kitchen wall", 2)
    assert findings[0].box == [0, 100, 1000, 900]

    store = LocalInspectionStore(str(tmp_path))
    store.save(
        unit="4b", mode="move_in", meeting_id="m1", jpeg=b"jpeg-bytes",
        scene=scene, findings=findings,
    )
    records = store.load_baseline("4b")
    assert records[0]["jpeg"] == b"jpeg-bytes"
    assert list_findings(records)[0]["issue"] == "crack above sink"

