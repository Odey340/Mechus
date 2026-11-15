"""Meeting transcript wake: suppress promo instant replies; follow-up window helpers."""

from __future__ import annotations

import unittest

from mechus.command_router import _HELP_REPLIES
from mechus.dialog_memory import (
    is_interrupt_request,
    is_lone_proper_name,
    is_repeat_request,
    is_transcript_backchannel,
    qualifies_as_transcript_followup_without_wake,
)
from mechus.wake_detector import (
    detect_wake,
    is_false_address_use,
    is_incomplete_transcript_command,
    is_meta_wake_discussion,
    is_product_mention_use,
    normalize_for_wake,
    preamble_before_wake_last,
    qualifies_as_wake_command,
    qualifies_as_directed_transcript_wake,
    should_suppress_transcript_wake,
    line_has_explicit_wake_token,
    strip_wake_phrase_last,
)
from mechus.intent_classifier import (
    INTENT_GENERAL,
    INTENT_GREETING,
    INTENT_HELP,
    INTENT_PRESENCE_CHECK,
    INTENT_RECAP,
    INTENT_STOP,
    classify_pipeline_intent,
    _is_presence_check,
)
_WAKE = ("hey computer", "computer", "mechus")


def _qualifies_as_transcript_followup(raw_speech: str) -> bool:
    cmd = (raw_speech or "").strip()
    norm = normalize_for_wake(cmd)
    if detect_wake(norm, _WAKE, max_dist=1):
        return False
    return qualifies_as_transcript_followup_without_wake(cmd)


class TestMeetingIntentGuards(unittest.TestCase):
    def test_participant_i_hear_you_not_presence(self) -> None:
        self.assertFalse(_is_presence_check("yeah i hear you on that"))

    def test_help_intent_after_wake_strip(self) -> None:
        n = normalize_for_wake("computer what can you do")
        cmd = strip_wake_phrase_last(n, _WAKE)
        self.assertEqual(classify_pipeline_intent(cmd), INTENT_HELP)

    def test_help_instant_replies_include_recap_promo(self) -> None:
        self.assertTrue(any("recap" in r.lower() for r in _HELP_REPLIES))


class TestTranscriptFollowupHelpers(unittest.TestCase):
    def test_qualifies_vague_followup_without_wake(self) -> None:
        self.assertTrue(_qualifies_as_transcript_followup("what about the timeline?"))

    def test_rejects_line_with_wake_phrase(self) -> None:
        self.assertFalse(
            _qualifies_as_transcript_followup("computer what about the timeline?")
        )


class TestMeetingActionOnly(unittest.TestCase):
    def test_hear_me_classifies_as_presence(self) -> None:
        self.assertTrue(_is_presence_check("can you hear me"))
        self.assertEqual(classify_pipeline_intent("can you hear me"), INTENT_PRESENCE_CHECK)

    def test_hear_me_after_computer_wake_qualifies(self) -> None:
        merged = normalize_for_wake("computer can you hear me")
        self.assertTrue(
            qualifies_as_wake_command(merged, _WAKE, max_dist=2, max_leading_words_before_wake=2)
        )

    def test_recap_is_action(self) -> None:
        self.assertEqual(
            classify_pipeline_intent("summarize what we decided"),
            INTENT_RECAP,
        )


class TestTranscriptWakeMerge(unittest.TestCase):
    def test_name_planets_qualifies_as_wake_command(self) -> None:
        merged = normalize_for_wake(
            "computer name all the eight planets in the solar system"
        )
        self.assertTrue(
            qualifies_as_wake_command(merged, _WAKE, max_dist=2, max_leading_words_before_wake=2)
        )

    def test_split_chunks_merge_to_wake_command(self) -> None:
        part1 = normalize_for_wake("hey")
        part2 = normalize_for_wake("computer what are the action items")
        merged = normalize_for_wake("hey computer what are the action items")
        self.assertFalse(
            qualifies_as_wake_command(part1, _WAKE, max_dist=2, max_leading_words_before_wake=2)
        )
        self.assertTrue(
            qualifies_as_wake_command(merged, _WAKE, max_dist=2, max_leading_words_before_wake=2)
        )

    def test_interjection_line_alone_not_directed_wake(self) -> None:
        """Without VAD-merge into a prior 'computer' fragment, filler must not wake."""
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake("oh yes and uh like um"),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=8,
            )
        )


class TestIncompleteTranscriptCommands(unittest.TestCase):
    def test_bare_can_you_is_incomplete(self) -> None:
        self.assertTrue(is_incomplete_transcript_command("can you"))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake("hey mechus can you"),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=4,
            )
        )

    def test_can_you_hear_me_still_qualifies(self) -> None:
        self.assertFalse(is_incomplete_transcript_command("can you hear me"))
        self.assertTrue(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake("mechus can you hear me"),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=2,
            )
        )

    def test_tell_me_a_little_bit_more_is_incomplete(self) -> None:
        self.assertTrue(is_incomplete_transcript_command("can you tell me a little bit more"))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake("hey mekkus can you tell me a little bit more"),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=4,
            )
        )

    def test_full_question_still_qualifies(self) -> None:
        self.assertFalse(
            is_incomplete_transcript_command(
                "who is arlong the 17 year old boy who made it into y combinator"
            )
        )

    def test_who_won_the_is_incomplete_fragment(self) -> None:
        self.assertTrue(is_incomplete_transcript_command("who won the"))
        self.assertFalse(is_incomplete_transcript_command("who won the nba game today"))


class TestRepeatRequest(unittest.TestCase):
    def test_repeat_phrases_detected(self) -> None:
        self.assertTrue(is_repeat_request("can you repeat yourself"))
        self.assertTrue(is_repeat_request("say that again"))
        self.assertTrue(is_repeat_request("what did you say"))

    def test_repeat_not_generic_question(self) -> None:
        self.assertFalse(is_repeat_request("name the eight planets"))

    def test_followup_qualifies_repeat_without_wake(self) -> None:
        self.assertTrue(_qualifies_as_transcript_followup("repeat that"))

    def test_summarize_meeting_requires_wake(self) -> None:
        from mechus.dialog_memory import qualifies_as_transcript_followup_without_wake

        self.assertFalse(qualifies_as_transcript_followup_without_wake("summarize the meeting"))
        self.assertFalse(_qualifies_as_transcript_followup("summarize the meeting"))

    def test_what_are_those_not_wakeless_followup(self) -> None:
        from mechus.dialog_memory import qualifies_as_transcript_followup_without_wake

        self.assertFalse(qualifies_as_transcript_followup_without_wake("what are those"))
        self.assertFalse(
            qualifies_as_transcript_followup_without_wake(
                "what are those things called on the periodic table"
            )
        )

    def test_repeat_with_wake_qualifies_directed(self) -> None:
        self.assertTrue(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake("hey mechus can you repeat that"),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=4,
            )
        )

    def test_interrupt_not_followup(self) -> None:
        self.assertTrue(is_interrupt_request("shut up"))
        self.assertTrue(is_interrupt_request("two stop"))
        self.assertTrue(is_interrupt_request("no wait"))
        self.assertTrue(is_interrupt_request("that's wrong"))
        self.assertTrue(is_interrupt_request("wrong"))
        self.assertFalse(
            qualifies_as_transcript_followup_without_wake(
                "shut up why is it taking so long"
            )
        )
        self.assertFalse(
            qualifies_as_transcript_followup_without_wake(
                "why does it sound lazy okay"
            )
        )
        self.assertEqual(
            classify_pipeline_intent("shut up why is it taking so long"),
            INTENT_STOP,
        )

    def test_talk_to_me_about_incomplete(self) -> None:
        self.assertTrue(is_incomplete_transcript_command("talk to me about"))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake("mechus talk to me about"),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=2,
            )
        )

    def test_single_name_not_followup(self) -> None:
        self.assertFalse(_qualifies_as_transcript_followup("Jeremy"))

    def test_backchannel_not_followup(self) -> None:
        self.assertFalse(_qualifies_as_transcript_followup("Mm-hmm"))

    def test_stt_noise_annotation_not_followup(self) -> None:
        self.assertFalse(_qualifies_as_transcript_followup("[water running]"))
        self.assertFalse(_qualifies_as_transcript_followup("[squeaking]"))

    def test_meeting_banter_does_not_followup(self) -> None:
        for line in (
            "It's good",
            "it's dead",
            "That's great",
            "sounds good",
            "yeah it's good",
            "Is that fast enough?",
            "Stop being mad at me",
        ):
            self.assertFalse(
                _qualifies_as_transcript_followup(line),
                msg=line,
            )

    def test_generic_room_questions_not_followup(self) -> None:
        """Humans asking each other must not wake Mechus without the wake phrase."""
        for line in (
            "what's the deadline for the project?",
            "how do we deploy to production?",
            "can anyone share the doc?",
            "what is the largest planet?",
        ):
            self.assertFalse(
                _qualifies_as_transcript_followup(line),
                msg=line,
            )

    def test_anaphoric_wh_still_followup(self) -> None:
        self.assertTrue(_qualifies_as_transcript_followup("how big is it?"))
        self.assertTrue(_qualifies_as_transcript_followup("what about that?"))

    def test_directed_tell_me_followup_after_mechus(self) -> None:
        self.assertTrue(
            _qualifies_as_transcript_followup("tell me about the solar system")
        )


class TestFalseAddressGuard(unittest.TestCase):
    def test_device_mentions_suppressed(self) -> None:
        for line in (
            "my computer keeps freezing",
            "the computer is so slow today",
            "his computer crashed again",
            "i need to fix my computer",
            "we use the computer for billing",
            "computer science homework is killing me",
        ):
            self.assertTrue(
                is_false_address_use(normalize_for_wake(line)),
                msg=line,
            )

    def test_real_address_not_suppressed(self) -> None:
        for line in (
            "computer can you hear me",
            "hey computer what are the eight planets",
            "computer summarize the meeting",
        ):
            self.assertFalse(
                is_false_address_use(normalize_for_wake(line)),
                msg=line,
            )

    def test_leading_words_strict_for_mid_utterance(self) -> None:
        # Long preamble followed by "computer help" should NOT count as wake (max_leading=1)
        n = normalize_for_wake("well anyway about that thing computer help me")
        self.assertFalse(
            qualifies_as_wake_command(n, _WAKE, max_dist=1, max_leading_words_before_wake=1)
        )

    def test_short_preamble_still_qualifies(self) -> None:
        n = normalize_for_wake("hey computer summarize the meeting")
        self.assertTrue(
            qualifies_as_wake_command(n, _WAKE, max_dist=1, max_leading_words_before_wake=1)
        )

    def test_long_preamble_before_computer_qualifies_with_meeting_default(self) -> None:
        n = normalize_for_wake(
            "all right that works computer what is the weather in delhi india"
        )
        self.assertTrue(
            qualifies_as_wake_command(n, _WAKE, max_dist=1, max_leading_words_before_wake=10)
        )
        cmd = strip_wake_phrase_last(n, _WAKE, max_dist=1)
        self.assertIn("weather", cmd)
        self.assertIn("delhi", cmd)

    def test_preamble_before_wake_last_extracts_prefix(self) -> None:
        n = normalize_for_wake(
            "all right that works computer what is the weather in delhi india"
        )
        self.assertEqual(
            preamble_before_wake_last(n, _WAKE, max_dist=1),
            "all right that works",
        )


class TestTranscriptWakeQualifyScope(unittest.TestCase):
    def test_long_same_speaker_preamble_does_not_push_wake_past_max_leading(self) -> None:
        """Wake must be detected on the current line even if combined text has a long preamble."""
        line = "computer do you think gautam would be able to climb mount everest"
        preamble = " ".join(["filler"] * 12)
        combined = f"{preamble} {line}"
        self.assertFalse(
            qualifies_as_wake_command(
                normalize_for_wake(combined),
                _WAKE,
                max_dist=1,
                max_leading_words_before_wake=10,
            )
        )
        self.assertTrue(
            qualifies_as_wake_command(
                normalize_for_wake(line),
                _WAKE,
                max_dist=1,
                max_leading_words_before_wake=10,
            )
        )


class TestMeetingActionWakeAllowed(unittest.TestCase):
    """Mirror of MechusAgent._meeting_action_wake_allowed to lock in 'when to talk' rules."""

    HESITATION = frozenset({
        "wait", "hold on", "hang on", "one sec", "one second", "one moment",
        "let me think", "let me check", "let me see", "give me a sec",
        "give me a second", "give me a moment", "um", "uh", "uhm", "hmm",
        "okay", "ok", "alright", "right", "yeah", "yep", "sure", "well", "so",
        "anyway", "anyways", "thanks", "thank you", "ah", "oh",
        "oh okay", "oh right",
    })
    SHORT_ACTIONS = ("recap", "summary", "summarize", "stop", "decisions",
                     "decision", "actions", "agenda", "risks", "timeline")

    @classmethod
    def _allow(cls, line: str) -> bool:
        import re as _re

        n = normalize_for_wake(line)
        if not detect_wake(n, _WAKE, max_dist=1):
            return False
        remainder = strip_wake_phrase_last(n, _WAKE, max_dist=1)
        rem = remainder.strip()
        if not rem:
            return False
        low = _re.sub(r"\s+", " ", rem.lower()).strip(" .,!?")
        intent = classify_pipeline_intent(remainder)
        if intent == INTENT_STOP:
            return True
        if low in cls.HESITATION:
            return False
        substantive = [t for t in _re.split(r"\s+", low) if len(t) >= 2]
        if not substantive:
            return False
        if _is_presence_check(remainder):
            return True
        if len(substantive) < 2 and low not in cls.SHORT_ACTIONS:
            return False
        non_action = {INTENT_GREETING, INTENT_HELP, "unknown", "clarification", "confirm"}
        return intent not in non_action

    def test_hear_me_allowed(self) -> None:
        self.assertTrue(self._allow("computer can you hear me"))

    def test_bare_wake_only_blocked(self) -> None:
        for line in ("mechus", "hey mechus", "Mechus", "Hey mechus"):
            self.assertFalse(self._allow(line), msg=line)

    def test_real_question_allowed(self) -> None:
        self.assertTrue(self._allow("computer what is the capital of France"))

    def test_short_action_keyword_allowed(self) -> None:
        self.assertTrue(self._allow("computer recap"))

    def test_hesitation_blocked(self) -> None:
        for line in ("computer um",):
            self.assertFalse(self._allow(line), msg=line)

    def test_hold_on_routes_as_stop_directive(self) -> None:
        for line in ("computer wait", "computer hold on", "computer one sec"):
            self.assertEqual(
                classify_pipeline_intent(strip_wake_phrase_last(normalize_for_wake(line), _WAKE, max_dist=1)),
                INTENT_STOP,
            )
            self.assertTrue(self._allow(line), msg=line)

    def test_one_word_followup_blocked(self) -> None:
        self.assertFalse(self._allow("computer maybe"))
        self.assertFalse(self._allow("computer something"))


class TestTranscriptWakeSttAliases(unittest.TestCase):
    _Mechus_ONLY = ("mechus",)

    def test_kuv_detects_mechus_wake(self) -> None:
        n = normalize_for_wake("mekkus can you recap the meeting")
        self.assertTrue(detect_wake(n, _WAKE, max_dist=2))
        # Production uses MAX_DIST=1; mekkus is 2 fuzzy edits from mechus — alias path.
        self.assertTrue(detect_wake(n, ("mechus",), max_dist=1))

    def test_hoov_detects_mechus_wake(self) -> None:
        n = normalize_for_wake("mechis can you hear me")
        self.assertTrue(detect_wake(n, _WAKE, max_dist=2))
        self.assertTrue(detect_wake(n, ("mechus",), max_dist=1))

    def test_common_stt_mishears_qualify_with_mechus_only_phrase(self) -> None:
        """Aliases + fuzzy matcher — no extra tokens in MECHUS_WAKE_PHRASE."""
        for raw in (
            "meckus can you recap the meeting",
            "mekus what did we decide",
            "mekkus summarize action items",
            "mechis can you hear me",
        ):
            n = normalize_for_wake(raw)
            with self.subTest(raw=raw):
                self.assertTrue(
                    qualifies_as_directed_transcript_wake(
                        n, self._Mechus_ONLY, max_dist=2, max_leading_words_before_wake=10
                    ),
                    msg=raw,
                )

    def test_kuv_qualifies_at_production_max_dist_one(self) -> None:
        """Regression: Zoom STT 'mekkus' must wake at MECHUS_WAKE_TRANSCRIPT_MAX_DIST=1."""
        n = normalize_for_wake("hey mekkus whats the weather in india right now")
        self.assertTrue(
            qualifies_as_directed_transcript_wake(
                n, self._Mechus_ONLY, max_dist=1, max_leading_words_before_wake=10
            )
        )
        self.assertTrue(line_has_explicit_wake_token(n, self._Mechus_ONLY, max_dist=1))

    def test_opinion_question_qualifies_after_computer(self) -> None:
        n = normalize_for_wake("computer whats your opinion on drake")
        self.assertTrue(
            qualifies_as_wake_command(
                n, _WAKE, max_dist=2, max_leading_words_before_wake=10
            )
        )


class TestPitchProductMentionGuard(unittest.TestCase):
    def test_pitch_lines_are_product_mentions(self) -> None:
        for line in (
            "and these is allows for mechus to be the intelligence layer for every meeting that you join",
            "does does mechus give you right it can help you manage your multi speaker flow",
            "advantage that mechus gives you as being efficient",
        ):
            self.assertTrue(is_product_mention_use(normalize_for_wake(line)), msg=line)

    def test_pitch_does_not_qualify_directed_wake(self) -> None:
        for line in (
            "and these is allows for mechus to be the intelligence layer for every meeting that you join",
            "does does mechus give you right it can help you manage your multi speaker flow",
        ):
            self.assertFalse(
                qualifies_as_directed_transcript_wake(
                    normalize_for_wake(line),
                    _WAKE,
                    max_dist=2,
                    max_leading_words_before_wake=10,
                ),
                msg=line,
            )

    def test_direct_question_still_qualifies(self) -> None:
        for line in (
            "computer what do you think",
            "computer what do you think we should do",
            "all right computer can you hear me",
        ):
            self.assertTrue(
                qualifies_as_directed_transcript_wake(
                    normalize_for_wake(line),
                    _WAKE,
                    max_dist=2,
                    max_leading_words_before_wake=10,
                ),
                msg=line,
            )

    def test_device_computer_still_suppressed(self) -> None:
        line = "uploaded to your computer and nothing leaves your computer"
        self.assertTrue(is_false_address_use(normalize_for_wake(line)))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                normalize_for_wake(line),
                _WAKE,
                max_dist=2,
                max_leading_words_before_wake=10,
            ),
        )

    def test_device_mention_before_direct_computer_question_not_suppressed(self) -> None:
        line = (
            "proceed straight to the live demonstration of the computer "
            "what do you think should we proceed with a demo"
        )
        n = normalize_for_wake(line)
        self.assertFalse(should_suppress_transcript_wake(n, _WAKE, max_dist=2))
        self.assertTrue(
            qualifies_as_directed_transcript_wake(
                n, _WAKE, max_dist=2, max_leading_words_before_wake=10
            )
        )

    def test_truncated_should_we_not_directed(self) -> None:
        n = normalize_for_wake("dont say what do you think what do you think computer should we")
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                n, _WAKE, max_dist=2, max_leading_words_before_wake=10
            )
        )

    def test_meta_discussion_suppressed(self) -> None:
        self.assertTrue(
            is_meta_wake_discussion(
                normalize_for_wake("say computer bro not mechus")
            )
        )

    def test_called_computer_meta_discussion_suppressed(self) -> None:
        line = "but now its called computer if you want we can call it your name anything"
        n = normalize_for_wake(line)
        self.assertTrue(is_meta_wake_discussion(n))
        self.assertTrue(should_suppress_transcript_wake(n, _WAKE, max_dist=2))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                n, _WAKE, max_dist=2, max_leading_words_before_wake=10
            )
        )

    def test_change_computer_to_suppressed(self) -> None:
        line = "tell them you can change computer to whatever you want"
        n = normalize_for_wake(line)
        self.assertTrue(is_meta_wake_discussion(n))
        self.assertTrue(should_suppress_transcript_wake(n, _WAKE, max_dist=2))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                n, _WAKE, max_dist=2, max_leading_words_before_wake=10
            )
        )

    def test_cool_does_not_fuzzy_wake_mechus(self) -> None:
        line = "we should do a cool love me edit you know like the grok edits"
        n = normalize_for_wake(line)
        self.assertFalse(detect_wake(n, _WAKE, max_dist=2))
        self.assertFalse(line_has_explicit_wake_token(n, _WAKE, max_dist=2))
        self.assertFalse(
            qualifies_as_directed_transcript_wake(
                n, _WAKE, max_dist=2, max_leading_words_before_wake=10
            )
        )

    def test_thats_cool_not_wake(self) -> None:
        n = normalize_for_wake("yeah that's cool")
        self.assertIn("cool", n)
        self.assertFalse(detect_wake(n, ("mechus",), max_dist=1))

    def test_computer_on_line_counts_as_explicit_wake(self) -> None:
        n = normalize_for_wake("computer what is the difference between glasses")
        self.assertTrue(line_has_explicit_wake_token(n, _WAKE, max_dist=2))

    def test_know_does_not_fuzzy_wake_mechus_at_dist_one(self) -> None:
        n = normalize_for_wake("you know how we talked about the budget")
        self.assertFalse(detect_wake(n, ("mechus",), max_dist=1))
        self.assertFalse(line_has_explicit_wake_token(n, ("mechus",), max_dist=1))

    def test_coov_still_wakes_mechus_at_dist_one(self) -> None:
        n = normalize_for_wake("hey meckus summarize the meeting")
        self.assertTrue(detect_wake(n, ("mechus",), max_dist=1))


class TestStopIntentWithQuestion(unittest.TestCase):
    def test_wait_plus_question_not_stop(self) -> None:
        self.assertEqual(
            classify_pipeline_intent("wait does it have opinions"),
            INTENT_GENERAL,
        )

    def test_bare_wait_still_stop(self) -> None:
        self.assertEqual(classify_pipeline_intent("wait"), INTENT_STOP)


if __name__ == "__main__":
    unittest.main()
