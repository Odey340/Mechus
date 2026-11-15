#!/usr/bin/env python3
"""
Simulate a full test meeting and measure Mechus performance.

1. Feeds meeting transcript chunks into memory in order (as in a real meeting).
2. At set points, simulates "Mechus" questions (no real STT/TTS).
3. Measures latency (memory + LLM) and answer quality per question.
4. Prints full transcript and a performance summary.

Usage:
  python3 scripts/simulate_meeting_test.py
  python3 scripts/simulate_meeting_test.py --stress   # OpenAI: default+hard meetings, seconds + p95 (needs MECHUS_OPENAI_API_KEY)
  python3 scripts/simulate_meeting_test.py --compare-latency   # A/B vs MECHUS_LOW_LATENCY=1
  python3 scripts/simulate_meeting_test.py --transcript path/to/transcript.txt   # raw adversarial transcript (Speaker: line; Mechus, question = wake-word)
"""

from __future__ import annotations

import os
import sys
import time

_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from mechus.envfile import load_env_file

from mechus.config import MechusConfig
from mechus.context_integrity_engine import ContextIntegrityEngine
from mechus.intent import INTENT_ACTION_ITEMS, INTENT_OPEN_QUESTIONS, classify_intent
from mechus.retrieval import retrieve_for_question
from mechus.memory import ConversationMemory
from mechus.responder import OpenAIResponder, ResponseInput, RuleResponder
from mechus.text import strip_wake_word


def _retrieve_kwargs(config: MechusConfig) -> dict:
    """Match production agent retrieval parameters (see simulate_financial_scrum_mechus._ask_mechus)."""
    return {
        "max_chars": config.retrieval_max_chars,
        "general_max_chars": config.retrieval_general_max_chars,
        "retrieval_mode": config.retrieval_mode,
        "bm25_weight": config.retrieval_bm25_weight,
        "tag_weight": config.retrieval_tag_weight,
        "embed_weight": config.retrieval_embed_weight,
        "embed_rerank_top_k": config.retrieval_embed_rerank_top_k,
        "openai_api_key": config.openai_api_key,
        "embedding_model": config.embedding_model,
        "retrieval_auto_lexical_gap_min": config.retrieval_auto_lexical_gap_min,
        "retrieval_auto_top_floor_hybrid": config.retrieval_auto_top_floor_hybrid,
        "query_embed_cache_max": config.retrieval_query_embed_cache_max,
    }


def _print_storage_model() -> None:
    notes = os.getenv("MECHUS_NOTES_DIR", "mechus_notes")
    mdir = os.getenv("MECHUS_MEETING_STORE_DIR", "mechus_meetings")
    print("--- Storage model (this script vs production) ---\n")
    print("  Simulation (this script):")
    print("    - ConversationMemory: rolling text segments in RAM (MECHUS_MEMORY_MAX_CHARS / RECENT).")
    print("    - ContextIntegrityEngine: tagged chunks, raw timestamps, canonical state in RAM.")
    print("  Production (MechusAgent live meeting):")
    print(f"    - NoteStore JSONL under MECHUS_NOTES_DIR (default {notes!r}).")
    print(f"    - MeetingStore enriched chunks under MECHUS_MEETING_STORE_DIR (default {mdir!r}).")
    print()


def _percentile_ms(latencies_ms: list[float], p: float) -> float:
    if not latencies_ms:
        return 0.0
    s = sorted(latencies_ms)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def _print_latency_summary_seconds(
    label: str,
    qa_log: list[dict],
    latencies_ms: list[float],
    wall_seconds: float,
) -> None:
    print(f"=== {label} ===\n")
    print(f"  Wall-clock (full run): {wall_seconds:.3f} s")
    print(f"  Questions answered: {len(qa_log)}")
    if not latencies_ms:
        print("  (no latency samples)\n")
        return
    lat_s = [x / 1000.0 for x in latencies_ms]
    print(
        f"  Total per question (s): min={min(lat_s):.3f}  max={max(lat_s):.3f}  "
        f"avg={sum(lat_s) / len(lat_s):.3f}  "
        f"p50={_percentile_ms(latencies_ms, 0.50) / 1000:.3f}  "
        f"p95={_percentile_ms(latencies_ms, 0.95) / 1000:.3f}"
    )
    if qa_log:
        r = [e.get("router_ms", 0) or 0 for e in qa_log]
        m = [e.get("memory_ms", 0) or 0 for e in qa_log]
        l = [e.get("llm_ms", 0) or 0 for e in qa_log]
        n = len(qa_log)
        print(
            f"  Per-stage avg (s): router={sum(r) / n / 1000:.4f}  "
            f"retrieval={sum(m) / n / 1000:.4f}  llm={sum(l) / n / 1000:.4f}"
        )
    print()


def _require_openai_for_stress() -> bool:
    load_env_file(os.getenv("MECHUS_ENV_FILE", os.path.join(_root, ".env")))
    key = (os.getenv("MECHUS_OPENAI_API_KEY") or "").strip()
    if not key:
        print("Error: --stress requires MECHUS_OPENAI_API_KEY in the environment.", file=sys.stderr)
        return False
    return True


def _run_stress_mode(compare_latency: bool) -> int:
    """Run default + hard simulations; print wall seconds, p50/p95 in seconds, optional A/B vs MECHUS_LOW_LATENCY."""
    if not _require_openai_for_stress():
        return 1
    cfg = MechusConfig.from_env()
    if (cfg.llm_backend or "").lower() != "openai" or not (cfg.openai_api_key or "").strip():
        print("Error: --stress requires MECHUS_LLM_BACKEND=openai and MECHUS_OPENAI_API_KEY.", file=sys.stderr)
        return 1

    print("=" * 60)
    print("OPENAI STRESS (default meeting + hard meeting; same path as live Q&A)")
    print("=" * 60)
    print()

    saved_ll = os.environ.get("MECHUS_LOW_LATENCY")

    def one_stress_round(label: str) -> tuple[float, list[dict], list[float]]:
        wall0 = time.perf_counter()
        r_def = run_simulation()
        r_hard = run_simulation(use_hard=True)
        wall = time.perf_counter() - wall0
        qa = r_def["qa_log"] + r_hard["qa_log"]
        lat = r_def["latencies_ms"] + r_hard["latencies_ms"]
        _print_latency_summary_seconds(label, qa, lat, wall)
        return wall, qa, lat

    try:
        if compare_latency:
            os.environ.pop("MECHUS_LOW_LATENCY", None)
            w1, qa1, lat1 = one_stress_round("Round A: MECHUS_LOW_LATENCY unset")
            os.environ["MECHUS_LOW_LATENCY"] = "1"
            w2, qa2, lat2 = one_stress_round("Round B: MECHUS_LOW_LATENCY=1")
            p95_1 = _percentile_ms(lat1, 0.95) / 1000.0
            p95_2 = _percentile_ms(lat2, 0.95) / 1000.0
            print("--- Compare (p95 total latency per question, seconds) ---\n")
            print(f"  Round A p95: {p95_1:.3f} s   wall: {w1:.3f} s")
            print(f"  Round B p95: {p95_2:.3f} s   wall: {w2:.3f} s")
            if p95_1 > 0:
                print(f"  p95 ratio B/A: {p95_2 / p95_1:.2f}x")
            print()
        else:
            one_stress_round("Stress run (current env)")
    finally:
        if saved_ll is None:
            os.environ.pop("MECHUS_LOW_LATENCY", None)
        else:
            os.environ["MECHUS_LOW_LATENCY"] = saved_ll

    _print_storage_model()
    print("Optimization: set MECHUS_LOW_LATENCY=1 for lexical-only retrieval and tighter budgets (see mechus/config.py).")
    print("Re-run with --compare-latency to measure A/B without editing .env.")
    return 0


# Standard meeting
MEETING_SCRIPT = [
    "Alice: Let's kick off. We need to decide on the Q2 launch date. I propose June 15.",
    "Bob: I agree with June 15. Action item: Alice will finalize the release notes by June 1.",
    "Carol: Open question: who owns the marketing rollout? We still need to figure out the budget.",
    "Alice: Decision: we have decided to use the existing API. Let's go with the v2 endpoint.",
    "Bob: Risk: we're worried about the dependency on the legacy system. Latency could spike.",
    "Carol: Assuming we get sign-off from legal by next Friday. Our assumption: no change to the timeline.",
    "Alice: Actually we should not use the v2 endpoint. Let's revert to v1 for now.",
    "Bob: So the final decision is we stay on v1. Carol will own the marketing rollout. Due by end of May.",
    "Carol: Metric: target 10% conversion. Goal is 50K users by launch.",
]

MECHUS_QUESTIONS = [
    (3, "What did we decide about the API?"),
    (6, "What did we just decide about the API?"),
    (8, "What are the action items?"),
    (8, "Can you give a quick recap of this meeting?"),
]

# Harder simulation: longer meeting, multiple reversals, conflicting numbers, speaker attribution
HARD_MEETING_SCRIPT = [
    "Alice: Kickoff. We need to lock the Q2 launch date and the API strategy. I'm pushing for June 15 and the v2 endpoint.",
    "Bob: I'm with Alice on June 15. I'll own the release notes, due June 1. Carol can you take marketing?",
    "Carol: Sure. Open question though: budget for the campaign. We said 50K last week but finance mentioned 40K. Which is it?",
    "Alice: Let's go with 50K for now. Decision: we're going with the v2 API. Engineering signed off.",
    "Dave: I'm not sure we should ship v2. The legacy system dependency is a risk. Latency could spike under load.",
    "Bob: Dave has a point. We're worried about the dependency. Let me check with platform.",
    "Alice: Actually scratch that. Let's revert to v1 for the launch. We can do v2 in Q3. So the final decision is we stay on v1.",
    "Carol: Got it. I'll own marketing rollout, due end of May. I'll assume 50K unless someone tells me otherwise.",
    "Bob: Action item for me: release notes by June 1. And I'll confirm the 50K vs 40K budget with finance by Friday.",
    "Dave: Risk to call out: if we stay on v1 we might hit rate limits. We said 10K RPS but the contract says 8K. Open question which number is right.",
    "Alice: So to recap: launch June 15, v1 not v2, Carol marketing by end of May, Bob release notes June 1. Budget and rate limits still TBD.",
]

# Hard questions: decision reversal, who said what, conflicting numbers, open points
HARD_MECHUS_QUESTIONS = [
    (3, "What did we decide about the API?"),                    # Early: v2
    (6, "What did we just decide about the API?"),              # After reversal: v1
    (7, "Who raised the concern about the legacy system?"),      # Speaker attribution: Dave
    (10, "What are all the action items and who owns them?"),   # Alice, Bob, Carol + tasks
    (10, "What questions are still open or TBD?"),             # Budget 50K vs 40K, rate limits 10K vs 8K
    (10, "Give me a full recap of decisions, action items, and risks."),
]

# Failure case: provisional vs final blur. Correct answer is v1 (final decision). Wrong: v2 or "v2 in Q3".
FAILURE_CASE_SCRIPT = [
    "Alice: Decision: we're going with the v2 API for launch. Engineering signed off.",
    "Bob: I'm worried about the legacy dependency. Latency could spike.",
    "Alice: Okay, let's revert to v1 for now. So the final decision is we stay on v1.",
    "Carol: We'll revisit v2 in Q3 when platform is ready.",
    "Dave: Sounds good. We're aligned on v2 directionally for later.",
    "Alice: Action item: Bob will own the v1 cutover by end of month.",
]
FAILURE_CASE_QUESTIONS = [(5, "What did we decide about the API?")]  # After last line (index 5); must answer v1 (final), not v2

# Script with explicit launch decision for state-completeness check (extractor should capture it).
MEETING_SCRIPT_WITH_LAUNCH = [
    "Alice: Let's decide the launch date. I propose June 15.",
    "Bob: I agree with June 15.",
    "Alice: Decision: we have decided the launch date is June 15.",
]

# Multi-topic meeting: different topics throughout; answers must be in the right context
MULTI_TOPIC_SCRIPT = [
    # --- Topic: PRICING (opening) ---
    "Alice: Let's start with pricing. I propose tiered: basic, pro, enterprise. We align on that?",
    "Bob: Agreed. Let's say basic at 29, pro at 79, enterprise custom. Decision: tiered pricing with those tiers.",
    "Carol: I'll own the pricing doc. Due by next Friday.",
    # --- Topic: API (switch) ---
    "Dave: Switching to API. Should we ship v2 or stay on v1 for launch?",
    "Alice: I want v2. Engineering signed off. Let's go with v2.",
    "Bob: Actually the legacy dependency is risky. I'd say we revert to v1 for launch and do v2 in Q3.",
    "Alice: Fine. Decision: we stay on v1 for launch, v2 in Q3.",
    # --- Topic: HIRING (new topic) ---
    "Carol: On hiring. We need two engineers by Q2. Open question is whether we backfill or add net new.",
    "Dave: Backfill for the two open roles. So decision: two backfills, not net new.",
    "Alice: Recruiting will have a first slate by end of March.",
    # --- Topic: back to PRICING (revision) ---
    "Bob: Back to pricing. Should we add a free tier for students?",
    "Alice: Yes. Add a free tier. So we have four tiers now: free, basic, pro, enterprise.",
    "Carol: I'll update the pricing doc to include the free tier. Still due Friday.",
    # --- Topic: TIMELINE (dates) ---
    "Dave: Timeline check. Launch date?",
    "Alice: June 15 for launch. Marketing by end of May, release notes by June 1. That's the timeline.",
]

# Questions at specific points so the answer must come from the RIGHT topic (not the last thing said)
# Index 14 = after last segment (Alice states June 15, May, June 1)
MULTI_TOPIC_QUESTIONS = [
    (2, "What did we decide about pricing?"),           # After pricing -> pricing only (tiered, 29/79)
    (5, "What did we decide about the API?"),           # After v2 discussed -> API only
    (6, "What did we decide about the API?"),           # After reversal -> v1 for launch, v2 Q3
    (9, "What did we decide about hiring?"),            # After hiring -> backfill, two roles, March slate
    (11, "What did we decide about pricing?"),         # After pricing revision -> include free tier
    (14, "What's the timeline?"),                      # After Alice states timeline -> June 15, May, June 1
    (14, "What are the action items and who owns them?"),  # Carol: pricing doc; recruiting: slate; etc.
    (14, "Give a quick recap of what we decided on pricing, API, and hiring."),  # All three, right context
]

# Finance / sales call: different situation (rep, prospect, pricing, objections, competitor, next steps)
FINANCE_SALES_SCRIPT = [
    "Sam: Thanks for jumping on, Alex. I'm Sam from FinanceFlow. Wanted to walk you through how we help teams like Acme Corp with reconciliation.",
    "Alex: Sure. We're evaluating a few options. What's the typical implementation timeline?",
    "Sam: Usually 6 to 8 weeks to go live. We do discovery first, then configuration, then onboarding. For a team your size, I'd say 8 weeks to be safe.",
    "Alex: And pricing? We have a hard cap this quarter.",
    "Sam: We have two tiers. Standard is 12K per year per module. Enterprise is custom but typically 45K annual with the compliance bundle. I can get you a discount if you commit by end of quarter.",
    "Alex: 12K is workable. The main objection we have is we're also looking at CompetitorX. They quoted us 10K.",
    "Sam: CompetitorX is solid for basic reporting. Where we win is audit trails and the finance controls. We can match 10K for the first year if you sign a two-year. That's something I'd need to get approved.",
    "Alex: Let me take that back. What are the next steps?",
    "Sam: Next steps: I'll send the 10K year-one proposal by Friday. You get legal to review. We schedule a demo with your controllership for next week. Then we target a contract by end of month so we're in before quarter close.",
    "Alex: Demo next week works. We'll need the proposal in writing.",
    "Sam: Got it. I'll send the proposal by Friday and calendar the demo. Anything else on pricing or the competitor comparison?",
]

FINANCE_SALES_QUESTIONS = [
    (3, "What's the implementation timeline they mentioned?"),
    (4, "What did they say about pricing?"),
    (6, "What objection did the prospect raise?"),
    (7, "What did they say about the competitor?"),
    (9, "What are the next steps?"),
    (10, "Give me a quick recap of the sales call."),
]

# Long meeting (~60 min): 40+ segments to test memory retrieval when context is large
def _long_meeting_script() -> list[str]:
    base = list(FINANCE_SALES_SCRIPT)
    extra = [
        "Alex: One more thing on the discount. Is that 10K per module or total?",
        "Sam: Total for the first year for up to two modules. Then it goes to standard 12K per module.",
        "Alex: And the compliance bundle you mentioned?",
        "Sam: That's part of Enterprise. Audit trails, role-based access. We can add that to Standard as an add-on for 4K.",
        "Alex: So 10K plus 4K for compliance in year one.",
        "Sam: Yes. I'll put that in the proposal.",
        "Alex: What about support during implementation?",
        "Sam: We assign a dedicated CSM. Weekly check-ins until go-live. No extra cost.",
        "Alex: And if we need to push go-live?",
        "Sam: We can extend the timeline. No penalty. We'd just adjust the renewal date.",
        "Alex: Good. Who do we need on our side for the demo?",
        "Sam: Ideally whoever owns reconciliation and someone from IT for the integration piece.",
        "Alex: We're also looking at rollout by region. Does that change the timeline?",
        "Sam: It can. Phase by region adds about two weeks per region. We can outline that in the SOW.",
        "Alex: And the CompetitorX comparison you mentioned?",
        "Sam: I'll send a one-pager. Main differentiators are audit trails and the finance controls. CompetitorX is weaker there.",
        "Alex: That would help. Thanks Sam.",
        "Sam: I'll send the proposal by Friday and calendar the demo. Thanks Alex.",
        "Alex: Quick question on the 8-week timeline.",
        "Sam: Sure.",
        "Alex: Is that from contract or from kickoff?",
        "Sam: From kickoff. Contract to kickoff is usually one to two weeks.",
        "Alex: So we're looking at 10 weeks end to end.",
        "Sam: Right. End of month contract puts you in before quarter close.",
        "Alex: And the two-year commitment for the 10K rate?",
        "Sam: Yes. Year one at 10K, year two at standard 12K per module unless we renew with a discount.",
        "Alex: Got it. I'll get back to you after we see the proposal.",
        "Sam: Sounds good. I'll have it over by Friday.",
    ]
    return base + extra  # 11 + 30 = 41 segments -> triggers long-meeting retrieval (35+)


LONG_MEETING_QUESTIONS = [
    (4, "What did they say about pricing?"),
    (7, "What objection did the prospect raise?"),
    (9, "What are the next steps?"),
    (40, "What discount or proposal did they agree on?"),   # Answer from early in long call
    (40, "What's the implementation timeline?"),            # Answer from early in long call
]


def _meeting_45min_script() -> list[str]:
    """~45 segments simulating a 45-minute product/planning meeting. Multiple topics, decisions, action items, reversals."""
    return [
        "Jordan: Let's kick off the Q2 planning. We need to lock launch date, API strategy, and pricing.",
        "Sam: I propose we target June 15 for launch. Engineering can hit that if we scope to v1.",
        "Alex: I agree with June 15. Decision: we have decided the launch date is June 15.",
        "Jordan: On the API. Should we ship v2 or stay on v1 for launch?",
        "Sam: I want v2. We've been on v1 for a year.",
        "Alex: Risk: the legacy dependency could cause latency spikes. I'd recommend v1 for launch.",
        "Jordan: Let's go with v1 for launch. We can do v2 in Q3. So the final decision is we stay on v1.",
        "Sam: Action item: Sam will own the v1 cutover and run the migration by May 20.",
        "Alex: I'll own the release notes. Due June 1.",
        "Jordan: Pricing. We had tiered basic, pro, enterprise. Any change?",
        "Sam: I'd add a free tier for students. Open question: do we cap free tier usage?",
        "Alex: We should cap at 10K API calls per month for free.",
        "Jordan: Decision: we add a free tier with 10K API cap. Basic at 29, pro at 79, enterprise custom.",
        "Sam: Marketing will update the pricing page by next Friday.",
        "Alex: Who owns the pricing doc?",
        "Jordan: Carol will own the pricing doc. Due next Friday.",
        "Sam: Hiring. We need two engineers by Q2.",
        "Alex: Open question: backfill or net new?",
        "Jordan: Let's do backfill for the two open roles. Decision: two backfills, not net new.",
        "Sam: Recruiting will have a first slate by end of March.",
        "Alex: Risk: if we can't hire in time, we slip the June 15 launch.",
        "Jordan: We're assuming we fill by April. Our assumption: no change to the timeline.",
        "Sam: One more thing. Budget for the campaign. Finance said 40K, we had 50K last week.",
        "Alex: Still TBD. We need to align with finance.",
        "Jordan: Postpone the budget question to next week. So we have launch June 15, v1 API, tiered pricing with free tier, two backfills.",
        "Sam: Action items: Sam v1 cutover May 20, Alex release notes June 1, Carol pricing doc next Friday, Recruiting slate end of March.",
        "Alex: And the 10K cap for free tier is decided.",
        "Jordan: Yes. Anything else?",
        "Sam: Competitor analysis. Should we do a one-pager?",
        "Alex: Yes. I'll draft it by Wednesday.",
        "Jordan: Good. So to recap: launch June 15, API v1 for launch v2 in Q3, pricing with free tier 10K cap, two backfills, slate by March.",
        "Sam: What about the budget?",
        "Jordan: Still open. We postponed to next week.",
        "Alex: And the risk on hiring?",
        "Jordan: We're assuming we fill by April.",
        "Sam: Got it. I'll send the cutover plan by EOD.",
        "Jordan: One more. Actually we should not slip v2 to Q3. Let's target v2 by end of Q2.",
        "Alex: That's aggressive. We said v1 for launch and v2 in Q3.",
        "Jordan: You're right. Let's stay with v1 for launch, v2 in Q3. Final decision unchanged.",
        "Sam: So v1 for launch, v2 in Q3. Understood.",
        "Alex: Action item: Alex will document the API roadmap and share with eng by Friday.",
        "Jordan: Good. Let's wrap. Recap: June 15 launch, v1 then v2 Q3, tiered pricing with free 10K cap, two backfills, budget TBD next week.",
        "Sam: And cutover May 20, release notes June 1, pricing doc next Friday, slate end of March.",
        "Jordan: Yes. Thanks everyone.",
        "Alex: Thanks.",
        "Sam: Thanks.",
    ]  # 45 segments


MEETING_45MIN_QUESTIONS = [
    (10, "What did we decide about the API?"),           # Early: v1 for launch
    (10, "What are the action items so far?"),
    (22, "What did we decide about pricing?"),           # Mid: free tier, 10K cap, basic/pro/enterprise
    (22, "What questions are still open?"),
    (35, "What is the launch date?"),
    (35, "What did we just confirm about the API?"),       # After re-confirm: still v1, v2 Q3
    (44, "Can you give a quick recap of the meeting?"),
]

# General voice agent: mix of meeting content and general intents (greeting, help, stop)
# Phrasing aligned with context engine patterns so fast path returns action items / decisions
GENERAL_VOICE_AGENT_SCRIPT = [
    "Alice: Quick sync. Decision: we're going with v1 for launch. Carol will own marketing by end of May.",
    "Bob: Action item: Bob will send the release notes by June 1.",
    "Carol: Open question: budget for the campaign still TBD.",
]
GENERAL_VOICE_QUESTIONS = [
    (0, "Hello"),
    (0, "What can you do?"),
    (2, "What are the action items?"),
    (2, "What did we decide?"),
    (2, "Give me a quick recap."),
    (2, "Thanks, that's all for now."),
]

# 20-minute sports planning meeting — live Mechus call-outs (wake, fast-path, reversals, grounding)
SPORTS_MEETING_SCRIPT = [
    "Alice: Alright everyone, let's finalize the basketball tournament plans.",
    "Bob: First, format. Round robin or single elimination?",
    "Charlie: Round robin gives everyone more games.",
    "Dana: But round robin adds about $800 in court time.",
    "Alice: That's not trivial.",
    "Bob: If we increase registration by $20 per team we can offset that.",
    "Charlie: That might reduce signups.",
    "Alice: Okay, let's settle this. I propose single elimination.",
    "Bob: I agree.",
    "Dana: Agreed.",
    "Charlie: Fine, let's go with single elimination.",
    "Alice: Decision made — single elimination bracket.",
    "Bob: Next — venue. Lincoln High gym is available March 15th.",
    "Dana: Rental cost is $1,200 for the full day.",
    "Alice: Let's book it.",
    "Bob: I'll book Lincoln High by this Friday.",
    "Charlie: For marketing, I'll design social media ads and flyers.",
    "Alice: When can you have drafts ready?",
    "Charlie: By next Wednesday.",
    "Dana: We also need sponsorships.",
    "Alice: Yes. At least three sponsors.",
    "Bob: I'll reach out to five local businesses by Monday.",
    "Charlie: What about referees?",
    "Bob: We'll need four certified refs.",
    "Alice: That's still unresolved.",
    "Dana: Budget update — with single elimination we're projected to net about $2,000 profit.",
    "Alice: Good.",
    "Charlie: Should we livestream the finals?",
    "Bob: That requires extra equipment rental.",
    "Dana: That would cost another $500.",
    "Alice: Let's postpone the livestream decision.",
    "Bob: Actually, what if we reconsider round robin?",
    "Dana: That would push profit down to maybe $800.",
    "Alice: That's too low. Let's stay with single elimination.",
    "Charlie: One more thing — do we need medical staff onsite?",
    "Dana: Yes, that's required by the school district.",
    "Alice: Add that to the checklist.",
]

# Mechus invocations: (after_line_index_0based, question)
SPORTS_MECHUS_QUESTIONS = [
    (11, "What did we just decide about the format?"),
    (15, "What are the action items so far?"),
    (21, "What is Bob responsible for?"),
    (30, "What questions are still open?"),
    (33, "Are we still doing single elimination?"),
    (36, "Can you give us a quick recap?"),
]

# Optional advanced stress: reversal then re-confirm, then ask final format
SPORTS_MEETING_SCRIPT_STRESS = SPORTS_MEETING_SCRIPT + [
    "Bob: Actually, let's switch back to round robin.",
    "Dana: No, that drops profit too much.",
    "Alice: Confirming — we are staying with single elimination.",
]
SPORTS_MECHUS_QUESTIONS_STRESS = SPORTS_MECHUS_QUESTIONS + [
    (39, "What is our final format decision?"),
]


def run_simulation(
    use_hard: bool = False,
    use_multi_topic: bool = False,
    use_finance: bool = False,
    use_long: bool = False,
    use_general: bool = False,
    use_sports: bool = False,
    use_sports_stress: bool = False,
    use_failure_case: bool = False,
    use_45min: bool = False,
) -> dict:
    config = MechusConfig.from_env()
    if use_long:
        script = _long_meeting_script()
        questions = LONG_MEETING_QUESTIONS
    elif use_45min:
        script = _meeting_45min_script()
        questions = MEETING_45MIN_QUESTIONS
    elif use_finance:
        script = FINANCE_SALES_SCRIPT
        questions = FINANCE_SALES_QUESTIONS
    elif use_multi_topic:
        script = MULTI_TOPIC_SCRIPT
        questions = MULTI_TOPIC_QUESTIONS
    elif use_failure_case:
        script = FAILURE_CASE_SCRIPT
        questions = FAILURE_CASE_QUESTIONS
    elif use_hard:
        script = HARD_MEETING_SCRIPT
        questions = HARD_MECHUS_QUESTIONS
    elif use_general:
        script = GENERAL_VOICE_AGENT_SCRIPT
        questions = GENERAL_VOICE_QUESTIONS
    elif use_sports_stress:
        script = SPORTS_MEETING_SCRIPT_STRESS
        questions = SPORTS_MECHUS_QUESTIONS_STRESS
    elif use_sports:
        script = SPORTS_MEETING_SCRIPT
        questions = SPORTS_MECHUS_QUESTIONS
    else:
        script = MEETING_SCRIPT
        questions = MECHUS_QUESTIONS

    memory = ConversationMemory(
        max_chars=config.memory_max_chars,
        recent_chars=config.memory_recent_chars,
    )
    # Context engine for deterministic retrieval (action items, open questions, decisions)
    context_engine = ContextIntegrityEngine(
        meeting_id=f"sim-{use_hard}-{use_finance}-{use_long}-{use_general}-{use_sports}-{use_sports_stress}-{use_failure_case}-{use_45min}"
    )
    if config.llm_backend == "openai" and config.openai_api_key:
        responder = OpenAIResponder(
            api_key=config.openai_api_key,
            model=config.openai_chat_model or "gpt-4o-mini",
            max_response_words=config.max_response_words,
        )
    else:
        responder = RuleResponder(max_response_words=config.max_response_words)

    transcript_log: list[str] = []
    qa_log: list[dict] = []
    latencies_ms: list[float] = []

    for i, line in enumerate(script):
        memory.add_segment(line)
        transcript_log.append(line)
        # Feed context engine (parse "Speaker: text")
        if ":" in line:
            speaker, _, rest = line.partition(":")
            context_engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000.0 + i)
        else:
            context_engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000.0 + i)
        for after_idx, question in questions:
            if i != after_idx:
                continue
            request_text = strip_wake_word(f"Mechus {question}", config.wake_phrase).strip() or question
            t0 = time.perf_counter()
            bundle = retrieve_for_question(
                request_text,
                context_engine=context_engine,
                memory=memory,
                recent_transcript="",
                **_retrieve_kwargs(config),
            )
            t_mem = (time.perf_counter() - t0) * 1000
            context_text = bundle.formatted_context
            memory_text = bundle.memory_text
            t_router0 = time.perf_counter()
            _ = classify_intent(question)
            t_router = (time.perf_counter() - t_router0) * 1000
            t_llm0 = time.perf_counter()
            inp = ResponseInput(
                wake_phrase=config.wake_phrase,
                wake_phrases=config.wake_phrases,
                request_text=request_text,
                context_text=context_text,
                memory_text=memory_text,
                context_chunks=getattr(bundle, "chunks", None) or [],
                intent=getattr(bundle, "intent", None) or "",
                state=getattr(bundle, "state", None) or {},
                retrieval_meta=bundle.meta,
            )
            answer = responder.respond(inp)
            t_llm = (time.perf_counter() - t_llm0) * 1000
            total_ms = t_router + t_mem + t_llm
            latencies_ms.append(total_ms)
            qa_log.append({
                "after_line": i + 1,
                "question": question,
                "answer": answer or "(no response)",
                "router_ms": round(t_router, 2),
                "memory_ms": round(t_mem, 2),
                "llm_ms": round(t_llm, 2),
                "total_ms": round(total_ms, 2),
                "llm_called": True,
                "event_ids": list(bundle.event_ids),
                "structured_path": False,
            })
            transcript_log.append(f"[Mechus] Q: {question}")
            transcript_log.append(f"[Mechus] A: {answer or '(no response)'}")

    final_state = context_engine.get_canonical_state_for_qa()
    return {
        "transcript": transcript_log,
        "qa_log": qa_log,
        "latencies_ms": latencies_ms,
        "responder": "openai" if isinstance(responder, OpenAIResponder) else "rules",
        "hard": use_hard,
        "multi_topic": use_multi_topic,
        "finance": use_finance,
        "long": use_long,
        "general": use_general,
        "sports": use_sports,
        "sports_stress": use_sports_stress,
        "failure_case": use_failure_case,
        "use_45min": use_45min,
        "state": final_state,
    }


def _is_wake_line(line: str) -> bool:
    """True if line is a wake-word question (Mechus, ... or Mechus: ...)."""
    s = line.strip()
    return s.startswith("Mechus,") or s.startswith("Mechus:")


def _extract_question_from_wake_line(line: str) -> str:
    """Extract question text after 'Mechus,' or 'Mechus:'."""
    s = line.strip()
    if s.startswith("Mechus,"):
        return s[5:].strip()
    if s.startswith("Mechus:"):
        return s[5:].strip()
    return s


def run_simulation_from_transcript(
    transcript_lines: list[str],
    *,
    config: MechusConfig | None = None,
) -> dict:
    """
    Run simulation from raw transcript: each line is 'Speaker: text'.
    Lines that start with 'Mechus,' or 'Mechus:' are wake-word questions; we respond and log.
    No predefined expected outcomes — blind adversarial test.
    """
    config = config or MechusConfig.from_env()
    memory = ConversationMemory(
        max_chars=config.memory_max_chars,
        recent_chars=config.memory_recent_chars,
    )
    context_engine = ContextIntegrityEngine(meeting_id="sim-raw-transcript")
    if config.llm_backend == "openai" and config.openai_api_key:
        responder = OpenAIResponder(
            api_key=config.openai_api_key,
            model=config.openai_chat_model or "gpt-4o-mini",
            max_response_words=config.max_response_words,
        )
    else:
        responder = RuleResponder(max_response_words=config.max_response_words)

    transcript_log: list[str] = []
    qa_log: list[dict] = []
    latencies_ms: list[float] = []

    for i, raw_line in enumerate(transcript_lines):
        line = raw_line.strip()
        if not line:
            continue
        if _is_wake_line(line):
            question = _extract_question_from_wake_line(line)
            request_text = strip_wake_word(f"Mechus {question}", config.wake_phrase).strip() or question
            t0 = time.perf_counter()
            bundle = retrieve_for_question(
                request_text,
                context_engine=context_engine,
                memory=memory,
                recent_transcript="",
                **_retrieve_kwargs(config),
            )
            t_mem = (time.perf_counter() - t0) * 1000
            context_text = bundle.formatted_context
            memory_text = bundle.memory_text
            t_router0 = time.perf_counter()
            _ = classify_intent(question)
            t_router = (time.perf_counter() - t_router0) * 1000
            t_llm0 = time.perf_counter()
            inp = ResponseInput(
                wake_phrase=config.wake_phrase,
                wake_phrases=config.wake_phrases,
                request_text=request_text,
                context_text=context_text,
                memory_text=memory_text,
                context_chunks=getattr(bundle, "chunks", None) or [],
                intent=getattr(bundle, "intent", None) or "",
                state=getattr(bundle, "state", None) or {},
                retrieval_meta=bundle.meta,
            )
            answer = responder.respond(inp)
            t_llm = (time.perf_counter() - t_llm0) * 1000
            total_ms = t_router + t_mem + t_llm
            latencies_ms.append(total_ms)
            qa_log.append({
                "after_line": i + 1,
                "question": question,
                "answer": answer or "(no response)",
                "router_ms": round(t_router, 2),
                "memory_ms": round(t_mem, 2),
                "llm_ms": round(t_llm, 2),
                "total_ms": round(total_ms, 2),
                "llm_called": True,
                "event_ids": list(bundle.event_ids),
                "structured_path": False,
            })
            transcript_log.append(line)
            transcript_log.append(f"[Mechus] Q: {question}")
            transcript_log.append(f"[Mechus] A: {answer or '(no response)'}")
            continue
        memory.add_segment(line)
        transcript_log.append(line)
        if ":" in line:
            speaker, _, rest = line.partition(":")
            context_engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000.0 + i)
        else:
            context_engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000.0 + i)

    # End-state for summary
    action_items = context_engine.get_action_items_structured()
    open_questions = context_engine.get_open_questions_list()
    postponed = context_engine.get_postponed_issues_list()
    decisions = context_engine.get_current_decisions()
    reversal_log = context_engine.get_reversal_log()

    return {
        "transcript": transcript_log,
        "qa_log": qa_log,
        "latencies_ms": latencies_ms,
        "responder": "openai" if isinstance(responder, OpenAIResponder) else "rules",
        "raw_transcript": True,
        "action_items_count": len(action_items),
        "open_questions_count": len(open_questions),
        "postponed_count": len(postponed),
        "final_decisions": [v for _, v in decisions],
        "reversals_handled": len(reversal_log),
    }


def evaluate_quality(
    qa_log: list[dict],
    hard: bool = False,
    multi_topic: bool = False,
    finance: bool = False,
    long: bool = False,
    general: bool = False,
) -> dict:
    """Simple quality checks based on expected content."""
    scores = []
    for entry in qa_log:
        q = (entry["question"] or "").lower()
        a = (entry["answer"] or "").lower()
        checks = []
        if general:
            if "hello" in q or q.strip() == "hello":
                checks.append(("greeting", "here" in a or "listen" in a or "recap" in a))
            elif "what can you" in q or "what do you do" in q:
                checks.append(("help", "listen" in a or "recap" in a or "action" in a))
            elif "action" in q:
                checks.append(("action items", "carol" in a or "bob" in a or "june" in a or "may" in a or "no action" in a))
            elif "decide" in q:
                checks.append(("decision", "v1" in a or "launch" in a or "no decision" in a))
            elif "recap" in q:
                checks.append(("recap", len(a.split()) >= 5 or "v1" in a or "launch" in a or "information" in a))
            elif "thanks" in q or "that's all" in q:
                checks.append(("stop", len(a) <= 10 or a.strip() == ""))
            else:
                checks.append(("answered", len(a) > 0))
        elif "decide" in q and "api" in q:
            checks.append(("mentions decision", any(x in a for x in ["v1", "v2", "api", "revert", "decided", "decision"])))
        if "action" in q:
            checks.append(("mentions actions", any(x in a for x in ["alice", "bob", "carol", "dave", "release", "marketing", "june", "may", "action"])))
        if "recap" in q:
            checks.append(("recap content", len(a.split()) >= 10 or "launch" in a or "decision" in a or "v1" in a))
        if "who" in q and ("concern" in q or "legacy" in q or "risk" in q):
            checks.append(("speaker attribution", "dave" in a or "someone" in a))
        if "open" in q or "tbd" in q:
            checks.append(("open points", any(x in a for x in ["budget", "50", "40", "rate", "limit", "tbd", "open"])))
        if multi_topic:
            if "pricing" in q and "hiring" not in q and "api" not in q:
                checks.append(("pricing only", any(x in a for x in ["tier", "29", "79", "free", "basic", "pro", "enterprise"])))
            if "api" in q:
                checks.append(("api only", any(x in a for x in ["v1", "v2", "launch", "q3"])))
            if "hiring" in q:
                checks.append(("hiring only", any(x in a for x in ["backfill", "engineer", "two", "march", "recruit"])))
            if "timeline" in q:
                checks.append(("timeline", any(x in a for x in ["june", "may", "march", "friday"])))
        if finance or long:
            if "pricing" in q:
                checks.append(("pricing", any(x in a for x in ["12", "10", "45", "tier", "discount", "k"])))
            if "objection" in q:
                checks.append(("objection", any(x in a for x in ["competitor", "10k", "quote", "cap"])))
            if "competitor" in q:
                checks.append(("competitor", any(x in a for x in ["competitorx", "competitor", "compare"])))
            if "next steps" in q:
                checks.append(("next steps", any(x in a for x in ["proposal", "demo", "friday", "contract", "legal"])))
            if "timeline" in q or "implementation" in q:
                checks.append(("implementation", any(x in a for x in ["week", "8", "6", "go live"])))
        score = sum(1 for _, v in checks if v) / max(1, len(checks))
        scores.append(score)
    return {
        "per_question_scores": scores,
        "average_score": sum(scores) / max(1, len(scores)),
        "total_questions": len(qa_log),
    }


def _run_raw_transcript(args) -> int:
    """Load raw transcript file, run blind adversarial test, print wake log and end summary."""
    path = args.transcript
    if not os.path.isfile(path):
        print(f"Error: transcript file not found: {path}", file=sys.stderr)
        return 1
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f.readlines()]
    lines = [ln for ln in lines if ln.strip()]

    print("=" * 60)
    print("RAW TRANSCRIPT (blind adversarial)")
    print("=" * 60)
    print()

    result = run_simulation_from_transcript(lines)

    print("--- Transcript + Mechus Q&A ---\n")
    for line in result["transcript"]:
        print(line)
    print()

    print("--- Wake call log (per wake-word) ---\n")
    for idx, entry in enumerate(result["qa_log"]):
        path_str = "structured" if entry.get("structured_path") else "llm"
        print(f"  Wake {idx + 1}: {entry.get('question', '')[:80]}")
        print(f"    router_ms={entry.get('router_ms', 0):.2f}  memory_ms={entry['memory_ms']:.2f}  llm_ms={entry['llm_ms']:.2f}  total_ms={entry['total_ms']:.2f}")
        print(f"    event_ids: {entry.get('event_ids', [])}")
        print(f"    path: {path_str}")
        print(f"    answer: {(entry.get('answer') or '')[:400]}")
        print()
    print()

    total = len(result["qa_log"])
    llm_calls = sum(1 for e in result["qa_log"] if e.get("llm_called"))
    structured_calls = sum(1 for e in result["qa_log"] if e.get("structured_path"))
    print("--- End summary ---\n")
    print(f"  Total wake-word responses: {total}")
    print(f"  Total LLM calls: {llm_calls}")
    print(f"  Structured vs LLM ratio: {structured_calls} structured, {llm_calls} llm")
    print(f"  Action items captured: {result.get('action_items_count', 0)}")
    print(f"  Active open questions: {result.get('open_questions_count', 0)}")
    print(f"  Postponed: {result.get('postponed_count', 0)}")
    print(f"  Final decisions tracked: {len(result.get('final_decisions', []))}")
    for d in result.get("final_decisions", [])[:10]:
        print(f"    - {d[:80]}")
    print(f"  Reversals handled: {result.get('reversals_handled', 0)}")
    print()
    return 0


def _run_failure_case() -> int:
    """
    Run provisional-vs-final blur scenario. Correct: current decision is v1 (final).
    Wrong: answer says v2 is the decision or blurs 'v2 in Q3' with current.
    """
    print("=" * 60)
    print("FAILURE CASE: Provisional vs final blur")
    print("=" * 60)
    print("  Script: decide v2 -> revert to v1 -> 'revisit v2 in Q3' -> 'aligned on v2 directionally'")
    print("  Question: What did we decide about the API?")
    print("  Expected: v1 (final). Failure: v2 or blur with v2/Q3.")
    print()

    result = run_simulation(use_failure_case=True)
    state = result.get("state") or {}
    decisions = state.get("decisions") or []
    qa = result.get("qa_log") or []
    answer = (qa[0].get("answer") or "").lower() if qa else ""

    # Canonical state must have api (or api_strategy) -> v1 (final)
    api_decisions = [d for d in decisions if "api" in (d.get("id") or "").lower()]
    state_has_v1 = any("v1" in (d.get("value") or "") for d in api_decisions)
    state_has_v2_as_current = any("v2" in (d.get("value") or "") and "v1" not in (d.get("value") or "") for d in api_decisions)

    # Answer must indicate v1 as decision, not v2
    answer_says_v1 = ("v1" in answer or "version 1" in answer) and (
        "v2" not in answer
        or any(x in answer for x in ["revisit", "q3", "directionally", "later", "reverted", "revert", "v1"])
    )
    answer_wrongly_says_v2 = "v2" in answer and not any(
        x in answer for x in ["revisit", "q3", "directionally", "later", "reverted", "revert", "stay on v1", "v1 for now", "version 1"]
    )

    print("--- Transcript + Q&A ---\n")
    for line in result["transcript"]:
        print(line)
    print()
    print("--- State (canonical decisions) ---")
    for d in decisions:
        print(f"  {d.get('id')}: {d.get('value')}")
    print()
    print("--- Checks ---")
    print(f"  State has api -> v1: {state_has_v1}")
    print(f"  State wrongly has api -> v2 only: {state_has_v2_as_current}")
    print(f"  Answer indicates v1: {answer_says_v1}")
    print(f"  Answer wrongly says v2 is current: {answer_wrongly_says_v2}")
    print()

    passed = state_has_v1 and not state_has_v2_as_current and answer_says_v1 and not answer_wrongly_says_v2
    if passed:
        print("  Result: PASS – State and answer correctly reflect final decision (v1).")
    else:
        print("  Result: FAIL – State or answer blurs provisional (v2/Q3) with final (v1).")
    print()
    return 0 if passed else 1


def _run_state_completeness_check() -> int:
    """
    Ingest MEETING_SCRIPT_WITH_LAUNCH (includes 'Decision: launch date June 15'), then assert
    canonical state contains a launch/timeline decision. Documents extraction gap until we add propose/agree.
    """
    print("=" * 60)
    print("STATE COMPLETENESS: Launch decision in canonical state")
    print("=" * 60)
    print("  Script includes: 'Decision: we have decided the launch date is June 15.'")
    print("  Assert: state.decisions has an entry with id or value mentioning launch/june/timeline.")
    print()

    context_engine = ContextIntegrityEngine(meeting_id="sim-state-completeness")
    for i, line in enumerate(MEETING_SCRIPT_WITH_LAUNCH):
        if ":" in line:
            speaker, _, rest = line.partition(":")
            context_engine.ingest_chunk(rest.strip(), speaker_id=speaker.strip(), timestamp=1000.0 + i)
        else:
            context_engine.ingest_chunk(line, speaker_id="unknown", timestamp=1000.0 + i)

    state = context_engine.get_canonical_state_for_qa()
    decisions = state.get("decisions") or []
    keywords = ("launch", "june", "timeline", "date")
    has_launch_decision = any(
        any(k in (d.get("id") or "").lower() or k in (d.get("value") or "").lower() for k in keywords)
        for d in decisions
    )

    print("--- Script ---")
    for line in MEETING_SCRIPT_WITH_LAUNCH:
        print(f"  {line}")
    print()
    print("--- State.decisions ---")
    for d in decisions:
        print(f"  id={d.get('id')!r} value={d.get('value')!r}")
    if not decisions:
        print("  (empty)")
    print()
    print(f"  Has launch/june/timeline decision: {has_launch_decision}")
    if has_launch_decision:
        print("  Result: PASS – State completeness OK.")
    else:
        print("  Result: FAIL – Event extraction did not capture launch decision (add propose/agree or explicit Decision pattern).")
    print()
    return 0 if has_launch_decision else 1


def _run_all_scenarios(args) -> int:
    """Run default, hard, multi-topic, and general voice agent scenarios; print summary matrix."""
    scenarios = [
        ("default", {"use_hard": False, "use_multi_topic": False, "use_finance": False, "use_long": False, "use_general": False}),
        ("hard", {"use_hard": True, "use_multi_topic": False, "use_finance": False, "use_long": False, "use_general": False}),
        ("multi_topic", {"use_hard": False, "use_multi_topic": True, "use_finance": False, "use_long": False, "use_general": False}),
        ("general_voice_agent", {"use_hard": False, "use_multi_topic": False, "use_finance": False, "use_long": False, "use_general": True}),
    ]
    results = []
    for name, kwargs in scenarios:
        t0 = time.perf_counter()
        result = run_simulation(**kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        quality = evaluate_quality(
            result["qa_log"],
            hard=result.get("hard", False),
            multi_topic=result.get("multi_topic", False),
            finance=result.get("finance", False),
            long=result.get("long", False),
            general=result.get("general", False),
        )
        results.append({
            "name": name,
            "quality": quality["average_score"],
            "passed": quality["average_score"] >= (0.4 if result.get("hard") else 0.5),
            "latencies": result["latencies_ms"],
            "qa_log": result["qa_log"],
            "elapsed_ms": elapsed,
        })
    print("=" * 60)
    print("ALL SCENARIOS SUMMARY")
    print("=" * 60)
    for r in results:
        avg_lat = sum(r["latencies"]) / max(1, len(r["latencies"]))
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  {r['name']}: quality={r['quality']:.2f} avg_latency={avg_lat:.0f}ms {status}")
    print()
    print("--- Per-scenario Q&A (last answer only) ---")
    for r in results:
        print(f"\n  [{r['name']}]")
        for e in r["qa_log"][:3]:
            a = (e.get("answer") or "")[:120]
            print(f"    Q: {e.get('question', '')[:50]} -> A: {a}...")
        if len(r["qa_log"]) > 3:
            print(f"    ... and {len(r['qa_log']) - 3} more")
    print()
    all_passed = all(r["passed"] for r in results)
    return 0 if all_passed else 1


def main() -> int:
    # Pytest discovers this utility because its filename ends in ``_test.py``.
    # Loading .env at import time polluted every unit test with local developer
    # presets, so only load runtime configuration when the script is executed.
    load_env_file(os.getenv("MECHUS_ENV_FILE", os.path.join(_root, ".env")))
    import argparse
    ap = argparse.ArgumentParser(description="Simulate meeting and measure Mechus performance")
    ap.add_argument("--hard", action="store_true", help="Run harder simulation (longer meeting, reversals)")
    ap.add_argument("--multi", action="store_true", help="Multi-topic meeting: pricing, API, hiring; right-context answers")
    ap.add_argument("--finance", action="store_true", help="Finance/sales call: rep, prospect, pricing, objections, competitor, next steps")
    ap.add_argument("--long", action="store_true", help="Long meeting (40+ segments): tests memory retrieval for 60-min-style context")
    ap.add_argument("--general", action="store_true", help="General voice agent: greeting, help, action items, recap, stop")
    ap.add_argument("--sports", action="store_true", help="20-min sports meeting with live Mechus call-outs (format, action items, reversals, recap)")
    ap.add_argument("--sports-stress", action="store_true", help="Sports meeting + extra reversal stress (final format decision)")
    ap.add_argument("--failure-case", action="store_true", help="Provisional vs final blur: v2 then revert to v1, then 'revisit v2 Q3' / 'aligned on v2'; must answer v1.")
    ap.add_argument("--state-completeness", action="store_true", help="Assert state contains launch/timeline decision after script with 'Decision: launch date June 15'.")
    ap.add_argument("--meeting-45min", dest="meeting_45min", action="store_true", help="45-minute meeting (~45 segments): multi-topic, decisions, action items, recap.")
    ap.add_argument("--transcript", metavar="FILE", help="Raw transcript file (Speaker: line; Mechus, question = wake-word). Blind adversarial test.")
    ap.add_argument("--all", action="store_true", help="Run all scenarios (default, hard, multi, general) and print summary")
    ap.add_argument(
        "--stress",
        action="store_true",
        help="OpenAI stress: run default + hard meeting scripts; print wall-clock seconds, p50/p95 per question, stage averages. Requires MECHUS_OPENAI_API_KEY.",
    )
    ap.add_argument(
        "--compare-latency",
        action="store_true",
        help="Run stress twice: without MECHUS_LOW_LATENCY, then with MECHUS_LOW_LATENCY=1. Implies --stress.",
    )
    args = ap.parse_args()

    if args.compare_latency or args.stress:
        return _run_stress_mode(compare_latency=args.compare_latency)

    if args.transcript:
        return _run_raw_transcript(args)

    if args.state_completeness:
        return _run_state_completeness_check()

    if args.failure_case:
        return _run_failure_case()

    if args.all:
        return _run_all_scenarios(args)

    print("=" * 60)
    if args.long:
        print("LONG MEETING (60-min style, memory retrieval test)")
    elif args.finance:
        print("FINANCE / SALES CALL")
    elif args.multi:
        print("MULTI-TOPIC MEETING (right-context answers)")
    elif args.hard:
        print("HARD SIMULATION")
    elif args.general:
        print("GENERAL VOICE AGENT (greeting, help, action items, recap, stop)")
    elif args.sports_stress:
        print("SPORTS MEETING + REVERSAL STRESS (final format decision)")
    elif args.sports:
        print("20-MIN SPORTS MEETING (live Mechus call-outs)")
    elif args.meeting_45min:
        print("45-MINUTE MEETING (multi-topic, decisions, action items, recap)")
    else:
        print("SIMULATED MEETING TEST")
    print("=" * 60)
    print()

    t_run = time.perf_counter()
    result = run_simulation(
        use_hard=args.hard,
        use_multi_topic=args.multi,
        use_finance=args.finance,
        use_long=args.long,
        use_general=args.general,
        use_sports=args.sports,
        use_sports_stress=args.sports_stress,
        use_45min=getattr(args, "meeting_45min", False),
    )
    elapsed = (time.perf_counter() - t_run) * 1000

    print("--- Meeting transcript + Mechus Q&A ---\n")
    for line in result["transcript"]:
        print(line)
    print()

    print("--- Mechus Q&A and latency ---\n")
    for idx, entry in enumerate(result["qa_log"]):
        print(f"  After line {entry['after_line']}: {entry['question']}")
        ans = entry.get("answer") or ""
        print(f"    Answer: {ans[:280]}{'...' if len(ans) > 280 else ''}")
        print(f"    Latency: router={entry.get('router_ms', 0)} ms, retrieval={entry['memory_ms']} ms, llm={entry['llm_ms']} ms, total={entry['total_ms']} ms")
        if result.get("sports") or result.get("sports_stress"):
            eids = entry.get("event_ids") or []
            path = "structured" if entry.get("structured_path") else "llm"
            print(f"    event_ids: {eids}  path: {path}")
    if result.get("sports") or result.get("sports_stress"):
        print("\n--- Wake call log (per-call metrics) ---\n")
        for idx, entry in enumerate(result["qa_log"]):
            print(f"  Call {idx + 1}: {entry['question']}")
            print(f"    router_ms={entry.get('router_ms', 0):.2f}  memory_ms={entry['memory_ms']:.2f}  llm_ms={entry['llm_ms']:.2f}  total_ms={entry['total_ms']:.2f}")
            print(f"    event_ids: {entry.get('event_ids', [])}")
            print(f"    answer: {entry.get('answer', '')[:400]}")
            print()
        print()
    print()

    quality = evaluate_quality(
        result["qa_log"],
        hard=result.get("hard", False),
        multi_topic=result.get("multi_topic", False),
        finance=result.get("finance", False),
        long=result.get("long", False),
        general=result.get("general", False),
    )
    print("--- Performance summary ---\n")
    print(f"  Responder: {result['responder']}")
    print(f"  Questions asked: {result['qa_log'].__len__()}")
    if result["latencies_ms"]:
        lat = result["latencies_ms"]
        print(f"  Latency (total per question): min={min(lat):.2f} ms, max={max(lat):.2f} ms, avg={sum(lat)/len(lat):.2f} ms")
        sorted_lat = sorted(lat)
        p95_idx = max(0, int(len(sorted_lat) * 0.95) - 1)
        print(f"  P95 latency: {sorted_lat[p95_idx]:.2f} ms")
    if result["qa_log"]:
        router_ms = [e.get("router_ms", 0) for e in result["qa_log"]]
        mem_ms = [e.get("memory_ms", 0) for e in result["qa_log"]]
        llm_ms = [e.get("llm_ms", 0) for e in result["qa_log"]]
        print(f"  Per-stage (avg): router={sum(router_ms)/len(router_ms):.2f} ms, retrieval={sum(mem_ms)/len(mem_ms):.2f} ms, llm={sum(llm_ms)/len(llm_ms):.2f} ms")
        total_llm = sum(1 for e in result["qa_log"] if e.get("llm_called"))
        print(f"  Total LLM calls: {total_llm} / {len(result['qa_log'])}")
        with_citations = sum(1 for e in result["qa_log"] if (e.get("event_ids") or []) and len(e.get("event_ids", [])) > 0)
        print(f"  Answers with event_ids (citations): {with_citations} / {len(result['qa_log'])} ({100*with_citations/max(1,len(result['qa_log'])):.0f}%)")
    print(f"  Answer quality (relevance): avg score {quality['average_score']:.2f} (0–1)")
    print(f"  Simulation run time: {elapsed / 1000.0:.3f} s ({elapsed:.0f} ms)")
    print()
    # PASS = acceptable latency AND semantic relevance (truth alignment); FAIL = relevance too low
    min_quality = 0.5
    if result.get("hard"):
        min_quality = 0.4
    passed = quality["average_score"] >= min_quality
    if passed:
        print("  Result: PASS – Latency and answer relevance acceptable.")
    else:
        print("  Result: FAIL – Answer relevance below threshold (semantic correctness).")
    print("  Tip: Set MECHUS_LLM_BACKEND=openai and MECHUS_OPENAI_API_KEY for LLM answers.")
    print()
    return 0 if passed else 1


if __name__ == "__main__":
    exit(main())
