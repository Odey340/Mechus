# Meeting Simulation Script (Human-Style)

Use this to test Mechus like a real meeting: **one line at a time**, with pauses, so Mechus processes it like live audio.

**Do not paste the whole script.** Speak (or paste) one line → wait 3–5 seconds → next line. For lines that are questions to Mechus, wait for Mechus to respond before continuing.

---

## Meeting: Product Launch Planning

**Participants:** Alex (PM), Sarah (Engineering), Mike (Marketing), Mechus (assistant)

---

### 1. Opening

**Alex:**  
Alright everyone, thanks for joining. The goal today is to finalize the launch plan for the beta release.

*(wait ~3 seconds)*

**Sarah:**  
Yeah, engineering is mostly done with the core features, but we still need to decide the rollout date.

*(wait)*

---

### 2. First question to Mechus

**Mike:**  
Mechus, can you start taking notes for this meeting?

*(wait for Mechus response)*

---

### 3. Discussion

**Alex:**  
Okay so first topic is the launch timeline. Originally we said mid-July.

*(wait)*

**Sarah:**  
Engineering would actually prefer July 25th. That gives us time for one more QA cycle.

*(wait)*

---

### 4. Decision point

**Mike:**  
From marketing's side July 25th works. That gives us two weeks to prepare the campaign.

*(wait)*

**Alex:**  
Alright let's lock it in then. The beta launch date will be July 25th.

*(wait)*

---

### 5. Ask Mechus

**Sarah:**  
Mechus, what did we just decide?

*(wait for Mechus response)*

---

### 6. Action items

**Mike:**  
I'll handle the launch announcement blog post.

*(wait)*

**Sarah:**  
Engineering will finish QA by July 20th.

*(wait)*

**Alex:**  
I'll coordinate the beta user invitations.

*(wait)*

---

### 7. Ask Mechus again

**Alex:**  
Mechus, can you list the action items so far?

*(wait for Mechus response)*

---

### 8. Risks discussion

**Sarah:**  
One risk is that the payment integration hasn't been fully tested yet.

*(wait)*

**Mike:**  
Another concern is whether the onboarding flow is clear enough for new users.

*(wait)*

---

### 9. Ask Mechus again

**Mike:**  
Mechus, have any risks been mentioned so far?

*(wait for response)*

---

### 10. Wrap-up

**Alex:**  
Alright quick recap before we end.

*(wait)*

**Alex:**  
Launch date July 25th, engineering finishes QA by the 20th, marketing prepares the announcement, and we monitor those two risks.

*(wait)*

---

### Final Mechus query

**Sarah:**  
Mechus, give us a quick recap of the meeting.

*(wait for response)*

---

## Optional stress tests

Use these during or after the meeting to test edge cases.

| Test | Line |
|------|------|
| **General knowledge** | Mike: Mechus, quick question — what is the difference between machine learning and AI? |
| **Interruption** | Sarah: Wait sorry Alex, before we move on — Mechus, what were the action items again? |
| **Ambiguous phrasing** | Alex: Mechus, what did we land on earlier about the launch date? |

---

## Tips

- Run `python scripts/run_meeting_simulation.py` to step through the script line-by-line with timing.
- Speak each line in the meeting (or paste into your meeting chat if it feeds Mechus).
- Pauses mimic real cadence and test: wake word, context tracking, intent routing, grounding, action items, decisions.
- For a harder test, use the chaotic simulation: `python scripts/run_meeting_simulation.py --chaotic`
