# Live follow-up evaluation (Mechus)

Use this **after** automated tests pass (`pytest tests/`). Voice/STT adds noise; this pass confirms **user-visible** behavior.

## What to verify (each multi-turn case)

1. **Top retrieved context stays on-topic** — transcript snippets in the prompt match the thread (NIH, blockers, Sprint/H1B, etc.).
2. **Vague follow-ups stop drifting** — “what about that?”, “the first one”, “same thing for H1B?” don’t jump to unrelated meeting content.
3. **Answers feel continuous** — Mechus refers to the prior turn naturally (optional: compare to a baseline build without dialog expansion).

## Suggested conversational checks (5–10 minutes)

Work through the scenarios in [`followup_eval_cases.json`](followup_eval_cases.json), or at minimum:

| # | First utterance | Follow-up | What “good” looks like |
|---|-------------------|-----------|-------------------------|
| 1 | “What did Alice say about the NIH rollout?” | “what about that?” | Answer still about NIH / Alice / rollout; retrieval not generic. |
| 2 | “What were the three blockers?” | “tell me more about the first one” | First blocker (e.g. funding) emphasized, not a random “first”. |
| 3 | “Who owns Sprint 24 follow-ups?” | “same thing for H1B?” | H1B ownership / routing, not only Sprint 24. |

Add 2–7 more from the JSON file as time allows.

## Debug fields (during eval)

In logs or `retrieval_meta` on the response path, confirm when useful:

- `request_text` vs `retrieval_effective_query`
- `vague_followup`, `retrieval_query_expanded`
- `cache_skipped` (should often be true on vague follow-ups)
- `selected_chunk_ids` / snippet text in the prompt

## Retrieval-only smoke (no voice)

```bash
python3 scripts/smoke_followup_retrieval.py
```

Uses a fixed synthetic transcript and simulates two-turn retrieval with the same expansion path as the agent. Good pre-flight before live eval.

## Light scorecard (optional)

Per follow-up turn: **0** wrong context, **1** partial, **2** clearly right. Average across cases; compare to a baseline if you have one.

---

## Message for your team

The feature is implemented and test-clean. We validated the split between original intent text and expanded retrieval query, fixed edge-case bugs (e.g. recap time phrases vs vague heuristics), and all current automated tests pass. **Next step is a live follow-up evaluation** with real conversation cases (this checklist + `followup_eval_cases.json`) to confirm user-visible improvement before calling it fully done.
