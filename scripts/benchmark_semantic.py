#!/usr/bin/env python3
"""
Benchmark harness: scores semantic correctness of Q&A by expected entities/facts per question.

Each scenario defines (after_line, question_substring, expected_entities).
An entity is a string that should appear in the answer (case-insensitive).
Semantic score = (entities found in answer) / (entities expected), per question, then averaged.

Usage:
  python3 scripts/benchmark_semantic.py --scenario default
  python3 scripts/benchmark_semantic.py --scenario hard
  python3 scripts/benchmark_semantic.py --all   # run all scenarios, print table
  # Set MECHUS_LLM_BACKEND=openai and MECHUS_OPENAI_API_KEY for LLM answers.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from scripts.simulate_meeting_test import run_simulation

# (after_line, question_contains, expected_entities) — question matched by substring
# after_line is the 1-based index stored in qa_log (line after which question was asked)
BENCHMARK_SPEC: dict[str, list[tuple[int, str, list[str]]]] = {
    "default": [
        (4, "decide about the api", ["v2", "api"]),
        (7, "just decide about the api", ["v1", "revert"]),
        (9, "action items", ["alice", "carol", "release", "marketing", "june", "may"]),
        (9, "recap", ["v1", "june", "launch"]),
    ],
    "hard": [
        (4, "decide about the api", ["v2", "june"]),
        (7, "just decide about the api", ["v1", "revert"]),
        (8, "who raised the concern", ["dave"]),
        (11, "action items and who owns", ["alice", "bob", "carol", "release", "marketing", "june", "may"]),
        (11, "open or tbd", ["budget", "50", "40", "rate", "limit", "tbd"]),
        (11, "full recap", ["v1", "june", "carol", "bob", "alice"]),
    ],
    "multi": [
        (3, "decide about pricing", ["tier", "29", "79", "basic", "pro", "enterprise"]),
        (6, "decide about the api", ["v1", "v2", "launch", "q3"]),
        (7, "decide about the api", ["v1", "v2"]),
        (10, "decide about hiring", ["backfill", "engineer", "two", "march"]),
        (12, "decide about pricing", ["free", "basic", "pro", "enterprise"]),
        (15, "timeline", ["june", "15", "may", "june 1"]),
        (15, "action items", ["carol", "pricing", "friday"]),
        (15, "recap", ["pricing", "api", "hiring"]),
    ],
    "finance": [
        (4, "implementation timeline", ["6", "8", "week", "go live"]),
        (5, "pricing", ["12", "45", "tier", "k"]),
        (7, "objection", ["competitor", "10", "quote"]),
        (8, "competitor", ["competitor", "10", "audit", "control"]),
        (10, "next steps", ["proposal", "friday", "demo", "legal"]),
        (11, "recap", ["sam", "10", "proposal", "demo", "friday"]),
    ],
    "long": [
        (5, "pricing", ["12", "10", "45", "tier", "k"]),
        (8, "objection", ["competitor", "10"]),
        (10, "next steps", ["proposal", "demo", "friday"]),
        (41, "discount", ["10", "proposal", "discount"]),
        (41, "implementation timeline", ["8", "week", "kickoff"]),
    ],
}


def _match_spec(after_line: int, question: str, spec: list[tuple[int, str, list[str]]]) -> list[str] | None:
    """Return expected_entities for the first matching (after_line, question_contains), else None."""
    q = (question or "").strip().lower()
    for line, sub, entities in spec:
        if line == after_line and sub in q:
            return entities
    return None


def score_qa_log_semantic(
    qa_log: list[dict],
    scenario: str,
) -> dict:
    """
    Score each Q&A pair by expected entities. Returns:
      per_question: list of { score, expected, found, missing, question }
      overall_score: 0-1
      entities_hit: total entities found
      entities_total: total expected
    """
    spec = BENCHMARK_SPEC.get(scenario, [])
    if not spec:
        return {
            "per_question": [],
            "overall_score": 0.0,
            "entities_hit": 0,
            "entities_total": 0,
            "n_matched": 0,
        }
    per_question = []
    total_hit = 0
    total_expected = 0
    for entry in qa_log:
        after_line = entry.get("after_line", 0)
        question = entry.get("question", "")
        answer = (entry.get("answer") or "").lower()
        expected = _match_spec(after_line, question, spec)
        if expected is None:
            per_question.append({
                "after_line": after_line,
                "question": question[:60],
                "score": None,
                "expected": [],
                "found": [],
                "missing": [],
            })
            continue
        found = [e for e in expected if e.lower() in answer]
        missing = [e for e in expected if e.lower() not in answer]
        score = len(found) / len(expected) if expected else 0.0
        per_question.append({
            "after_line": after_line,
            "question": question[:60],
            "score": score,
            "expected": expected,
            "found": found,
            "missing": missing,
        })
        total_hit += len(found)
        total_expected += len(expected)
    n_matched = sum(1 for p in per_question if p["score"] is not None)
    overall = total_hit / total_expected if total_expected else 0.0
    return {
        "per_question": per_question,
        "overall_score": overall,
        "entities_hit": total_hit,
        "entities_total": total_expected,
        "n_matched": n_matched,
    }


def run_benchmark(scenario: str) -> dict:
    """Run simulation for scenario and return result + semantic benchmark scores."""
    use_hard = scenario == "hard"
    use_multi = scenario == "multi"
    use_finance = scenario == "finance"
    use_long = scenario == "long"
    result = run_simulation(
        use_hard=use_hard,
        use_multi_topic=use_multi,
        use_finance=use_finance,
        use_long=use_long,
    )
    scores = score_qa_log_semantic(result["qa_log"], scenario)
    return {
        "scenario": scenario,
        "result": result,
        "semantic": scores,
        "responder": result["responder"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark semantic correctness of meeting Q&A")
    ap.add_argument("--scenario", default="default", choices=["default", "hard", "multi", "finance", "long"],
                    help="Scenario to run")
    ap.add_argument("--all", action="store_true", help="Run all scenarios and print summary table")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print per-question entity breakdown")
    args = ap.parse_args()

    if args.all:
        scenarios = ["default", "hard", "multi", "finance"]
        print("Semantic benchmark (entity-level correctness)\n")
        print(f"{'Scenario':<12} {'Score':<8} {'Hit/Total':<12} {'Responder':<10}")
        print("-" * 50)
        for sc in scenarios:
            t0 = time.perf_counter()
            out = run_benchmark(sc)
            elapsed = time.perf_counter() - t0
            s = out["semantic"]
            score = s["overall_score"]
            hit_total = f"{s['entities_hit']}/{s['entities_total']}"
            print(f"{sc:<12} {score:.2f}     {hit_total:<12} {out['responder']:<10}  ({elapsed:.0f}s)")
            if args.verbose:
                for p in s["per_question"]:
                    if p["score"] is not None:
                        miss = ", ".join(p["missing"][:5]) or "—"
                        print(f"    [{p['after_line']}] {p['question']!r} -> {p['score']:.2f} (missing: {miss})")
        return 0

    out = run_benchmark(args.scenario)
    s = out["semantic"]
    print(f"Scenario: {args.scenario}")
    print(f"Semantic score (entity correctness): {s['overall_score']:.2f} ({s['entities_hit']}/{s['entities_total']} entities)")
    print()
    if args.verbose or s["n_matched"] <= 10:
        for p in s["per_question"]:
            if p["score"] is not None:
                print(f"  [line {p['after_line']}] {p['question']}")
                print(f"    Score: {p['score']:.2f}  Found: {p['found']}  Missing: {p['missing']}")
    return 0 if s["overall_score"] >= 0.5 else 1


if __name__ == "__main__":
    sys.exit(main())
