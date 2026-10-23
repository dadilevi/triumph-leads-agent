"""Triumph Leads Agent — phase 1 "co-pilot" local web app."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from .agent import Agent, Suggestion, anthropic_llm, suggestion_to_json
from .calendar_service import Slot, make_calendar, propose_slots
from .config import Config
from .db import DB, STATUSES
from .excel_import import parse_leads_excel
from .knowledge import Knowledge
from .phone import display_phone, wa_link

INTENT_LABELS = {"answer": "תשובה", "meeting": "תיאום פגישה", "handoff": "להעביר לנציג", "not_interested": "לא מעוניין"}


def create_app(cfg: Config | None = None, llm=None, calendar=None) -> Flask:
    cfg = cfg or Config.from_env()
    app = Flask(__name__)
    app.secret_key = "local-only"  # app runs on localhost only
    kn = Knowledge.load(cfg.models_file, cfg.business_file)
    tz = ZoneInfo(kn.business.get("timezone", "Asia/Jerusalem"))
    db = DB(cfg.db_path)
    if llm is None and cfg.ai_enabled:
        llm = anthropic_llm(cfg.anthropic_api_key, cfg.anthropic_model)
    agent = Agent(kn, llm)
    _cal = {"obj": calendar}

    def cal():
        if _cal["obj"] is None:
            _cal["obj"] = make_calendar(cfg, tz)
        return _cal["obj"]

    app.extensions["tla"] = {"db": db, "kn": kn, "agent": agent, "cfg": cfg}

    @app.context_processor
    def inject():
        return {
            "STATUSES": STATUSES,
            "INTENT_LABELS": INTENT_LABELS,
            "ai_enabled": agent.llm is not None,
            "is_dummy": kn.is_dummy,
            "calendar_name": "Google Calendar" if cfg.calendar_provider == "google" else "יומן דמה (מקומי)",
            "display_phone": display_phone,
        }

    def lead_or_404(lead_id):
        lead = db.get_lead(lead_id)
        if not lead:
            abort(404)
        return lead

    def model_for(lead):
        return next((m for m in kn.models if m.id == lead["model_id"]), None) or kn.find_model(lead["model_raw"])

    def save_suggestion(lead_id: int, s: Suggestion) -> None:
        js = suggestion_to_json(s)
        db.set_suggestion(lead_id, js)
        if s.slots:
            db.set_offered_slots(lead_id, json.dumps(json.loads(js)["slots"], ensure_ascii=False))

    def load_suggestion(lead) -> dict | None:
        return json.loads(lead["pending_suggestion"]) if lead["pending_suggestion"] else None

    # ---- list / import -------------------------------------------------
    @app.get("/")
    def index():
        status = request.args.get("status") or None
        leads = db.list_leads(status)
        models = {m.id: m.name for m in kn.models}
        return render_template("index.html", leads=leads, counts=db.status_counts(), current=status, models=models)

    @app.post("/import")
    def import_excel():
        f = request.files.get("file")
        if not f or not f.filename:
            flash("לא נבחר קובץ.", "error")
            return redirect(url_for("index"))
        try:
            res = parse_leads_excel(f.stream)
        except Exception as e:
            flash(f"לא הצלחתי לקרוא את הקובץ: {e}", "error")
            return redirect(url_for("index"))
        if res.missing_fields:
            flash(f"עמודות שלא זוהו: {', '.join(res.missing_fields)}. עמודות שזוהו: {res.column_map}", "error")
        added = dup = 0
        for pl in res.leads:
            m = kn.find_model(pl.model_raw)
            _, created = db.upsert_lead(
                first_name=pl.first_name, last_name=pl.last_name, email=pl.email, phone=pl.phone,
                phone_raw=pl.phone_raw, model_raw=pl.model_raw, model_id=m.id if m else None,
            )
            added += created
            dup += not created
        flash(f"נוספו {added} לידים חדשים" + (f", {dup} כבר היו ברשימה" if dup else "") + ".", "ok")
        for w in res.warnings[:10]:
            flash(w, "warn")
        return redirect(url_for("index"))

    # ---- lead page -----------------------------------------------------
    @app.get("/lead/<int:lead_id>")
    def lead_page(lead_id):
        lead = lead_or_404(lead_id)
        msgs = db.messages(lead_id)
        sug = load_suggestion(lead)
        if sug is None and not msgs and lead["status"] == "new":
            s = agent.opening(lead["first_name"], model_for(lead), lead["model_raw"])
            save_suggestion(lead_id, s)
            sug = json.loads(suggestion_to_json(s))
        return render_template(
            "lead.html", lead=lead, msgs=msgs, sug=sug, model=model_for(lead),
            offered=json.loads(lead["offered_slots"]) if lead["offered_slots"] else [],
            meetings=db.meetings(lead_id), wa_base=f"https://wa.me/{lead['phone']}" if lead["phone"] else None,
        )

    @app.post("/lead/<int:lead_id>/sent")
    def mark_sent(lead_id):
        lead = lead_or_404(lead_id)
        text = (request.form.get("text") or "").strip()
        if text:
            db.add_message(lead_id, "out", text)
        db.set_suggestion(lead_id, None)
        if lead["status"] in ("new", "replied"):
            db.set_status(lead_id, "sent")
        return redirect(url_for("lead_page", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/incoming")
    def incoming(lead_id):
        lead = lead_or_404(lead_id)
        text = (request.form.get("text") or "").strip()
        if not text:
            return redirect(url_for("lead_page", lead_id=lead_id))
        db.add_message(lead_id, "in", text)
        _suggest(lead_id, lead)
        return redirect(url_for("lead_page", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/regenerate")
    def regenerate(lead_id):
        lead = lead_or_404(lead_id)
        if any(m["direction"] == "in" for m in db.messages(lead_id)):
            _suggest(lead_id, lead)
        else:
            db.set_suggestion(lead_id, None)
        return redirect(url_for("lead_page", lead_id=lead_id))

    def _suggest(lead_id, lead):
        history = [(m["direction"], m["text"]) for m in db.messages(lead_id)]
        try:
            slots = propose_slots(cal(), kn.meetings, tz)
        except Exception as e:
            flash(f"לא הצלחתי לקרוא את היומן: {e}", "error")
            slots = []
        s = agent.suggest_reply(lead["first_name"], model_for(lead), history, slots)
        save_suggestion(lead_id, s)
        if s.intent == "handoff":
            db.set_status(lead_id, "handoff")
        elif s.intent == "not_interested":
            db.set_status(lead_id, "not_relevant")
        elif lead["status"] not in ("meeting",):
            db.set_status(lead_id, "replied")

    @app.post("/lead/<int:lead_id>/slots")
    def offer_slots(lead_id):
        """Manually ask for free slots and draft a message offering them."""
        lead = lead_or_404(lead_id)
        try:
            slots = propose_slots(cal(), kn.meetings, tz)
        except Exception as e:
            flash(f"לא הצלחתי לקרוא את היומן: {e}", "error")
            return redirect(url_for("lead_page", lead_id=lead_id))
        if not slots:
            flash("אין מועדים פנויים בטווח החיפוש.", "warn")
            return redirect(url_for("lead_page", lead_id=lead_id))
        opts = ", ".join(s.label() for s in slots)
        s = Suggestion(f"{lead['first_name']}, יש לי {opts}. מה מתאים לך?", "meeting", slots=slots)
        save_suggestion(lead_id, s)
        return redirect(url_for("lead_page", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/book")
    def book(lead_id):
        lead = lead_or_404(lead_id)
        start = datetime.fromisoformat(request.form["start"])
        end = datetime.fromisoformat(request.form["end"])
        m = model_for(lead)
        model_name = m.name if m else lead["model_raw"]
        full = f"{lead['first_name']} {lead['last_name']}".strip()
        title = kn.meetings.get("event_title", "פגישה — {full_name} ({model})").format(full_name=full, model=model_name)
        desc = f"טלפון: {display_phone(lead['phone']) or lead['phone_raw']}\nמייל: {lead['email']}\nדגם: {model_name}\n" \
               f"נוצר ע\"י Triumph Leads Agent"
        try:
            eid = cal().create_event(title, start, end, desc)
        except Exception as e:
            flash(f"יצירת האירוע ביומן נכשלה: {e}", "error")
            return redirect(url_for("lead_page", lead_id=lead_id))
        db.add_meeting(lead_id, start.isoformat(), end.isoformat(), eid)
        db.set_status(lead_id, "meeting")
        db.set_offered_slots(lead_id, None)
        slot = Slot(start, end)
        addr = kn.business.get("address", "")
        confirm = f"סגרנו {lead['first_name']}! נתראה ב{slot.label()}" + (f", ב{addr}" if addr else "") + \
                  ". תביא רישיון בתוקף 🏍️"
        db.set_suggestion(lead_id, suggestion_to_json(Suggestion(confirm, "answer")))
        flash(f"הפגישה נוספה ל{('Google Calendar' if cfg.calendar_provider == 'google' else 'יומן הדמה')}.", "ok")
        return redirect(url_for("lead_page", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/status")
    def change_status(lead_id):
        lead_or_404(lead_id)
        db.set_status(lead_id, request.form["status"])
        return redirect(request.referrer or url_for("lead_page", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/notes")
    def notes(lead_id):
        lead_or_404(lead_id)
        db.set_notes(lead_id, request.form.get("notes", ""))
        return redirect(url_for("lead_page", lead_id=lead_id))

    app.jinja_env.globals["wa_link"] = wa_link
    return app
