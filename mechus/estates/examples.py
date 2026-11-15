"""Model findings: the standard every report is held to.

They're shown to the vision model as few-shot examples (so wording, sizing,
location and category reasoning stay consistent across reports) and they're
also what the bundled sample property uses.
"""

from __future__ import annotations

import json

STYLE_GUIDE = (
    "Write each finding like a professional inspector: name the item, the defect, an estimated size in inches, "
    "and its exact location relative to a landmark (door, window, stove, outlet). One sentence, neutral tone, no blame. "
    "Give the category reason in one short clause that cites the evidence (e.g. 'unmarked in move-in footage of the same wall'). "
    "Never guess at what isn't visible; lower the confidence instead."
)

MODEL_FINDINGS = [
    {"item": "Wall", "category": "new_damage", "severity": "moderate",
     "description": "Hole about 2 in. wide in the drywall behind the living room door, at doorknob height.",
     "reason": "Wall is intact in the move-in footage from the same angle."},
    {"item": "Carpet", "category": "wear_and_tear", "severity": "minor",
     "description": "Flattened pile along the walking path from the door to the window.",
     "reason": "Gradual traffic wear over a 12-month lease is normal."},
    {"item": "Countertop", "category": "new_damage", "severity": "moderate",
     "description": "Dark circular burn mark about 3 in. across on the countertop, right of the stove.",
     "reason": "Countertop was unmarked at move-in."},
    {"item": "Oven", "category": "cleaning", "severity": "minor",
     "description": "Heavy grease and food residue on the oven door glass and floor.",
     "reason": "Beyond routine end-of-lease cleaning."},
    {"item": "Tub tile", "category": "pre_existing", "severity": "minor",
     "description": "Chipped corner on the tile at the base of the tub, left side.",
     "reason": "Documented in the move-in report."},
    {"item": "Blinds", "category": "missing", "severity": "moderate",
     "description": "Blinds missing from the bedroom window; both mounting brackets remain.",
     "reason": "Blinds are present in the move-in footage."},
]

MODEL_DEFECTS = [
    {"item": "Tub tile", "type": "chip", "severity": "minor",
     "description": "Chipped corner on the tile at the base of the tub, left side."},
    {"item": "Wall", "type": "scuff", "severity": "minor",
     "description": "Light scuff marks about 6 in. long on the hallway wall beside the light switch."},
]


def compare_examples() -> str:
    return STYLE_GUIDE + " Model findings (match this quality): " + json.dumps(MODEL_FINDINGS, ensure_ascii=False)


def inventory_examples() -> str:
    return STYLE_GUIDE + " Model move-in defects (match this quality): " + json.dumps(MODEL_DEFECTS, ensure_ascii=False)
