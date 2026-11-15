#!/usr/bin/env python3
"""
Simple test for action item extraction using Ollama directly.
"""

import json
import urllib.request
import sys

def run_action_item_extraction():
    """Test extracting action items using Ollama directly."""
    
    test_text = "Bob said he will write the pricing doc by Friday."
    
    print("=" * 60)
    print("Testing Action Item Extraction")
    print("=" * 60)
    print(f"\nInput text: {test_text}\n")
    
    # Build prompt for extraction
    system_prompt = (
        "You are extracting structured information from a meeting transcript segment.\n"
        "Extract ONLY what is explicitly stated. Do NOT infer or speculate.\n"
        "Return valid JSON only, no commentary.\n"
    )
    
    user_prompt = (
        f"Extract structured information from this meeting segment:\n\n"
        f"Text: {test_text}\n\n"
        f"Event Type: action_item\n"
        f"Intent: propose\n\n"
        f"Extract and return JSON with these fields:\n"
        f"- clean_text: Remove filler words (um, uh, you know), stage directions, and normalize.\n"
        f"- entities: List of specific entities mentioned (names, products, numbers, dates).\n"
        f"- claims: List of factual claims or statements made.\n"
        f"- decisions: List of explicit decisions made (only if clearly stated).\n"
        f"- action_items: List of objects with 'owner', 'task', 'due' (optional), 'confidence' (0.0-1.0).\n"
        f"- open_questions: List of questions asked that weren't answered.\n"
        f"- confidence: Confidence score 0.0-1.0 for the extraction.\n\n"
        f"CRITICAL: Only extract what is EXPLICITLY stated. If a field has no clear content, use empty list/null.\n"
        f"Return ONLY valid JSON, no markdown, no explanation."
    )
    
    prompt = f"{system_prompt}\n\n{user_prompt}"
    
    body = {
        "model": "qwen2.5:7b-instruct",
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "top_p": 0.9,
            "num_predict": 512,
        },
    }
    
    print("Calling Ollama...")
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        
        import time
        start = time.time()
        
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        
        elapsed = (time.time() - start) * 1000
        
        response_text = str(payload.get("response", "")).strip()
        
        print(f"✅ Response received in {elapsed:.1f}ms\n")
        print("=" * 60)
        print("RAW RESPONSE")
        print("=" * 60)
        print(response_text)
        print()
        
        # Try to extract JSON (handle markdown code blocks)
        import re
        # Remove markdown code blocks if present
        response_text = re.sub(r'```json\s*', '', response_text)
        response_text = re.sub(r'```\s*', '', response_text)
        response_text = response_text.strip()
        
        # Try to find JSON object
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                print("=" * 60)
                print("PARSED JSON")
                print("=" * 60)
                print(json.dumps(data, indent=2))
                print()
                
                # Check action items
                action_items = data.get("action_items", [])
                if action_items:
                    print("=" * 60)
                    print("ACTION ITEMS EXTRACTED")
                    print("=" * 60)
                    for i, ai in enumerate(action_items, 1):
                        print(f"\n{i}. Owner: {ai.get('owner', 'N/A')}")
                        print(f"   Task: {ai.get('task', 'N/A')}")
                        print(f"   Due: {ai.get('due', 'Not specified')}")
                        print(f"   Confidence: {ai.get('confidence', 0.0):.2f}")
                    
                    # Validate
                    ai = action_items[0]
                    success = (
                        "bob" in ai.get("owner", "").lower() and
                        ("pricing" in ai.get("task", "").lower() or "doc" in ai.get("task", "").lower()) and
                        ("friday" in (ai.get("due") or "").lower())
                    )
                    
                    if success:
                        print("\n✅ SUCCESS: Action item correctly extracted!")
                        return True
                    else:
                        print("\n⚠️  PARTIAL: Action item extracted but may need refinement")
                        return False
                else:
                    print("\n❌ FAILED: No action items in response")
                    return False
            except json.JSONDecodeError as e:
                print(f"\n❌ FAILED: Could not parse JSON: {e}")
                return False
        else:
            print("\n❌ FAILED: No JSON found in response")
            return False
            
    except urllib.error.URLError as e:
        print(f"\n❌ ERROR: Could not connect to Ollama: {e}")
        print("\nMake sure Ollama is running:")
        print("  ollama serve")
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\n🧪 Testing Action Item Extraction with Ollama\n")
    success = run_action_item_extraction()
    sys.exit(0 if success else 1)
