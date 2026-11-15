"""Language detection and multilingual helpers for Koov."""

from __future__ import annotations

import random
# ISO-639-1 codes → human-readable names for LLM / logging directives.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ro": "Romanian",
    "ru": "Russian",
    "uk": "Ukrainian",
    "tr": "Turkish",
    "ar": "Arabic",
    "he": "Hebrew",
    "fa": "Persian",
    "hi": "Hindi",
    "bn": "Bengali",
    "ta": "Tamil",
    "te": "Telugu",
    "ur": "Urdu",
    "pa": "Punjabi",
    "gu": "Gujarati",
    "ml": "Malayalam",
    "kn": "Kannada",
    "mr": "Marathi",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "vi": "Vietnamese",
    "th": "Thai",
    "id": "Indonesian",
    "ms": "Malay",
    "sw": "Swahili",
    "el": "Greek",
    "cs": "Czech",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "hu": "Hungarian",
}

_UNKNOWN_CODES = frozenset({"", "nn", "und", "unk"})


def normalize_language_code(code: object) -> str | None:
    """Return a lowercased ISO-639 prefix or None when unknown."""
    try:
        raw = (str(code) if code is not None else "").strip().lower()
    except Exception:
        return None
    if not raw or len(raw) < 2 or raw in _UNKNOWN_CODES:
        return None
    return raw.split("-", 1)[0].split("_", 1)[0]


def is_english_language(code: str | None) -> bool:
    prefix = normalize_language_code(code)
    return prefix in (None, "en")


def _script_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {
        "latin": 0,
        "cjk": 0,
        "kana": 0,
        "hangul": 0,
        "arabic": 0,
        "hebrew": 0,
        "devanagari": 0,
        "cyrillic": 0,
        "thai": 0,
    }
    for ch in text:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            counts["kana"] += 1
        elif 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            counts["cjk"] += 1
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            counts["hangul"] += 1
        elif 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F:
            counts["arabic"] += 1
        elif 0x0590 <= o <= 0x05FF:
            counts["hebrew"] += 1
        elif 0x0900 <= o <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x0E00 <= o <= 0x0E7F:
            counts["thai"] += 1
        elif o <= 0x024F:
            counts["latin"] += 1
        else:
            counts["cjk"] += 1
    return counts


def guess_language_from_text(text: str, *, min_letters: int = 4) -> str | None:
    """Lightweight script guess when STT omits language (e.g. short realtime segments)."""
    sample = (text or "").strip()
    if not sample:
        return None
    counts = _script_counts(sample)
    total = sum(counts.values())
    if total < min_letters:
        return None
    if counts["kana"] >= 2 or (counts["kana"] >= 1 and counts["cjk"] >= 1):
        return "ja"
    if counts["hangul"] >= min_letters:
        return "ko"
    if counts["cjk"] >= min_letters:
        return "zh"
    if counts["arabic"] >= min_letters:
        return "ar"
    if counts["hebrew"] >= min_letters:
        return "he"
    if counts["devanagari"] >= min_letters:
        return "hi"
    if counts["cyrillic"] >= min_letters:
        return "ru"
    if counts["thai"] >= min_letters:
        return "th"
    return None


def language_display_name(code: str | None) -> str:
    prefix = normalize_language_code(code)
    if not prefix:
        return "English"
    return LANGUAGE_NAMES.get(prefix, prefix)


# Instant canned replies for common intents (no LLM latency).
_INSTANT_I18N: dict[str, dict[str, tuple[str, ...]]] = {
    "presence": {
        "es": ("Sí, te escucho.", "Te oigo bien.", "Aquí estoy."),
        "fr": ("Oui, je t'entends.", "Je t'entends bien.", "Je suis là."),
        "de": ("Ja, ich höre dich.", "Ich verstehe dich.", "Bin da."),
        "pt": ("Sim, te ouço.", "Te ouço bem.", "Estou aqui."),
        "it": ("Sì, ti sento.", "Ti sento bene.", "Ci sono."),
        "hi": ("हाँ, मैं सुन रहा हूँ।", "आवाज़ साफ़ है।", "मैं यहाँ हूँ।"),
        "ja": ("はい、聞こえています。", "はっきり聞こえます。", "ここにいます。"),
        "ko": ("네, 들립니다.", "잘 들려요.", "여기 있어요."),
        "zh": ("听得见。", "听得很清楚。", "我在。"),
        "ar": ("نعم، أسمعك.", "أسمعك بوضوح.", "أنا هنا."),
    },
    "wake_only": {
        "es": ("Sí, aquí estoy.", "Dime.", "Te escucho."),
        "fr": ("Oui, je suis là.", "Vas-y.", "Je t'écoute."),
        "de": ("Ja, bin da.", "Los.", "Ich höre zu."),
        "pt": ("Sim, estou aqui.", "Pode falar.", "Te escuto."),
        "it": ("Sì, ci sono.", "Dimmi.", "Ti ascolto."),
        "hi": ("हाँ, मैं यहाँ हूँ।", "बोलिए।", "सुन रहा हूँ।"),
        "ja": ("はい、ここにいます。", "どうぞ。", "聞いています。"),
        "ko": ("네, 여기 있어요.", "말씀하세요.", "듣고 있어요."),
        "zh": ("在的。", "请说。", "我在听。"),
        "ar": ("نعم، أنا هنا.", "تفضل.", "أنا أستمع."),
    },
    "greeting": {
        "es": ("Hola, estoy aquí — pídeme un resumen cuando quieras.", "Te escucho, ¿qué necesitas?"),
        "fr": ("Salut, je suis là — demande un récap quand tu veux.", "Je t'écoute, de quoi as-tu besoin ?"),
        "de": ("Hey, ich bin da — frag mich jederzeit nach einer Zusammenfassung.", "Ich höre zu, was brauchst du?"),
        "pt": ("Oi, estou aqui — peça um resumo quando quiser.", "Te escuto, o que precisa?"),
        "it": ("Ciao, ci sono — chiedimi un riepilogo quando vuoi.", "Ti ascolto, di cosa hai bisogno?"),
        "hi": ("मैं यहाँ हूँ — recap या निर्णय पूछ सकते हैं।", "सुन रहा हूँ, क्या चाहिए?"),
        "ja": ("ここにいます。要約が必要ならいつでもどうぞ。", "聞いています。何が必要ですか？"),
        "ko": ("여기 있어요. 요약이 필요하면 말씀하세요.", "듣고 있어요. 무엇이 필요하세요?"),
        "zh": ("我在。需要总结随时问我。", "在听，你需要什么？"),
        "ar": ("أنا هنا — اطلب ملخصاً في أي وقت.", "أستمع إليك، ماذا تحتاج؟"),
    },
    "help": {
        "es": ("Llevo toda la reunión — pide resumen, decisiones o riesgos.", "Pregunta por acciones o lo que decidimos."),
        "fr": ("Je suis la réunion — demande récap, décisions ou risques.", "Demande les actions ou ce qu'on a décidé."),
        "de": ("Ich verfolge das Meeting — frag nach Recap, Entscheidungen oder Risiken.", "Frag nach Action Items oder Beschlüssen."),
        "pt": ("Acompanho a reunião — peça resumo, decisões ou riscos.", "Pergunte por ações ou o que decidimos."),
        "it": ("Seguo la riunione — chiedi riepilogo, decisioni o rischi.", "Chiedi azioni o cosa abbiamo deciso."),
        "hi": ("मैं meeting follow कर रहा हूँ — recap, decisions या risks पूछें।", "action items या फैसले पूछ सकते हैं।"),
        "ja": ("会議を追っています。要約・決定・リスクを聞いてください。", "アクションや決定事項を聞けます。"),
        "ko": ("회의를 따라가고 있어요. 요약, 결정, 리스크를 물어보세요.", "액션 아이템이나 결정을 물어볼 수 있어요."),
        "zh": ("我在跟进会议。可以问总结、决定或风险。", "可以问行动项或我们决定了什么。"),
        "ar": ("أتابع الاجتماع — اطلب ملخصاً أو قرارات أو مخاطر.", "اسأل عن المهام أو ما قررناه."),
    },
    "unknown": {
        "es": ("No estoy seguro. Prueba a pedir un resumen, decisiones o acciones.",),
        "fr": ("Je ne suis pas sûr. Demande un récap, des décisions ou des actions.",),
        "de": ("Bin mir nicht sicher. Frag nach Recap, Entscheidungen oder Action Items.",),
        "pt": ("Não tenho certeza. Peça um resumo, decisões ou ações.",),
        "it": ("Non sono sicuro. Chiedi un riepilogo, decisioni o azioni.",),
        "hi": ("समझ नहीं आया। recap, decisions या action items पूछकर देखें।",),
        "ja": ("はっきりしません。要約・決定・アクションを試してください。",),
        "ko": ("잘 모르겠어요. 요약, 결정, 액션 아이템을 요청해 보세요.",),
        "zh": ("不太确定。试试问总结、决定或行动项。",),
        "ar": ("لست متأكداً. جرّب طلب ملخص أو قرارات أو مهام.",),
    },
}


def pick_instant_reply(
    key: str,
    english_replies: tuple[str, ...] | list[str],
    *,
    user_language: str | None = None,
) -> str:
    """Pick a localized instant reply when available, else English."""
    prefix = normalize_language_code(user_language)
    if prefix and prefix != "en":
        localized = _INSTANT_I18N.get(key, {}).get(prefix)
        if localized:
            return random.choice(localized)
    return random.choice(list(english_replies))
