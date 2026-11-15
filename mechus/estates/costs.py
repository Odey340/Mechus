"""Deterministic, explainable repair estimates.

Only new damage, cleaning and missing items are ever chargeable; wear and tear,
pre-existing issues and resolved items are always $0.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from mechus.estates.store import CHARGEABLE

_SEVERITY = {"minor": 0.6, "moderate": 1.0, "major": 1.7}


@lru_cache(maxsize=1)
def _table() -> dict[str, Any]:
    path = Path(os.getenv("MECHUS_ESTATES_PRICES") or Path(__file__).with_name("prices.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def estimate(finding: dict[str, Any]) -> dict[str, Any]:
    category = finding.get("category")
    if category not in CHARGEABLE:
        return {"low": 0, "high": 0, "basis": "Not chargeable (" + str(category or "baseline").replace("_", " ") + ")", "chargeable": False}
    table = _table()
    if category == "cleaning":
        rule = table["cleaning"]
    elif category == "missing":
        rule = table["missing"]
    else:
        text = " ".join(str(finding.get(k) or "") for k in ("item", "type", "description")).lower()
        rule = next((r for r in table["rules"] if any(m in text for m in r["match"])), table["fallback"])
    mult = _SEVERITY.get(str(finding.get("severity") or "moderate"), 1.0)
    low, high = round(rule["low"] * mult / 5) * 5, round(rule["high"] * mult / 5) * 5
    return {"low": low, "high": max(low, high), "basis": f"{rule['label']} ({finding.get('severity', 'moderate')})", "chargeable": True}


def totals(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counted = [f for f in findings if f.get("review") != "dismissed"]
    by_cat: dict[str, int] = {}
    for f in counted:
        by_cat[f.get("category", "")] = by_cat.get(f.get("category", ""), 0) + 1
    charge = [f for f in counted if (f.get("cost") or {}).get("chargeable")]
    return {
        "findings": len(counted),
        "by_category": by_cat,
        "new_issues": sum(by_cat.get(c, 0) for c in CHARGEABLE),
        "chargeable_low": sum(int((f.get("cost") or {}).get("low", 0)) for f in charge),
        "chargeable_high": sum(int((f.get("cost") or {}).get("high", 0)) for f in charge),
        "pending_review": sum(1 for f in counted if f.get("review", "pending") == "pending"),
    }
