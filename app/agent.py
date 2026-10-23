"""Drafts opening messages and reply suggestions.

Two engines:
  * Claude (when ANTHROPIC_API_KEY is set) — grounded strictly in the knowledge files.
  * Rule-based fallback — templates + keyword FAQ. Hands off whenever unsure.

The human always reviews and presses "send" in WhatsApp. Nothing is sent automatically.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .calendar_service import Slot
from .knowledge import Knowledge, Model

INTENTS = ("answer", "meeting", "handoff", "not_interested")

SYSTEM_PROMPT = """אתה עוזר מכירות של סוכנות אופנועי Triumph. אתה מנסח הודעות וואטסאפ שאיש המכירות יקרא, יערוך וישלח בעצמו.

כללים מחייבים:
1. ענה אך ורק מתוך "מידע מאושר" שלמטה. אסור להמציא מחירים, מבצעים, זמני אספקה, מפרטים, תנאי מימון או כל עובדה אחרת.
2. אם התשובה לא נמצאת במידע המאושר, או שכתוב "לא ידוע — להעביר לנציג": intent="handoff", ובהודעה ללקוח כתוב בחום שתבדוק ותחזור אליו בהקדם. אל תנחש.
3. אם הלקוח מבקש נציג/אדם, כועס, או מעלה נושא רגיש — intent="handoff".
4. המטרה: לקבוע נסיעת מבחן או ביקור בסוכנות. כשהלקוח מגלה עניין או שואל על הגעה — intent="meeting" והצע את המועדים הפנויים שברשימה (רק אותם, במדויק).
5. אם הלקוח אומר שהוא לא מעוניין — intent="not_interested", תודה קצרה ומנומסת, בלי לחץ.
6. טון: עברית, אישי, קצר (1–3 משפטים), כמו איש מכירות טוב. לא רובוטי, בלי רשימות, אימוג'י אחד לכל היותר.
7. פנה ללקוח בשמו הפרטי. חתום רק אם זו הודעת פתיחה.
"""

REPLY_TOOL = {
    "name": "draft_reply",
    "description": "החזר טיוטת הודעת וואטסאפ ללקוח וסיווג הכוונה.",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "טיוטת ההודעה ללקוח, בעברית"},
            "intent": {"type": "string", "enum": list(INTENTS)},
            "handoff_reason": {"type": "string", "description": "למה צריך נציג (אם intent=handoff), לאיש המכירות"},
        },
        "required": ["message", "intent"],
    },
}


@dataclass
class Suggestion:
    message: str
    intent: str = "answer"
    handoff_reason: str = ""
    used_ai: bool = False
    slots: list[Slot] = field(default_factory=list)


LLMCall = Callable[[str, list[dict], dict], dict]  # (system, messages, tool) -> tool input dict


def anthropic_llm(api_key: str, model: str) -> LLMCall:
    def call(system: str, messages: list[dict], tool: dict) -> dict:
        import anthropic  # lazy: app works without the SDK in no-AI mode

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=system,
            messages=messages,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise RuntimeError("Claude did not return a tool_use block")

    return call


class Agent:
    def __init__(self, kn: Knowledge, llm: LLMCall | None = None):
        self.kn = kn
        self.llm = llm

    # ---- opening -------------------------------------------------------
    def template_opening(self, first_name: str, model_name: str) -> str:
        b = self.kn.business
        return self.kn.opening_template.format(
            first_name=first_name or "",
            model=model_name or "האופנוע",
            salesperson=b.get("salesperson_name", ""),
            business=b.get("name", ""),
        ).strip()

    def opening(self, first_name: str, model: Model | None, model_raw: str) -> Suggestion:
        model_name = model.name if model else (model_raw or "")
        base = self.template_opening(first_name, model_name)
        if not self.llm:
            return Suggestion(base)
        user = (
            f"נסח הודעת פתיחה ראשונה לליד חדש.\nשם פרטי: {first_name}\nדגם שהתעניין בו: {model_name or 'לא ידוע'}\n"
            f"דוגמת סגנון מאושרת (אפשר לשפר קלות, לא להאריך):\n{base}\n\n"
            f"חתום בשם {self.kn.business.get('salesperson_name', '')}. intent=answer.\n\n"
            f"# מידע מאושר\n{self.kn.prompt_context(model)}"
        )
        try:
            out = self.llm(SYSTEM_PROMPT, [{"role": "user", "content": user}], REPLY_TOOL)
            return Suggestion(out["message"].strip(), "answer", used_ai=True)
        except Exception as e:  # fall back, never block the salesperson
            return Suggestion(base, handoff_reason=f"AI לא זמין ({e}); הוצגה תבנית.")

    # ---- replies -------------------------------------------------------
    def suggest_reply(
        self,
        first_name: str,
        model: Model | None,
        history: list[tuple[str, str]],  # [(direction 'out'/'in', text), ...] incl. latest incoming
        slots: list[Slot],
    ) -> Suggestion:
        incoming = next((t for d, t in reversed(history) if d == "in"), "")
        if self.llm:
            try:
                return self._ai_reply(first_name, model, history, slots)
            except Exception as e:
                s = self._rule_reply(first_name, model, incoming, slots)
                s.handoff_reason = (s.handoff_reason + f" | AI לא זמין: {e}").strip(" |")
                return s
        return self._rule_reply(first_name, model, incoming, slots)

    def _ai_reply(self, first_name, model, history, slots) -> Suggestion:
        convo = "\n".join(("לקוח: " if d == "in" else "אנחנו: ") + t for d, t in history)
        slot_text = "\n".join(f"- {s.label()}" for s in slots) or "אין מועדים פנויים — העבר לנציג."
        user = (
            f"שם הלקוח: {first_name}\n\n# השיחה עד עכשיו\n{convo}\n\n"
            f"# מועדים פנויים להצעה\n{slot_text}\n\n# מידע מאושר\n{self.kn.prompt_context(model)}\n\n"
            "נסח את התשובה הבאה שלנו להודעה האחרונה של הלקוח."
        )
        out = self.llm(SYSTEM_PROMPT, [{"role": "user", "content": user}], REPLY_TOOL)
        intent = out.get("intent") if out.get("intent") in INTENTS else "handoff"
        return Suggestion(
            message=out.get("message", "").strip(),
            intent=intent,
            handoff_reason=out.get("handoff_reason", "") or "",
            used_ai=True,
            slots=slots if intent == "meeting" else [],
        )

    # ---- rule-based fallback --------------------------------------------
    NOT_INTERESTED = ["לא מעוניין", "לא מעוניינת", "לא רלוונטי", "תוריד אותי", "אל תפנו", "לא תודה", "כבר קניתי"]
    MEETING = ["נסיעת מבחן", "לנסות", "להגיע", "לבוא", "פגישה", "לקבוע", "מתי אפשר", "מתי פתוחים", "ביקור", "לראות אותו"]
    FIELD_WORDS = {
        "price": ["מחיר", "כמה עולה", "עולה", "עלות", "₪", "שקל"],
        "colors": ["צבע", "צבעים"],
        "availability": ["מלאי", "זמין", "אספקה", "מתי מגיע", "כמה זמן"],
        "promotions": ["מבצע", "הנחה", "מבצעים"],
        "license": ["רישיון", "רשיון", "דרגה", "a2", "a1"],
    }

    def _rule_reply(self, first_name, model: Model | None, incoming: str, slots: list[Slot]) -> Suggestion:
        t = incoming.lower()
        name = first_name or ""
        hold = f"שאלה טובה {name}, אני בודק ומחזיר לך תשובה מדויקת בהקדם 🙏".replace("  ", " ")

        if self.kn.wants_handoff(incoming):
            return Suggestion(f"בטח {name}, מישהו מהצוות יחזור אליך בהקדם.", "handoff", "הלקוח ביקש נציג")
        if any(w in t for w in self.NOT_INTERESTED):
            return Suggestion(f"תודה שעדכנת {name}! אם תרצה בעתיד — אנחנו כאן 🙂", "not_interested")

        # factual question about the model
        asked = [f for f, words in self.FIELD_WORDS.items() if any(w in t for w in words)]
        if asked:
            if not model:
                return Suggestion(hold, "handoff", "שאלה עובדתית אבל הדגם לא זוהה")
            parts, unknown = [], []
            for f in asked:
                v = getattr(model, f)
                if v in (None, "", []):
                    unknown.append(f)
                else:
                    parts.append(_field_sentence(f, model, v))
            if unknown:
                return Suggestion(hold, "handoff", f"אין מידע מאושר על: {', '.join(unknown)} ({model.name})")
            msg = " ".join(parts) + " רוצה לבוא לנסיעת מבחן?"
            return Suggestion(msg.strip(), "answer")

        if any(w in t for w in self.MEETING) or re.search(r"\b(כן|בשמחה|אשמח|יאללה)\b", t):
            if not slots:
                return Suggestion(hold, "handoff", "הלקוח רוצה פגישה אבל אין מועדים פנויים")
            opts = ", ".join(s.label() for s in slots)
            return Suggestion(f"מעולה {name}! יש לי {opts}. מה מתאים לך?", "meeting", slots=slots)

        faqs = self.kn.match_faq(incoming)
        if faqs:
            known = [f for f in faqs if f.answer]
            if len(known) == len(faqs):
                return Suggestion(" ".join(f.answer for f in known), "answer")
            missing = ", ".join(f.topic for f in faqs if not f.answer)
            return Suggestion(hold, "handoff", f"אין תשובה מאושרת לנושא: {missing}")

        return Suggestion(hold, "handoff", "לא זוהתה שאלה מוכרת — כדאי שנציג יענה")


def _field_sentence(f: str, model: Model, v) -> str:
    v = ", ".join(v) if isinstance(v, list) else v
    return {
        "price": f"המחיר של ה-{model.name} הוא {v}.",
        "colors": f"ה-{model.name} מגיע בצבעים: {v}.",
        "availability": f"לגבי זמינות: {v}.",
        "promotions": f"כרגע יש: {v}.",
        "license": f"ה-{model.name} מתאים לרישיון {v}.",
    }[f]


def suggestion_to_json(s: Suggestion) -> str:
    return json.dumps(
        {"message": s.message, "intent": s.intent, "handoff_reason": s.handoff_reason, "used_ai": s.used_ai,
         "slots": [{"start": x.start.isoformat(), "end": x.end.isoformat(), "label": x.label()} for x in s.slots]},
        ensure_ascii=False,
    )
