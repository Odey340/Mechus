#!/usr/bin/env python3
"""
Terminal-only test: run the meeting simulation through the responder and print
Koov's outputs. No meeting, no audio — just feed transcript lines and see responses.

Loads .env from repo root; uses same memory + context engine + retrieval + responder
as the real agent.

Usage (run from repo root):
  python scripts/test_responder_simulation.py              # Product Launch script, full output
  python scripts/test_responder_simulation.py --chaotic   # Chaotic stress-test
  python scripts/test_responder_simulation.py --rules-only # Rules-only (no OpenAI)
  python scripts/test_responder_simulation.py -q           # Quiet: only Koov's responses
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Load .env before any koov imports that read config
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from koov.config import KoovConfig
from koov.context_integrity_engine import ContextIntegrityEngine
from koov.memory import ConversationMemory
from koov.responder import (
    HybridResponder,
    OpenAIResponder,
    ResponseInput,
    RuleResponder,
)
from koov.retrieval import retrieve_for_question

# Same script data as run_meeting_simulation.py
PRODUCT_LAUNCH_SCRIPT: list[tuple[str, str, bool]] = [
    ("Alex", "Alright everyone, thanks for joining. The goal today is to finalize the launch plan for the beta release.", False),
    ("Sarah", "Yeah, engineering is mostly done with the core features, but we still need to decide the rollout date.", False),
    ("Mike", "Koov, can you start taking notes for this meeting?", True),
    ("Alex", "Okay so first topic is the launch timeline. Originally we said mid-July.", False),
    ("Sarah", "Engineering would actually prefer July 25th. That gives us time for one more QA cycle.", False),
    ("Mike", "From marketing's side July 25th works. That gives us two weeks to prepare the campaign.", False),
    ("Alex", "Alright let's lock it in then. The beta launch date will be July 25th.", False),
    ("Sarah", "Koov, what did we just decide?", True),
    ("Mike", "I'll handle the launch announcement blog post.", False),
    ("Sarah", "Engineering will finish QA by July 20th.", False),
    ("Alex", "I'll coordinate the beta user invitations.", False),
    ("Alex", "Koov, can you list the action items so far?", True),
    ("Sarah", "One risk is that the payment integration hasn't been fully tested yet.", False),
    ("Mike", "Another concern is whether the onboarding flow is clear enough for new users.", False),
    ("Mike", "Koov, have any risks been mentioned so far?", True),
    ("Alex", "Alright quick recap before we end.", False),
    ("Alex", "Launch date July 25th, engineering finishes QA by the 20th, marketing prepares the announcement, and we monitor those two risks.", False),
    ("Sarah", "Koov, give us a quick recap of the meeting.", True),
]

CHAOTIC_SCRIPT: list[tuple[str, str, bool]] = [
    ("Alex", "So we're thinking Q3 for the—", False),
    ("Sarah", "Wait sorry, Koov, are you there?", True),
    ("Mike", "I thought we said Q2 last week.", False),
    ("Alex", "We did but then engineering said—", False),
    ("Sarah", "Koov, what did we decide about the timeline?", True),
    ("Mike", "Actually can we backtrack. Koov, what's the difference between a beta and an alpha release?", True),
    ("Alex", "So to be clear the date is June 15 now.", False),
    ("Sarah", "Yeah June 15. Koov, can you give us the action items?", True),
    ("Mike", "I'll do the blog post.", False),
    ("Alex", "And I'll do the invites. Koov, quick one — what is two plus two?", True),
    ("Sarah", "We're getting off track. Koov, recap what we decided today.", True),
]


def _extract_request(line: str, wake_phrases: tuple[str, ...]) -> str:
    """Strip 'Koov, ' / 'Koov ' and wake phrases to get the actual request."""
    text = line.strip()
    for w in wake_phrases:
        w = w.strip()
        if text.lower().startswith(w.lower() + ","):
            text = text[len(w) + 1 :].strip()
        elif text.lower().startswith(w.lower() + " "):
            text = text[len(w) + 1 :].strip()
    if text.lower().startswith("koov,"):
        text = text[5:].strip()
    elif text.lower().startswith("koov "):
        text = text[4:].strip()
    return text


def _build_responder(config: KoovConfig, rules_only: bool = False):
    rules = RuleResponder(max_response_words=config.max_response_words)
    if rules_only:
        return rules
    if config.llm_backend == "openai" and config.openai_api_key:
        openai_resp = OpenAIResponder(
            api_key=config.openai_api_key,
            model=config.openai_chat_model or "gpt-4o-mini",
            max_response_words=config.max_response_words,
            openai_max_tokens=config.response_openai_max_tokens,
            timeout_s=config.response_openai_timeout_s,
            prompt_max_chars=config.response_prompt_max_chars,
            json_mode=config.response_json_mode,
            stream_enabled=config.response_stream_enabled,
        )
        return HybridResponder(primary=openai_resp, fallback=rules)
    return rules


def run_simulation(
    script: list[tuple[str, str, bool]],
    config: KoovConfig,
    use_context_engine: bool = True,
    verbose: bool = True,
    rules_only: bool = False,
) -> None:
    memory = ConversationMemory(
        max_chars=config.memory_max_chars,
        recent_chars=config.memory_recent_chars,
    )
    engine: ContextIntegrityEngine | None = None
    if use_context_engine:
        engine = ContextIntegrityEngine(meeting_id="sim")

    responder = _build_responder(config, rules_only=rules_only)
    wake_phrases = config.wake_phrases or (config.wake_phrase,)

    print()
    print("=" * 70)
    print("  Responder simulation (no meeting, no audio)")
    print("  LLM backend:", "rules only" if rules_only else config.llm_backend)
    print("=" * 70)
    print()

    for i, (speaker, line, is_koov_question) in enumerate(script, 1):
        # Add this line to meeting context
        segment = f"{speaker}: {line}"
        memory.add_segment(segment)
        if engine is not None:
            _mu, _end, _tagged = engine.ingest_chunk(line.strip(), speaker_id=speaker, timestamp=time.time())

        if verbose:
            print(f"  [{i}] {speaker}: {line}")

        if not is_koov_question:
            if verbose:
                print()
            continue

        request = _extract_request(line, wake_phrases)
        if not request:
            request = line

        bundle = retrieve_for_question(
            request,
            context_engine=engine,
            memory=memory,
            recent_transcript="",
            max_chars=config.retrieval_max_chars,
            general_max_chars=config.retrieval_general_max_chars,
        )
        inp = ResponseInput(
            wake_phrase=config.wake_phrase,
            wake_phrases=wake_phrases,
            request_text=request,
            context_text=bundle.formatted_context,
            memory_text=bundle.memory_text,
            context_chunks=bundle.chunks,
            intent=bundle.intent,
            state=bundle.state,
            retrieval_meta=bundle.meta,
        )
        response = responder.respond(inp).strip()

        print()
        print("  ---> Koov:")
        if response:
            print(f"      {response}")
        else:
            print("      (no response)")
        print()

    print("=" * 70)
    print("  Simulation complete.")
    print("=" * 70)
    print()


def main() -> None:
    # Keep module import side-effect free: pytest discovers this utility because
    # of its filename, and importing it must not inject a developer .env into the
    # entire test process.
    env_path = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(env_path):
        from koov.envfile import load_env_file

        load_env_file(env_path)
    parser = argparse.ArgumentParser(
        description="Test responder with meeting simulation (terminal only, no meeting)."
    )
    parser.add_argument(
        "--chaotic",
        action="store_true",
        help="Use chaotic / stress-test script.",
    )
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Force rules-only responder (no OpenAI).",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only print Koov's responses, not the script lines.",
    )
    args = parser.parse_args()

    config = KoovConfig.from_env()
    script = CHAOTIC_SCRIPT if args.chaotic else PRODUCT_LAUNCH_SCRIPT
    run_simulation(
        script,
        config,
        use_context_engine=True,
        verbose=not args.quiet,
        rules_only=args.rules_only,
    )


if __name__ == "__main__":
    main()
