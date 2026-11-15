#!/usr/bin/env python3
"""
Test Koov real-time meeting Q&A: meeting context + optional web search.

Simulates:
1. Pre-load meeting transcript into memory
2. Ask a question as if user said "Koov, ..."
3. Measure latency: memory retrieval, web search (if used), LLM response
4. Print answer quality and full transcript at end

Run with LLM (OpenAI) for full answer quality; set KOOV_LLM_BACKEND=openai and KOOV_OPENAI_API_KEY.
Usage:
  python3 scripts/test_koov_meeting_qa.py
  python3 scripts/test_koov_meeting_qa.py --question "What did we decide about the API?"
  python3 scripts/test_koov_meeting_qa.py --web "What is UGC?"
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Project root
_script_dir = os.path.dirname(os.path.abspath(__file__))
_root = os.path.dirname(_script_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

from koov.config import KoovConfig
from koov.memory import ConversationMemory
from koov.responder import OpenAIResponder, ResponseInput, RuleResponder
from koov.text import strip_wake_word

# Sample meeting transcript (pre-loaded into memory)
SAMPLE_MEETING = [
    "Alice: Let's decide on the Q2 launch. I propose June 15.",
    "Bob: I agree with June 15. Alice will finalize release notes by June 1.",
    "Carol: Open question who owns marketing rollout. We need to figure out the budget.",
    "Alice: Decision we have decided to use the existing API, v2 endpoint.",
    "Bob: Risk we're worried about dependency on the legacy system. Latency could spike.",
    "Carol: Assuming we get sign-off from legal by next Friday. No change to the timeline.",
    "Alice: Actually we should not use v2. Let's revert to v1 for now.",
    "Bob: So the final decision is we stay on v1. Carol will own marketing rollout, due by end of May.",
    "Carol: Target 10% conversion. Goal is 50K users by launch.",
]

DEFAULT_QUESTION = "What did we decide about the API?"
WEB_QUESTION = "What is UGC in marketing?"


def run_test(question: str, use_web: bool = False) -> dict:
    """Run one Q&A test. If use_web, question is changed to a web-style question."""
    config = KoovConfig.from_env()
    memory = ConversationMemory(max_chars=config.memory_max_chars, recent_chars=config.memory_recent_chars)
    for line in SAMPLE_MEETING:
        memory.add_segment(line)

    request_text = question.strip()
    if not request_text.lower().startswith("koov"):
        request_text = f"Koov {request_text}"
    request_text = strip_wake_word(request_text, config.wake_phrase).strip() or question.strip()

    # Latency: memory retrieval
    t0 = time.perf_counter()
    memory_text = memory.retrieve(request_text=request_text)
    t_memory = (time.perf_counter() - t0) * 1000

    context_text = ""

    if config.llm_backend == "openai" and config.openai_api_key:
        responder = OpenAIResponder(
            api_key=config.openai_api_key,
            model=config.openai_chat_model or "gpt-4o-mini",
            max_response_words=config.max_response_words,
        )
    else:
        responder = RuleResponder(max_response_words=config.max_response_words)

    inp = ResponseInput(
        wake_phrase=config.wake_phrase,
        request_text=request_text,
        context_text=context_text,
        memory_text=memory_text,
    )
    t_llm0 = time.perf_counter()
    response_text = responder.respond(inp)
    t_llm = (time.perf_counter() - t_llm0) * 1000

    total_ms = t_memory + t_llm
    return {
        "question": question,
        "request_text": request_text,
        "answer": response_text,
        "latency_ms": {
            "memory_ms": round(t_memory, 1),
            "llm_ms": round(t_llm, 1),
            "total_ms": round(total_ms, 1),
        },
        "memory_len": len(memory_text),
        "transcript": SAMPLE_MEETING + [f"User (Koov): {request_text}", f"Koov: {response_text}"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Test Koov meeting Q&A and latency")
    ap.add_argument("--question", "-q", default=DEFAULT_QUESTION, help="Question to ask (as if after 'Koov')")
    ap.add_argument("--web", action="store_true", help="Force a web-backed question (e.g. 'What is X?')")
    args = ap.parse_args()
    question = args.question
    if args.web:
        question = WEB_QUESTION

    print("=== Koov meeting Q&A test ===\n")
    print(f"Question: {question}\n")
    result = run_test(question, use_web=args.web)
    print("Answer:", result["answer"] or "(no response)")
    print("\n--- Latency (ms) ---")
    for k, v in result["latency_ms"].items():
        print(f"  {k}: {v}")
    print(f"\nMemory context length: {result['memory_len']} chars")
    print("\n--- Transcript at end of meeting ---")
    for line in result["transcript"]:
        print(line)
    print("\n--- Summary ---")
    print(f"Total response time: {result['latency_ms']['total_ms']} ms")
    print("With real audio: Wake → STT → Memory → LLM → TTS. Latency is in GET /api/status?meeting_id=... as 'latency'.")
    print("For better answer quality, set KOOV_LLM_BACKEND=openai and KOOV_OPENAI_API_KEY in .env.")
    return 0


if __name__ == "__main__":
    exit(main())
