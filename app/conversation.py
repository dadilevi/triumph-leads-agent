"""ConversationEngine — runs a lead conversation end to end.

Inbound message → store → (human owns it? stop) → agent decides → act on intent
(book the meeting / hand off / close) → deliver: send now (auto) or leave a draft (supervised).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .agent import Agent, Suggestion, slots_to_list, suggestion_to_json
from .calendar_service import Slot, propose_slots
from .channels import Channel, SendError
from .db import DB
from .knowledge import Knowledge, Model
from .phone import display_phone, normalize_il_phone

WINDOW = timedelta(hours=24)  # WhatsApp customer-service window for free-form messages


class ConversationEngine:
    def __init__(self, db: DB, kn: Knowledge, agent: Agent, channel: Channel, calendar_getter, tz: ZoneInfo,
                 mode: str = "supervised", opening_template: str = "", template_lang: str = "he",
                 notify_phone: str = "", debounce_seconds: float = 0.0):
        self.db, self.kn, self.agent, self.channel = db, kn, agent, channel
        self.cal = calendar_getter
        self.tz = tz
        self.mode = mode if channel.can_send else "supervised"
        self.opening_template = opening_template
        self.template_lang = template_lang
        self.notify_phone = notify_phone
        self.debounce = debounce_seconds
        self._lead_locks: dict[int, threading.Lock] = {}
        self._timers: dict[int, threading.Timer] = {}
        self._guard = threading.Lock()

    @property
    def auto(self) -> bool:
        return self.mode == "auto" and self.channel.can_send

    # ---- helpers -------------------------------------------------------
    def _lock(self, lead_id: int) -> threading.Lock:
        with self._guard:
            return self._lead_locks.setdefault(lead_id, threading.Lock())

    def model_for(self, lead) -> Model | None:
        return next((m for m in self.kn.models if m.id == lead["model_id"]), None) or self.kn.find_model(lead["model_raw"])

    def _alert(self, lead, kind: str, text: str) -> None:
        self.db.add_alert(lead["id"] if lead else None, kind, text)
        if self.notify_phone and self.channel.can_send and kind in ("meeting", "handoff", "error"):
            who = f"{lead['first_name']} {lead['last_name']}".strip() if lead else ""
            try:  # best effort — on Cloud API this only works inside the salesperson's 24h window
                self.channel.send_text(self.notify_phone, f"🔔 {who}: {text}")
            except Exception:
                pass

    def _slots_for_turn(self, lead) -> list[Slot]:
        """Slots the customer may pick from: the ones already offered (if still free), else fresh ones."""
        try:
            fresh = propose_slots(self.cal(), self.kn.meetings, self.tz)
        except Exception as e:
            self._alert(lead, "error", f"לא הצלחתי לקרוא את היומן: {e}")
            return []
        if lead["offered_slots"]:
            offered = [Slot(datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
                       for s in json.loads(lead["offered_slots"])]
            now = datetime.now(self.tz)
            try:
                busy = self.cal().busy(now, now + timedelta(days=30))
            except Exception:
                busy = []
            still_free = [s for s in offered if s.start > now and not any(s.start < b1 and b0 < s.end for b0, b1 in busy)]
            if still_free:
                return still_free
        return fresh

    def _in_window(self, lead) -> bool:
        if not lead["last_inbound_at"]:
            return False
        return datetime.now() - datetime.fromisoformat(lead["last_inbound_at"]) < WINDOW

    # ---- sending -------------------------------------------------------
    def send(self, lead_id: int, text: str, author: str = "agent") -> bool:
        """Send a free-form message now via the channel and record it. Returns False on failure."""
        lead = self.db.get_lead(lead_id)
        if not self.channel.can_send:  # wa.me: the human already sent it from their phone
            self.db.add_message(lead_id, "out", text, author=author)
            return True
        if self.channel.needs_template_for_first_message and not self._in_window(lead):
            self.db.add_message(lead_id, "out", text, author=author, status="failed")
            self._alert(lead, "error", "חלון 24 השעות של וואטסאפ סגור — אפשר לשלוח רק תבנית מאושרת. צריך נציג.")
            return False
        try:
            res = self.channel.send_text(lead["phone"], text)
        except SendError as e:
            self.db.add_message(lead_id, "out", text, author=author, status="failed")
            self._alert(lead, "error", f"שליחה נכשלה: {e}")
            return False
        self.db.add_message(lead_id, "out", text, author=author, wa_id=res.wa_id)
        return True

    # ---- opening -------------------------------------------------------
    def start(self, lead_id: int) -> Suggestion:
        """Opening message for a new lead. Auto mode sends it; otherwise it becomes a draft."""
        lead = self.db.get_lead(lead_id)
        model = self.model_for(lead)
        s = self.agent.opening(lead["first_name"], model, lead["model_raw"])
        if not self.auto:
            self.db.set_suggestion(lead_id, suggestion_to_json(s))
            return s
        self.send_opening(lead_id, s.message)
        return s

    def send_opening(self, lead_id: int, text: str) -> bool:
        lead = self.db.get_lead(lead_id)
        if not lead["phone"] and self.channel.name != "simulator":
            self._alert(lead, "error", "אין מספר טלפון תקין — אי אפשר לפתוח שיחה.")
            return False
        if self.channel.needs_template_for_first_message:
            if not self.opening_template:
                self._alert(lead, "error", "חסר WHATSAPP_OPENING_TEMPLATE — אי אפשר לפתוח שיחה ב-Cloud API.")
                return False
            model = self.model_for(lead)
            params = [lead["first_name"] or "", model.name if model else (lead["model_raw"] or "")]
            try:
                res = self.channel.send_template(lead["phone"], self.opening_template, self.template_lang, params)
            except SendError as e:
                self._alert(lead, "error", f"שליחת תבנית הפתיחה נכשלה: {e}")
                return False
            shown = self.agent.template_opening(params[0], params[1])
            self.db.add_message(lead_id, "out", shown, author="agent", wa_id=res.wa_id)
        else:
            if not self.send(lead_id, text):
                return False
        self.db.set_suggestion(lead_id, None)
        if lead["status"] == "new":
            self.db.set_status(lead_id, "sent")
        return True

    # ---- inbound -------------------------------------------------------
    def find_or_create_lead(self, phone: str, profile_name: str = "", source: str = "inbound") -> int:
        lead = self.db.find_lead_by_phone(phone)
        if lead:
            return lead["id"]
        parts = (profile_name or "").split()
        lead_id, _ = self.db.upsert_lead(
            first_name=parts[0] if parts else "", last_name=" ".join(parts[1:]), email="", phone=phone,
            phone_raw=phone, model_raw="", model_id=None, source=source,
        )
        self.db.add_alert(lead_id, "new_lead", f"פנייה חדשה ישירות בוואטסאפ מ-{display_phone(phone)}")
        return lead_id

    def handle_inbound(self, phone: str, text: str, wa_id: str | None = None, profile_name: str = "",
                       source: str = "inbound") -> int | None:
        """A customer wrote to us. Returns lead id, or None for a duplicate delivery."""
        phone = normalize_il_phone(phone) or phone
        lead_id = self.find_or_create_lead(phone, profile_name, source)
        if self.db.add_message(lead_id, "in", text, author="customer", wa_id=wa_id) is None:
            return None  # webhook retry — already handled
        self.db.touch_inbound(lead_id)
        self._schedule(lead_id)
        return lead_id

    def handle_inbound_for_lead(self, lead_id: int, text: str) -> None:
        """Customer message pasted by hand (wa.me mode) or typed in the simulator for a lead without a phone."""
        self.db.add_message(lead_id, "in", text, author="customer")
        self.db.touch_inbound(lead_id)
        self._schedule(lead_id)

    def _schedule(self, lead_id: int) -> None:
        """Customers often send several short messages in a row — wait a moment and answer them together."""
        if self.debounce <= 0:
            self.process_turn(lead_id)
            return
        with self._guard:
            old = self._timers.pop(lead_id, None)
            if old:
                old.cancel()
            t = threading.Timer(self.debounce, self._run_turn_safely, args=(lead_id,))
            t.daemon = True
            self._timers[lead_id] = t
            t.start()

    def _run_turn_safely(self, lead_id: int) -> None:
        try:
            self.process_turn(lead_id)
        except Exception as e:  # never crash the webhook thread silently
            self.db.add_alert(lead_id, "error", f"שגיאה בטיפול בהודעה: {e}")

    def handle_echo(self, phone: str, text: str, wa_id: str | None = None) -> int | None:
        """Coexistence: someone replied from the WhatsApp Business app on the phone → a human took over."""
        phone = normalize_il_phone(phone) or phone
        lead = self.db.find_lead_by_phone(phone)
        if not lead:
            return None
        if self.db.add_message(lead["id"], "out", text, author="human", wa_id=wa_id) is None:
            return None
        if not lead["agent_paused"]:
            self.db.set_paused(lead["id"], True, "נציג ענה מהטלפון")
            self._alert(lead, "human_took_over", "נציג ענה מהאפליקציה — הסוכן הפסיק לענות בשיחה הזו.")
        return lead["id"]

    def resume(self, lead_id: int) -> None:
        self.db.set_paused(lead_id, False)

    def pause(self, lead_id: int, reason: str = "הושהה ידנית") -> None:
        self.db.set_paused(lead_id, True, reason)

    # ---- the turn ------------------------------------------------------
    def process_turn(self, lead_id: int, force_draft: bool = False) -> Suggestion | None:
        """force_draft: a human asked for a (re)draft — ignore the pause and never auto-send."""
        with self._lock(lead_id):
            lead = self.db.get_lead(lead_id)
            auto = self.auto and not force_draft
            if lead["agent_paused"] and not force_draft:
                self._alert(lead, "handoff", "הלקוח כתב שוב — השיחה אצל נציג, הסוכן לא ענה.")
                return None
            if lead["status"] == "not_relevant":
                return None
            history = [(m["direction"], m["text"]) for m in self.db.messages(lead_id) if m["status"] == "sent"]
            slots = self._slots_for_turn(lead)
            s = self.agent.reply(lead["first_name"], self.model_for(lead), history, slots)
            incoming = next((t for d, t in reversed(history) if d == "in"), "")
            topics = [f.topic for f in self.kn.match_faq(incoming) if f.notify]
            if topics and not s.notify:
                s.notify = "הלקוח שאל על " + ", ".join(topics)

            if s.intent == "book" and auto:
                s = self._book(lead, s.chosen)
            elif s.intent == "book":  # supervised: never touch the calendar without a human click
                s = Suggestion(self.confirmation_text(lead, s.chosen), "book", used_ai=s.used_ai, slots=[s.chosen],
                               handoff_reason="הלקוח בחר מועד — לחץ על המועד לקביעה ביומן, ואז שלח את האישור.")

            if s.slots:
                self.db.set_offered_slots(lead_id, json.dumps(slots_to_list(s.slots), ensure_ascii=False))

            if s.notify:  # FYI for the salesperson; the conversation continues
                self._alert(lead, "info", s.notify)

            if s.intent == "handoff" and s.pause:  # the whole conversation goes to a human
                self.db.set_status(lead_id, "handoff")
                self.db.set_paused(lead_id, True, s.handoff_reason or "הסוכן העביר לנציג")
                self._alert(lead, "handoff", s.handoff_reason or "הסוכן העביר לנציג")
            elif s.intent == "handoff":  # one open question for the team — the agent keeps talking
                self._alert(lead, "question", s.handoff_reason or "שאלה פתוחה לצוות")
                if lead["status"] not in ("meeting",):
                    self.db.set_status(lead_id, "sent" if auto else "replied")
            elif s.intent == "not_interested":
                self.db.set_status(lead_id, "not_relevant")
            elif not (s.intent == "book" and auto) and lead["status"] not in ("meeting",):
                self.db.set_status(lead_id, "sent" if auto else "replied")

            if auto:
                self.send(lead_id, s.message)
                self.db.set_suggestion(lead_id, None)
            else:
                self.db.set_suggestion(lead_id, suggestion_to_json(s))
            return s

    def _book(self, lead, slot: Slot | None) -> Suggestion:
        name = lead["first_name"]
        if slot is None:
            return Suggestion(f"תודה {name}, מישהו מהצוות יחזור אליך לתאם 🙏", "handoff", "בחירת מועד לא זוהתה")
        try:
            eid = self.create_meeting(lead, slot)
        except Exception as e:
            return Suggestion(f"תודה {name}, מישהו מהצוות יחזור אליך לאשר את המועד 🙏", "handoff",
                              f"יצירת האירוע ביומן נכשלה: {e}")
        s = Suggestion(self.confirmation_text(lead, slot), "book", used_ai=False, chosen=slot)
        s.handoff_reason = f"נקבעה פגישה (אירוע {eid})"
        return s

    def confirmation_text(self, lead, slot: Slot) -> str:
        addr = self.kn.business.get("address", "")
        return f"סגרנו {lead['first_name']}! נתראה ב{slot.label()}" + (f", ב{addr}" if addr else "") + \
               ". תביא רישיון בתוקף 🏍️"

    def create_meeting(self, lead, slot: Slot) -> str:
        """Create the calendar event + bookkeeping. Used by the agent (book) and by the UI buttons."""
        model = self.model_for(lead)
        model_name = model.name if model else lead["model_raw"]
        full = f"{lead['first_name']} {lead['last_name']}".strip()
        title = self.kn.meetings.get("event_title", "פגישה — {full_name} ({model})").format(full_name=full, model=model_name)
        desc = (f"טלפון: {display_phone(lead['phone']) or lead['phone_raw']}\nמייל: {lead['email']}\n"
                f"דגם: {model_name}\nנקבע ע\"י Triumph Leads Agent")
        eid = self.cal().create_event(title, slot.start, slot.end, desc)
        self.db.add_meeting(lead["id"], slot.start.isoformat(), slot.end.isoformat(), eid)
        self.db.set_status(lead["id"], "meeting")
        self.db.set_offered_slots(lead["id"], None)
        self._alert(lead, "meeting", f"נקבעה פגישה: {slot.label()} ({model_name or 'דגם לא ידוע'})")
        return eid
