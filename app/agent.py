"""The agent's "brain": decides what to say next in a lead conversation.

Two engines:
  * Claude (when ANTHROPIC_API_KEY is set) — grounded strictly in the knowledge files.
  * Rule-based fallback — templates + keyword FAQ. Hands off whenever unsure.

The agent never sends anything itself. It returns a Suggestion; the ConversationEngine decides
whether it is sent automatically (auto mode) or waits for a human (supervised / wa.me mode).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .calendar_service import HEB_DAYS, Slot
from .knowledge import Knowledge, Model

# answer: plain reply · meeting: offering slots · book: customer picked one of the offered slots
# handoff: a human must take over · not_interested: close politely
INTENTS = ("answer", "meeting", "book", "handoff", "not_interested")

SYSTEM_PROMPT = """אתה {agent_name} של {business} — סוכנות אופנועי Triumph. אתה מנהל שיחת וואטסאפ עם לקוח שהשאיר פרטים.
ההודעה שתכתוב עשויה להישלח ללקוח ישירות, בלי עריכה.

כללים מחייבים:
1. עובדות (מחירים, מבצעים, זמני אספקה, מפרטים, מימון, אחריות) — אך ורק מתוך "מידע מאושר" שלמטה. אסור להמציא או לנחש.
2. שאלה שאין לה תשובה במידע המאושר (או שכתוב "לא ידוע — להעביר לנציג"): intent="handoff", pause_agent=false.
   כתוב ללקוח שתבדוק עם הצוות ותחזור אליו, והמשך את השיחה כרגיל (למשל הצע נסיעת מבחן). השיחה לא נעצרת.
3. רק אם הלקוח מבקש במפורש נציג/אדם, כועס, מתלונן או מעלה נושא רגיש: intent="handoff", pause_agent=true.
4. שיחה רגילה (ברכות, "ספר לי עליו", התלבטויות, שאלות כלליות על חוויית רכיבה) — ענה בחום ובטבעיות ככל שאפשר בלי להמציא עובדות, ותוביל בעדינות לנסיעת מבחן. intent="answer".
5. המטרה: לקבוע נסיעת מבחן או ביקור בסוכנות. כשהלקוח מגלה עניין — intent="meeting" והצע את המועדים מרשימת "מועדים להצעה" בלבד, במדויק.
6. אם הלקוח בחר בבירור אחד המועדים שהוצעו — intent="book" ו-chosen_slot = המספר שלו ברשימה. מועד שלא ברשימה — הצע שוב את הרשימה.
7. אם הלקוח לא מעוניין — intent="not_interested", תודה קצרה ומנומסת, בלי לחץ.
8. לעולם אל תטען שאתה בן אדם. אם שואלים — אמור בפשטות שאתה העוזר הדיגיטלי של הסוכנות ושאפשר לדבר עם נציג בכל רגע.
9. טון: עברית, אישי, קצר (1–3 משפטים), כמו איש מכירות טוב. לא רובוטי, בלי רשימות, אימוג'י אחד לכל היותר. פנה בשם הפרטי.
"""

REPLY_TOOL = {
    "name": "reply",
    "description": "ההודעה הבאה ללקוח וסיווג הכוונה.",
    "input_schema": {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "ההודעה ללקוח, בעברית"},
            "intent": {"type": "string", "enum": list(INTENTS)},
            "chosen_slot": {"type": "integer", "description": "רק כש-intent=book: מספר המועד שהלקוח בחר (1, 2, 3...)"},
            "handoff_reason": {"type": "string", "description": "רק כש-intent=handoff: מה צריך מהצוות (לאיש המכירות)"},
            "pause_agent": {"type": "boolean", "description": "true רק אם הלקוח ביקש נציג / כועס / נושא רגיש"},
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
    slots: list[Slot] = field(default_factory=list)  # slots offered in this message
    chosen: Slot | None = None  # intent == "book"
    pause: bool = False  # hard handoff: a human must take the whole conversation
    notify: str = ""  # FYI alert for the salesperson (e.g. "asked about financing") — conversation continues


LLMCall = Callable[[str, list[dict], dict], dict]  # (system, messages, tool) -> tool input dict


def anthropic_llm(api_key: str, model: str) -> LLMCall:
    def call(system: str, messages: list[dict], tool: dict) -> dict:
        import anthropic  # lazy: the app works without the SDK in no-AI mode

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

    @property
    def system_prompt(self) -> str:
        b = self.kn.business
        return SYSTEM_PROMPT.format(
            agent_name=b.get("agent_name", "העוזר הדיגיטלי"), business=b.get("name", "הסוכנות")
        )

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
        """Opening message. On WhatsApp Cloud API the first message must be an approved template,
        so the engine uses the template there; this text is for wa.me / simulator."""
        model_name = model.name if model else (model_raw or "")
        base = self.template_opening(first_name, model_name)
        if not self.llm:
            return Suggestion(base)
        user = (
            f"נסח הודעת פתיחה ראשונה לליד חדש.\nשם פרטי: {first_name}\nדגם שהתעניין בו: {model_name or 'לא ידוע'}\n"
            f"דוגמת סגנון מאושרת (אפשר לשפר קלות, לא להאריך):\n{base}\nintent=answer.\n\n"
            f"# מידע מאושר\n{self.kn.prompt_context(model)}"
        )
        try:
            out = self.llm(self.system_prompt, [{"role": "user", "content": user}], REPLY_TOOL)
            return Suggestion(out["message"].strip(), "answer", used_ai=True)
        except Exception as e:  # fall back, never block
            return Suggestion(base, handoff_reason=f"AI לא זמין ({e}); הוצגה תבנית.")

    # ---- replies -------------------------------------------------------
    def reply(
        self,
        first_name: str,
        model: Model | None,
        history: list[tuple[str, str]],  # [(direction 'out'/'in', text), ...] incl. latest incoming
        slots: list[Slot],  # slots the customer may pick from (already offered, else fresh)
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

    suggest_reply = reply  # backwards-compatible name

    def _ai_reply(self, first_name, model, history, slots) -> Suggestion:
        convo = "\n".join(("לקוח: " if d == "in" else "אנחנו: ") + t for d, t in history[-30:])
        slot_text = "\n".join(f"{i}. {s.label()}" for i, s in enumerate(slots, 1)) or "אין מועדים פנויים — העבר לנציג."
        user = (
            f"שם הלקוח: {first_name}\n\n# השיחה עד עכשיו\n{convo}\n\n"
            f"# מועדים להצעה\n{slot_text}\n\n# מידע מאושר\n{self.kn.prompt_context(model)}\n\n"
            "כתוב את ההודעה הבאה שלנו להודעה האחרונה של הלקוח."
        )
        out = self.llm(self.system_prompt, [{"role": "user", "content": user}], REPLY_TOOL)
        intent = out.get("intent") if out.get("intent") in INTENTS else "handoff"
        s = Suggestion(
            message=(out.get("message") or "").strip(),
            intent=intent,
            handoff_reason=out.get("handoff_reason", "") or "",
            used_ai=True,
            pause=bool(out.get("pause_agent")) and out.get("intent") == "handoff",
        )
        if intent == "meeting":
            s.slots = slots
        if intent == "book":
            idx = out.get("chosen_slot")
            if isinstance(idx, int) and 1 <= idx <= len(slots):
                s.chosen = slots[idx - 1]
            else:  # model said book but gave no valid slot — don't guess
                s.intent, s.handoff_reason = "handoff", "הלקוח כנראה בחר מועד אבל לא זוהה איזה"
        if not s.message:
            s.intent, s.handoff_reason = "handoff", s.handoff_reason or "AI החזיר הודעה ריקה"
            s.message = f"תודה {first_name}, מישהו מהצוות יחזור אליך בהקדם 🙏"
        return s

    # ---- rule-based fallback --------------------------------------------
    NOT_INTERESTED = ["לא מעוניין", "לא מעוניינת", "לא רלוונטי", "תוריד אותי", "אל תפנו", "לא תודה", "כבר קניתי"]
    MEETING = ["נסיעת מבחן", "לנסות", "להגיע", "לבוא", "פגישה", "לקבוע", "מתי אפשר", "מתי פתוחים", "ביקור", "לראות אותו"]
    BOT_QUESTION = ["בוט", "רובוט", "אתה אמיתי", "בן אדם", "מחשב עונה"]
    FIELD_WORDS = {
        "price": ["מחיר", "כמה עולה", "עולה", "עלות", "₪", "שקל"],
        "colors": ["צבע", "צבעים"],
        "availability": ["מלאי", "זמין", "אספקה", "מתי מגיע", "כמה זמן"],
        "promotions": ["מבצע", "הנחה", "מבצעים"],
        "license": ["רישיון", "רשיון", "דרגה", "a2", "a1"],
        "warranty": ["אחריות"],
        "service": ["טיפול", "טיפולים"],
    }

    GREETING = ["היי", "הי ", "שלום", "בוקר טוב", "ערב טוב", "צהריים טובים", "אהלן", "מה נשמע", "מה קורה", "hello", "hi"]
    THANKS = ["תודה", "מעולה", "סבבה", "אחלה", "תותח"]
    TELL_ME = ["ספר לי", "תספר", "פרטים", "מה יש לו", "מה מיוחד", "מעוניין", "מעוניינת", "מתעניין", "מתעניינת", "מידע"]

    def _rule_reply(self, first_name, model: Model | None, incoming: str, slots: list[Slot]) -> Suggestion:
        t = incoming.lower().strip()
        name = first_name or ""
        mname = model.name if model else "האופנוע"
        check = f"שאלה טובה {name} — אני בודק את זה עם הצוות וחוזר אליך. בינתיים, רוצה לקבוע נסיעת מבחן?"

        if self.kn.wants_handoff(incoming):
            return Suggestion(f"בטח {name}, מישהו מהצוות יחזור אליך בהקדם.", "handoff", "הלקוח ביקש נציג", pause=True)
        if any(w in t for w in self.BOT_QUESTION):
            return Suggestion(
                f"אני העוזר הדיגיטלי של {self.kn.business.get('name', 'הסוכנות')} 🙂 אם תרצה לדבר עם נציג — רק תגיד.",
                "answer",
            )
        if any(w in t for w in self.NOT_INTERESTED):
            return Suggestion(f"תודה שעדכנת {name}! אם תרצה בעתיד — אנחנו כאן 🙂", "not_interested")

        chosen = match_slot_choice(incoming, slots)
        if chosen:
            return Suggestion("", "book", chosen=chosen)

        asked = [f for f, words in self.FIELD_WORDS.items() if any(w in t for w in words)]
        if asked:
            if not model:
                return Suggestion(check, "handoff", "שאלה עובדתית אבל הדגם לא זוהה")
            parts, unknown = [], []
            for f in asked:
                v = getattr(model, f)
                if v in (None, "", []):
                    unknown.append(f)
                else:
                    parts.append(_field_sentence(f, model, v))
            if unknown and not parts:
                return Suggestion(check, "handoff", f"אין מידע מאושר על: {', '.join(unknown)} ({model.name})")
            msg = " ".join(parts)
            if unknown:
                msg += " את השאר אני בודק עם הצוות וחוזר אליך."
                return Suggestion((msg + " רוצה לבוא לנסיעת מבחן?").strip(), "handoff",
                                  f"אין מידע מאושר על: {', '.join(unknown)} ({model.name})")
            return Suggestion((msg + " רוצה לבוא לנסיעת מבחן?").strip(), "answer")

        if any(w in t for w in self.MEETING) or re.search(r"\b(כן|בשמחה|אשמח|יאללה|בטח)\b", t):
            if not slots:
                return Suggestion(check, "handoff", "הלקוח רוצה פגישה אבל אין מועדים פנויים")
            opts = ", ".join(s.label() for s in slots)
            return Suggestion(f"מעולה {name}! יש לי {opts}. מה מתאים לך?", "meeting", slots=slots)

        faqs = self.kn.match_faq(incoming)
        if faqs:
            if all(f.answer for f in faqs):
                return Suggestion(" ".join(f.answer for f in faqs), "answer")
            missing = ", ".join(f.topic for f in faqs if not f.answer)
            return Suggestion(check, "handoff", f"אין תשובה מאושרת לנושא: {missing}")

        if any(w in t for w in self.TELL_ME):
            if model and model.highlights:
                return Suggestion(f"בשמחה! ה-{model.name}: " + "; ".join(model.highlights[:3]) +
                                  ". הכי טוב להרגיש אותו בנסיעת מבחן — מתי נוח לך?", "answer")
            return Suggestion(f"בשמחה {name}! הכי טוב להכיר את ה-{mname} בנסיעת מבחן — רוצה שאציע כמה מועדים?", "answer")

        if any(w in t for w in self.GREETING):
            return Suggestion(f"היי {name}! 🙂 שמח שחזרת אליי. רוצה לשמוע עוד על ה-{mname}, או שנקבע נסיעת מבחן?", "answer")

        if any(w in t for w in self.THANKS):
            return Suggestion(f"בכיף {name}! אם יש עוד שאלה או שבא לך לקבוע נסיעת מבחן — אני כאן.", "answer")

        # didn't understand — ask, don't escalate
        return Suggestion(
            f"לא בטוח שהבנתי {name} 🙂 אפשר לשאול אותי על המחיר, הצבעים או המפרט של ה-{mname}, או שנקבע נסיעת מבחן.",
            "answer",
        )


ORDINALS = {"הראשון": 0, "הראשונה": 0, "השני": 1, "השנייה": 1, "השניה": 1, "השלישי": 2, "השלישית": 2}


def match_slot_choice(text: str, slots: list[Slot]) -> Slot | None:
    """Rule-based: did the customer pick exactly one of the offered slots?"""
    if not slots:
        return None
    t = text.strip()
    cands = list(slots)

    hit = []
    # dates first ("26/10"), then strip them so their numbers aren't read as hours
    date_re = r"(?<!\d)(\d{1,2})[./](\d{1,2})(?!\d)"
    for d, mo in re.findall(date_re, t):
        hit += [s for s in cands if s.start.day == int(d) and s.start.month == int(mo)]
    rest = re.sub(date_re, " ", t)
    # times: "10:00", "ב-10", "ב10" (digits only — "בעשר" is too fuzzy)
    for h, m in re.findall(r"(?<!\d)(\d{1,2})(?::(\d{2}))?(?!\d)", rest):
        h, m = int(h), int(m or 0)
        if h <= 23:
            hit += [s for s in cands if s.start.hour == h and s.start.minute == m]
    for i, day in enumerate(HEB_DAYS):
        if re.search(rf"(?:יום\s+|ב){day}\b", t):
            hit += [s for s in cands if s.start.weekday() == i]
    for word, idx in ORDINALS.items():
        if re.search(rf"\b{word}\b", t) and "יום" not in t and idx < len(cands):
            hit.append(cands[idx])
    uniq = {s.key(): s for s in hit}
    return next(iter(uniq.values())) if len(uniq) == 1 else None


def _field_sentence(f: str, model: Model, v) -> str:
    v = ", ".join(v) if isinstance(v, list) else v
    return {
        "price": f"המחיר של ה-{model.name} הוא {v}.",
        "colors": f"ה-{model.name} מגיע בצבעים: {v}.",
        "availability": f"לגבי זמינות: {v}.",
        "promotions": f"כרגע יש: {v}.",
        "license": f"ה-{model.name} מתאים לרישיון {v}.",
        "warranty": f"האחריות: {v}.",
        "service": f"טיפולים: {v}.",
    }[f]


def slots_to_list(slots: list[Slot]) -> list[dict]:
    return [{"start": x.start.isoformat(), "end": x.end.isoformat(), "label": x.label()} for x in slots]


def suggestion_to_json(s: Suggestion) -> str:
    return json.dumps(
        {"message": s.message, "intent": s.intent, "handoff_reason": s.handoff_reason, "used_ai": s.used_ai,
         "slots": slots_to_list(s.slots)},
        ensure_ascii=False,
    )
