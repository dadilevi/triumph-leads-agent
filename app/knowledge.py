"""Approved knowledge: models + business details + FAQ. The agent answers only from here."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # Python weekday() order


def _norm(s: str) -> str:
    return re.sub(r"[\s\-_\"'׳״]+", " ", str(s).lower()).strip()


@dataclass
class Model:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    category: str | None = None
    license: str | None = None
    price: str | None = None
    colors: list[str] = field(default_factory=list)
    availability: str | None = None
    promotions: str | None = None
    includes: str | None = None
    highlights: list[str] = field(default_factory=list)

    def as_prompt_text(self) -> str:
        def v(x):
            if x is None or x == [] or x == "":
                return "לא ידוע — להעביר לנציג"
            return ", ".join(x) if isinstance(x, list) else str(x)

        return "\n".join(
            [
                f"דגם: {self.name}",
                f"  קטגוריה: {v(self.category)}",
                f"  רישיון נדרש: {v(self.license)}",
                f"  מחיר: {v(self.price)}",
                f"  צבעים: {v(self.colors)}",
                f"  זמינות: {v(self.availability)}",
                f"  מבצעים: {v(self.promotions)}",
                f"  כולל: {v(self.includes)}",
                f"  נקודות חוזק: {v(self.highlights)}",
            ]
        )


@dataclass
class FAQ:
    topic: str
    keywords: list[str]
    answer: str | None


@dataclass
class Knowledge:
    models: list[Model]
    business: dict
    meetings: dict
    opening_template: str
    faq: list[FAQ]
    handoff_keywords: list[str]
    is_dummy: bool = False

    @classmethod
    def load(cls, models_file: Path, business_file: Path) -> "Knowledge":
        m = yaml.safe_load(Path(models_file).read_text(encoding="utf-8")) or {}
        b = yaml.safe_load(Path(business_file).read_text(encoding="utf-8")) or {}
        models = [Model(**{k: v for k, v in x.items() if k in Model.__dataclass_fields__}) for x in m.get("models", [])]
        faq = [FAQ(topic=f["topic"], keywords=f.get("keywords", []), answer=f.get("answer")) for f in b.get("faq", [])]
        return cls(
            models=models,
            business=b.get("business", {}),
            meetings=b.get("meetings", {}),
            opening_template=b.get("opening_template", "היי {first_name}, ראיתי שהתעניינת ב-{model}."),
            faq=faq,
            handoff_keywords=b.get("handoff_keywords", []),
            is_dummy=bool(m.get("_dummy") or b.get("_dummy")),
        )

    # ---- lookups -------------------------------------------------------
    def find_model(self, text: str | None) -> Model | None:
        """Best-effort match of a free-text model name (from Excel or a message)."""
        if not text:
            return None
        t = _norm(text)
        best, best_len = None, 0
        for model in self.models:
            for cand in [model.name, model.id, *model.aliases]:
                c = _norm(cand)
                if c and c in t and len(c) > best_len:
                    best, best_len = model, len(c)
        return best

    def match_faq(self, text: str) -> list[FAQ]:
        t = _norm(text)
        return [f for f in self.faq if any(_norm(k) in t for k in f.keywords)]

    def wants_handoff(self, text: str) -> bool:
        t = _norm(text)
        return any(_norm(k) in t for k in self.handoff_keywords)

    def prompt_context(self, focus: Model | None = None) -> str:
        """Everything the LLM is allowed to know, as plain text."""
        parts = ["## פרטי העסק"]
        for k, v in self.business.items():
            parts.append(f"- {k}: {v}")
        parts.append(f"- משך פגישה: {self.meetings.get('duration_minutes', 45)} דקות")
        hours = self.meetings.get("hours", {})
        if hours:
            heb = {"sun": "א", "mon": "ב", "tue": "ג", "wed": "ד", "thu": "ה", "fri": "ו", "sat": "ש"}
            parts.append("- שעות פעילות: " + ", ".join(f"{heb[d]} {h[0]}–{h[1]}" for d, h in hours.items()))
        parts.append("\n## הדגם שהלקוח התעניין בו")
        parts.append(focus.as_prompt_text() if focus else "לא זוהה דגם.")
        others = [m for m in self.models if m is not focus]
        if others:
            parts.append("\n## דגמים נוספים")
            parts.extend(m.as_prompt_text() for m in others)
        parts.append("\n## שאלות נפוצות (תשובות מאושרות)")
        for f in self.faq:
            parts.append(f"- {f.topic}: {f.answer or 'לא ידוע — להעביר לנציג'}")
        return "\n".join(parts)
