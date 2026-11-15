from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


class CommandValidator:
    """
    Validates that "Koov" is followed by an actionable command.
    Ensures the system doesn't respond to false wakes or incomplete commands.
    """
    
    # Minimum command length (excluding wake word)
    MIN_COMMAND_LENGTH = 3
    
    # Actionable command patterns (wake word may be stripped before check)
    # More patterns = more natural phrases accepted as input (STT → agent responds).
    ACTIONABLE_PATTERNS = [
        # Questions
        r"\b(what|who|when|where|why|how|which|whose|whom)\b",
        r"\b(can you|could you|would you|will you|should we|are you|do you)\b",
        r"\b(can|could|would|will|should|is|are|was|were|did|does|do)\b",
        r"\b(tell me|give me|show me|explain|describe|define|list)\b",
        # Commands
        r"\b(summarize|summary|recap|overview|review)\b",
        r"\b(meeting|the meeting)\s+(summarize|summary)\b",
        r"\b(summarize|summary)\s+(the\s+)?meeting\b",
        r"\b(action items?|next steps?|todo|tasks?)\b",
        r"\b(decisions?|decided|agreed)\b",
        r"\b(notes?|take notes|keep notes)\b",
        r"\b(give|tell|show|explain|describe|find|get|make|create|compare|analyze|analyse)\b",
        # Knowledge/information
        r"\b(information|info|details?|facts?|history|historical|about|regarding)\b",
        r"\b(most|best|worst|latest|recent|current|top|important|significant)\b",
        # Requests
        r"\b(please|help|assist)\b",
        # Greetings / check-ins (so Koov responds when you call out)
        r"\b(hi|hello|hey|yo|there|listening|hear me)\b",
        # Question mark
        r"\?",
        # Extra: opinions, suggestions, reminders
        r"\b(think|opinion|thoughts?|suggest|recommend|advice)\b",
        r"\b(remember|remind|recall|forgot|forget)\b",
        r"\b(repeat|say again|rephrase|clarify)\b",
        # Meeting/collab
        r"\b(owner|assign|who\'?s? doing|who owns|action for)\b",
        r"\b(timeline|deadline|due date|when is|by when)\b",
        r"\b(blocker|blocked|risk|risks|concern|open question)\b",
        r"\b(updates?|status|progress|where are we)\b",
        r"\b(agenda|follow-?up|follow up)\b",
        r"\b(capture|record|write down|note that|add to)\b",
        r"\b(highlight|focus|priority|key takeaway)\b",
        # Natural follow-ups
        r"\b(quick question|got a question|one more thing)\b",
        r"\b(what\'?s? next|whats next|anything else)\b",
        r"\b(so what|in summary|bottom line)\b",
        r"\b(translate|spell|meaning of|what does)\b",
    ]
    
    # Non-actionable patterns (filler only; greetings are now allowed so Koov responds when called)
    NON_ACTIONABLE_PATTERNS = [
        r"^\s*[a-z]{1,2}\s*$",  # 1-2 letter word only
        r"^\s*(um|uh|er|hmm|yeah|yes|no|ok|okay)\s*$",  # Just filler words
    ]
    
    def __init__(self, wake_phrase: str = "koov", wake_phrases: tuple[str, ...] | None = None):
        self._wake_phrase = wake_phrase.lower()
        self._wake_phrases = wake_phrases if wake_phrases else (self._wake_phrase,)
        self._actionable_re = re.compile("|".join(f"({p})" for p in self.ACTIONABLE_PATTERNS), re.IGNORECASE)
        self._non_actionable_re = re.compile("|".join(f"({p})" for p in self.NON_ACTIONABLE_PATTERNS), re.IGNORECASE)
    
    def validate_command(self, text: str, wake_already_confirmed: bool = False) -> tuple[bool, str]:
        """
        Validate that text contains an actionable command after wake word.
        Returns (is_valid, reason). Rejects bare wake-only; greetings need a real command.

        When wake_already_confirmed=True (Vosk already fired), the wake phrase is not required
        in the transcription — any non-trivial, non-filler text is accepted as a command.
        """
        if not text:
            return False, "Empty command"
        
        text_l = text.lower().strip()

        # When wake was already hardware-confirmed by Vosk, skip the wake-phrase-in-text check.
        # Just require the text is non-trivial and not pure filler.
        if wake_already_confirmed:
            if len(text_l) < self.MIN_COMMAND_LENGTH:
                return False, "Command too short"
            if self._non_actionable_re.match(text_l):
                return False, "Non-actionable pattern (filler only)"
            return True, "Valid command (wake confirmed by detector)"
        
        # Check if any wake word is present
        if not any(w in text_l for w in self._wake_phrases):
            # Wake word might have been stripped, check if it's a valid command anyway
            if len(text.strip()) < self.MIN_COMMAND_LENGTH:
                return False, "Command too short"
            # Allow commands without wake word if they're clearly commands
            if self._actionable_re.search(text):
                return True, "Valid command (wake word already stripped)"
            return False, "No wake word and not clearly a command"
        
        # Remove first matching wake word for validation
        text_after_wake = text_l
        for w in self._wake_phrases:
            if w in text_after_wake:
                text_after_wake = text_after_wake.replace(w, "", 1).strip()
                break
        
        # Bare wake ("Koov." / "Computer.") — user wants action items only,
        # not a "yes I'm here" ack on isolated wake words. Reject so the agent
        # stays silent until a real question/command follows.
        if not text_after_wake or text_after_wake in self._wake_phrases:
            return False, "Wake only — no command (silent)"
        
        # Check for non-actionable patterns (filler only)
        if self._non_actionable_re.match(text_after_wake):
            return False, "Non-actionable pattern (filler only)"
        
        # Check minimum length (allow short greetings like "hello", "hi there")
        if len(text_after_wake) < self.MIN_COMMAND_LENGTH:
            # Allow if it matches a greeting/check-in so Koov responds when called
            if self._actionable_re.search(text_after_wake):
                return True, "Valid greeting/check-in"
            return False, f"Command too short after wake word (min {self.MIN_COMMAND_LENGTH} chars)"
        
        # Check for actionable patterns
        if not self._actionable_re.search(text_after_wake):
            # Allow if it's a reasonable length and not just filler
            if len(text_after_wake.split()) >= 2:
                return True, "Valid command (sufficient length)"
            return False, "No actionable pattern detected"
        
        return True, "Valid actionable command"
    
    def extract_command(self, text: str) -> str:
        """Extract the command part after the wake word."""
        if not text:
            return ""
        text_l = text.lower()
        for w in self._wake_phrases:
            if w in text_l:
                parts = text_l.split(w, 1)
                if len(parts) > 1:
                    return parts[1].strip()
        return text.strip()

