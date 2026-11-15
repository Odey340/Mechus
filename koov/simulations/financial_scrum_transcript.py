"""
Shared utterances for the Q2 Sprint 23 financial scrum (same narrative as test_financial_scrum.py).

Used by scripts/simulate_financial_scrum_koov.py (production ContextIntegrityEngine path).

`build_financial_scrum_timeline()` interleaves transcript lines with Koov questions like a real meeting:
people speak → someone asks Koov → conversation continues → … → time-window recaps
("last 5 minutes" / "last 10 minutes") → capstone question spanning
the full meeting (early velocity/burn + late decisions/dates).
"""

from __future__ import annotations

from typing import Literal, Union

# One timeline step: section heading, spoken line, or Koov question
FinancialScrumTimelineItem = Union[
    tuple[Literal["section"], str],
    tuple[Literal["utterance"], str, str],
    tuple[Literal["koov"], str, str],
]

# (speaker_key, line_text) — keys map to display names in the simulator
FINANCIAL_SCRUM_UTTERANCES: list[tuple[str, str]] = [
    ("sarah", "Okay everyone, let's kick off the Sprint 23 financial scrum. Quick reminder: our Q2 budget cap is 1.2 million dollars and we are currently in week 8 of 13."),
    ("sarah", "Sprint 23 ran from April 1st to April 14th. We planned 84 story points and the team completed 71. That's an 84.5 percent completion rate."),
    ("mike", "Yeah, the shortfall was mostly the payment gateway integration. It ballooned from 13 points to 21 points mid-sprint. We underestimated the PCI-DSS compliance overhead."),
    ("jake", "Understood. For context, the payment gateway feature is tied to the May 15th go-live. If we slip that, we lose the merchant agreement which is worth 340,000 dollars in committed revenue."),
    ("priya", "From a finance perspective, Sprint 23 burned 112,000 dollars against a sprint budget of 98,000. We're 14,000 over budget. Year-to-date we've spent 487,000 of the 1.2 million, so we're at 40.6 percent budget consumed with 61.5 percent of the quarter remaining."),
    ("lisa", "QA flagged 18 bugs in sprint 23. 4 are critical, 9 are major, and 5 are minor. The 4 critical bugs are all in the payment module and need to be resolved before May 15th."),
    ("priya", "Let me walk through the Q2 cost breakdown. Engineering labor is 61 percent of spend, that's 297,000 dollars. Cloud infrastructure is running at 23 percent, so about 112,000 dollars — which is 18,000 over our cloud forecast because of the load testing environments we spun up."),
    ("priya", "Third-party licenses are 9 percent, around 44,000 dollars. The remaining 7 percent, about 34,000 dollars, is split between QA tooling and security audits. The PCI-DSS audit alone was 22,000 dollars which was unplanned."),
    ("mike", "The cloud overage — can we shut down some of those load testing environments? We've completed 3 of the 5 planned load tests."),
    ("priya", "Yes. If we decommission those 3 envs this week we save roughly 6,000 dollars per week. Over the remaining 5 weeks of Q2 that's 30,000 in savings."),
    ("jake", "Let's do it. Mike, can you own that? Decommission the completed load test environments by end of week."),
    ("mike", "Sure, I'll have them down by Friday April 12th."),
    ("sarah", "Great. That's a decision — we're decommissioning load test environments by April 12th to recover 30,000 dollars."),
    ("sarah", "Moving to the risk register. We have 3 active risks this sprint."),
    ("jake", "Risk 1: Payment gateway delay. Probability high, impact critical. If we don't hit May 15th we lose the 340,000 dollar merchant agreement. Mitigation: pull 2 senior engineers from the reporting module to focus on payment."),
    ("mike", "Risk 2: Engineering capacity. Dev is at 94 percent utilization this week. We have one engineer out sick and another on a 3-day conference April 16th through 18th. If the sick leave extends past Wednesday, Sprint 24 velocity will drop by an estimated 15 story points."),
    ("priya", "Risk 3: Budget overrun. At the current burn rate of 14,000 per sprint over budget, we project a 70,000 dollar overrun by end of Q2. That would put us at 1.27 million against the 1.2 million cap — 70,000 in the red."),
    ("lisa", "Blocker: The 4 critical payment bugs can't be fixed until the payment gateway vendor provides an updated SDK. We've been waiting since April 8th. No ETA from the vendor yet."),
    ("jake", "I'll escalate to the vendor account manager today. I need an SDK delivery commitment by April 14th or we need to find an alternative."),
    ("sarah", "Agreed. Jake owns the vendor escalation and needs a response by April 14th."),
    ("sarah", "Sprint 24 runs April 15th through April 28th. Based on our adjusted velocity we're planning 75 story points, down from the original 84, to account for the capacity constraints."),
    ("jake", "Top priorities for Sprint 24: first, complete the payment gateway integration — that's 21 remaining story points. Second, fix the 4 critical and 9 major bugs, which QA estimates at 18 points. Third, begin the reporting module at 20 story points. That's 59 points of committed work, leaving 16 points for buffer."),
    ("mike", "I want to flag that moving 2 engineers off reporting to payment means the reporting module will slip by one sprint. Instead of delivering in Sprint 24 it'll be Sprint 25."),
    ("jake", "I accept that trade-off. Payment is the revenue risk. Reporting can slip to Sprint 25 ending May 12th. But no further — we have enterprise demos scheduled for May 19th."),
    ("priya", "Sprint 24 budget is set at 95,000 dollars. Given the overage last sprint I'm adding a mid-sprint checkpoint — if we hit 50,000 spend by April 21st we regroup."),
    ("lisa", "QA will need 3 additional days of testing for the payment module because of PCI-DSS sign-off. That moves our QA complete date from April 25th to April 28th — which is the last day of the sprint. Zero buffer."),
    ("sarah", "Let's lock the go-live milestones. Payment gateway code complete: April 28th end of Sprint 24. PCI-DSS QA sign-off: April 28th. Staging deployment and smoke test: April 30th."),
    ("jake", "Production deployment: May 5th. Merchant onboarding begins May 6th. Full go-live May 15th. That's the hard deadline because the merchant agreement auto-renews on May 16th."),
    ("mike", "May 5th production deployment is aggressive. We need infrastructure ready by May 3rd. That means the devops team needs to start environment prep by April 24th. I'll assign that to Carlos."),
    ("lisa", "I need a code freeze by April 26th to have enough time for regression testing before the May 5th deploy. If there's any code committed after April 26th it resets my regression clock."),
    ("jake", "Code freeze April 26th is locked. Any post-freeze changes need my explicit sign-off and Lisa's approval. That's a team decision."),
    ("priya", "The 340,000 dollar merchant revenue posts to Q2 books on May 16th. If we miss that date it rolls to Q3, which affects our Q2 financial close. The CFO has visibility on this milestone."),
    ("sarah", "Let's confirm all decisions before we close. One: we reallocate 2 engineers from reporting to payment gateway immediately."),
    ("jake", "Two: reporting module slips to Sprint 25, completing May 12th. Enterprise demos on May 19th are unaffected."),
    ("priya", "Three: cloud load test decommission by April 12th, saving 30,000 dollars. Four: Sprint 24 budget cap is 95,000 dollars with a mid-sprint check at 50,000."),
    ("mike", "Five: code freeze April 26th. Any exceptions need Jake and Lisa sign-off."),
    ("sarah", "Six: Jake escalates to payment vendor by today and needs SDK commitment by April 14th. Seven: Carlos begins infra prep by April 24th for May 5th production deploy."),
    ("lisa", "And QA PCI-DSS sign-off is due April 28th. If we miss that, May 5th deploy slides and we risk the May 15th go-live."),
]

# Time-window recaps (digits required for koov.retrieval._extract_time_window_minutes)
FINANCIAL_SCRUM_WINDOW_RECAP_5M: tuple[str, str] = (
    "window-recap-5m",
    "Koov, can you recap the last 5 minutes?",
)
FINANCIAL_SCRUM_WINDOW_RECAP_10M: tuple[str, str] = (
    "window-recap-10m",
    "Can you recap the last 10 minutes?",
)

# (label, question) — same order as test_financial_scrum Koov questions
FINANCIAL_SCRUM_KOOV_QUESTIONS: list[tuple[str, str]] = [
    ("mid-1-budget", "What is our current budget status?"),
    ("mid-2-cloud", "What did we decide about cloud costs?"),
    ("mid-3-risks", "What are the active risks and their financial impact?"),
    ("mid-4-sprint24", "What story points and budget are committed for Sprint 24?"),
    ("mid-5-actions", "What action items have been assigned so far and who owns them?"),
    ("mid-6-golive", "What are the key go-live dates and who is responsible for each?"),
    ("final-recap-1", "Can you give me a full recap of this meeting?"),
    ("final-recap-2", "What are all the action items and who owns them?"),
    ("final-recap-3", "What are the key financial numbers from today?"),
    ("final-recap-4", "What are all the critical dates and deadlines?"),
    ("final-recap-5", "What risks were identified and what are the mitigation plans?"),
    FINANCIAL_SCRUM_WINDOW_RECAP_5M,
    FINANCIAL_SCRUM_WINDOW_RECAP_10M,
]

# Requires retrieval across the whole meeting (Sprint 23 stats from the opening + final decisions/dates)
FULL_MEETING_CAPSTONE_QUESTION: tuple[str, str] = (
    "capstone-full-meeting",
    (
        "This question needs everything from the start of the call through the wrap-up: "
        "We opened with Sprint 23 velocity — 84 points planned, 71 completed — and the 112,000 dollar burn "
        "versus 98,000 sprint budget. By the end we locked code freeze April 26th, May 15th go-live, "
        "and vendor escalation deadlines. "
        "How do those early Sprint 23 numbers and the payment-gateway risk connect to the final "
        "timeline and budget decisions we made at the close?"
    ),
)


def build_financial_scrum_timeline() -> list[FinancialScrumTimelineItem]:
    """
    Interleaved meeting: dialogue segments, Koov interruptions, then a capstone that
    explicitly demands early + late context (placed after all transcript is in the engine).
    """
    U = FINANCIAL_SCRUM_UTTERANCES
    t: list[FinancialScrumTimelineItem] = []

    def add_utterances(start: int, end: int) -> None:
        for i in range(start, end):
            t.append(("utterance", U[i][0], U[i][1]))

    t.append(("section", "SPRINT 23 VELOCITY REVIEW"))
    add_utterances(0, 6)
    t.append(("koov",) + FINANCIAL_SCRUM_KOOV_QUESTIONS[0])

    t.append(("section", "Q2 COST BREAKDOWN"))
    add_utterances(6, 13)
    t.append(("koov",) + FINANCIAL_SCRUM_KOOV_QUESTIONS[1])

    t.append(("section", "RISK REGISTER & BLOCKERS"))
    add_utterances(13, 20)
    t.append(("koov",) + FINANCIAL_SCRUM_KOOV_QUESTIONS[2])

    t.append(("section", "SPRINT 24 PLANNING"))
    add_utterances(20, 26)
    t.append(("koov",) + FINANCIAL_SCRUM_KOOV_QUESTIONS[3])
    t.append(("koov",) + FINANCIAL_SCRUM_KOOV_QUESTIONS[4])

    t.append(("section", "GO-LIVE TIMELINE REVIEW"))
    add_utterances(26, 32)
    t.append(("koov",) + FINANCIAL_SCRUM_WINDOW_RECAP_5M)
    t.append(("koov",) + FINANCIAL_SCRUM_KOOV_QUESTIONS[5])

    t.append(("section", "FINAL DECISIONS"))
    add_utterances(32, len(U))
    t.append(("koov",) + FINANCIAL_SCRUM_WINDOW_RECAP_10M)

    t.append(("section", "KOOV — FULL-MEETING QUESTION (EARLY + LATE CONTEXT)"))
    t.append(("koov",) + FULL_MEETING_CAPSTONE_QUESTION)

    t.append(("section", "FINAL RECAP QUESTIONS"))
    for q in FINANCIAL_SCRUM_KOOV_QUESTIONS[6:11]:
        t.append(("koov",) + q)

    return t

SPEAKER_LABELS: dict[str, str] = {
    "sarah": "Sarah (Scrum Master)",
    "mike": "Mike (Dev Lead)",
    "priya": "Priya (Finance Lead)",
    "jake": "Jake (Product Owner)",
    "lisa": "Lisa (QA Lead)",
}
