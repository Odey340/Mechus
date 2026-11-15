"""Streaming JSON spoken extractor + sentence boundary emitter."""

from __future__ import annotations

import unittest

from koov.llm_stream import (
    SentenceBoundaryEmitter,
    SpokenJsonStreamer,
    parse_openai_sse_deltas,
)


class TestSpokenJsonStreamer(unittest.TestCase):
    def test_simple_field_extracted(self) -> None:
        s = SpokenJsonStreamer()
        out = s.feed('{"spoken":"Hello world","private_thoughts":"x"}')
        self.assertEqual(out, "Hello world")
        self.assertTrue(s.done)

    def test_handles_whitespace_around_colon(self) -> None:
        s = SpokenJsonStreamer()
        out = s.feed('{ "spoken" : "Hi there"}')
        self.assertEqual(out, "Hi there")

    def test_streaming_across_token_boundaries(self) -> None:
        s = SpokenJsonStreamer()
        chunks = ['{"spo', 'ken":"', "Hel", "lo ", 'world","next":1}']
        out = "".join(s.feed(c) for c in chunks)
        self.assertEqual(out, "Hello world")
        self.assertTrue(s.done)

    def test_skips_private_thoughts(self) -> None:
        """``private_thoughts`` content must never leak out."""
        s = SpokenJsonStreamer()
        payload = '{"private_thoughts":"secret thoughts","spoken":"the answer"}'
        out = s.feed(payload)
        self.assertEqual(out, "the answer")
        self.assertNotIn("secret", out)

    def test_handles_escape_sequences(self) -> None:
        s = SpokenJsonStreamer()
        out = s.feed(r'{"spoken":"She said \"hi\" then left.\nDone."}')
        self.assertEqual(out, 'She said "hi" then left.\nDone.')

    def test_escape_split_across_feeds(self) -> None:
        s = SpokenJsonStreamer()
        out = s.feed('{"spoken":"hi\\')
        out += s.feed('"there"}')
        self.assertEqual(out, 'hi"there')

    def test_done_after_close_quote(self) -> None:
        s = SpokenJsonStreamer()
        s.feed('{"spoken":"done."}')
        self.assertTrue(s.done)
        more = s.feed('{"spoken":"second"}')
        self.assertEqual(more, "")

    def test_unicode_escape(self) -> None:
        s = SpokenJsonStreamer()
        out = s.feed(r'{"spoken":"caf\u00e9"}')
        self.assertEqual(out, "café")


class TestSentenceBoundaryEmitter(unittest.TestCase):
    def test_emits_after_period_and_space(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=2)
        out = em.feed("Hello there. ")
        self.assertEqual(out, ["Hello there."])

    def test_does_not_emit_too_early(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=4)
        self.assertEqual(em.feed("Hi. "), [])
        self.assertEqual(em.feed("How are you doing today? "), ["Hi. How are you doing today?"])

    def test_handles_question_mark(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=2)
        self.assertEqual(em.feed("Are you there? "), ["Are you there?"])

    def test_handles_exclamation(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=2)
        self.assertEqual(em.feed("Look out! "), ["Look out!"])

    def test_flush_returns_leftover(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=2)
        em.feed("First one. ")
        out = em.flush()
        self.assertEqual(out, [])
        em2 = SentenceBoundaryEmitter(first_min_words=2)
        em2.feed("incomplete sentence with no end")
        self.assertEqual(em2.flush(), ["incomplete sentence with no end"])

    def test_streaming_one_char_at_a_time(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=2)
        text = "How are you? I'm fine, thanks. Bye!"
        collected: list[str] = []
        for ch in text:
            collected.extend(em.feed(ch))
        collected.extend(em.flush())
        self.assertEqual(
            collected,
            ["How are you?", "I'm fine, thanks.", "Bye!"],
        )

    def test_soft_break_on_long_clause(self) -> None:
        """A very long clause without a terminator breaks at the last comma."""
        em = SentenceBoundaryEmitter(
            first_min_words=2,
            min_words=1,
            soft_break_min_words=8,
        )
        out = em.feed("alpha beta gamma delta epsilon, zeta eta theta")
        self.assertEqual(out, ["alpha beta gamma delta epsilon,"])

    def test_period_inside_value_not_emitted_without_space(self) -> None:
        em = SentenceBoundaryEmitter(first_min_words=2)
        out = em.feed("Visit foo.com today and you'll")
        self.assertEqual(out, [])


class TestParseOpenAISSE(unittest.TestCase):
    def test_extracts_content_delta(self) -> None:
        line = 'data: {"id":"x","choices":[{"delta":{"content":"Hello"}}]}'
        self.assertEqual(parse_openai_sse_deltas(line), "Hello")

    def test_returns_none_for_done(self) -> None:
        self.assertIsNone(parse_openai_sse_deltas("data: [DONE]"))

    def test_returns_none_for_heartbeat(self) -> None:
        self.assertIsNone(parse_openai_sse_deltas(""))
        self.assertIsNone(parse_openai_sse_deltas(":heartbeat"))

    def test_returns_none_for_malformed(self) -> None:
        self.assertIsNone(parse_openai_sse_deltas("data: {not valid json"))

    def test_returns_none_for_no_content_delta(self) -> None:
        line = 'data: {"choices":[{"delta":{"role":"assistant"}}]}'
        self.assertIsNone(parse_openai_sse_deltas(line))


class TestEndToEndStreaming(unittest.TestCase):
    """Simulate an OpenAI JSON-mode stream and verify only ``spoken`` reaches the listener."""

    def test_json_streamer_plus_sentence_emitter(self) -> None:
        # Each chunk represents what would arrive over SSE.
        json_stream = [
            '{"priv',
            'ate_thoughts":"thinking about it",',
            '"spo',
            'ken":"',
            "Sure, ",
            "let me check. ",
            "Solon is in Ohio. ",
            "Got it",
            '."}',
        ]
        spoken = SpokenJsonStreamer()
        emitter = SentenceBoundaryEmitter(first_min_words=3)
        collected: list[str] = []
        for chunk in json_stream:
            new = spoken.feed(chunk)
            collected.extend(emitter.feed(new))
            if spoken.done:
                break
        collected.extend(emitter.flush())
        self.assertEqual(
            collected,
            ["Sure, let me check.", "Solon is in Ohio.", "Got it."],
        )


if __name__ == "__main__":
    unittest.main()
