"""Triumph Leads Agent — local web app: leads list, conversations, simulator, WhatsApp webhook."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from .agent import Agent, Suggestion, anthropic_llm, suggestion_to_json
from .calendar_service import Slot, make_calendar, propose_slots
from .channels import CloudChannel, SimulatorChannel, WameChannel, parse_webhook, verify_signature
from .config import Config
from .conversation import ConversationEngine
from .db import DB, STATUSES
from .excel_import import parse_leads_excel
from .knowledge import Knowledge
from .phone import display_phone, wa_link

INTENT_LABELS = {"answer": "תשובה", "meeting": "הצעת מועדים", "book": "קביעת פגישה",
                 "handoff": "העברה לנציג", "not_interested": "לא מעוניין"}
CHANNEL_LABELS = {"wame": "שליחה ידנית (wa.me)", "simulator": "סימולטור", "cloud": "WhatsApp Cloud API"}
AUTHOR_LABELS = {"customer": "לקוח", "agent": "סוכן", "human": "נציג"}


def make_channel(cfg: Config):
    if cfg.channel == "cloud":
        return CloudChannel(cfg.whatsapp_phone_number_id, cfg.whatsapp_token, cfg.whatsapp_api_version)
    if cfg.channel == "wame":
        return WameChannel()
    return SimulatorChannel()


def create_app(cfg: Config | None = None, llm=None, calendar=None, channel=None) -> Flask:
    cfg = cfg or Config.from_env()
    app = Flask(__name__)
    app.secret_key = "local-only"  # the UI is served on localhost only
    kn = Knowledge.load(cfg.models_file, cfg.business_file)
    tz = ZoneInfo(kn.business.get("timezone", "Asia/Jerusalem"))
    db = DB(cfg.db_path)
    if llm is None and cfg.ai_enabled:
        llm = anthropic_llm(cfg.anthropic_api_key, cfg.anthropic_model)
    agent = Agent(kn, llm)
    channel = channel or make_channel(cfg)
    _cal = {"obj": calendar}

    def cal():
        if _cal["obj"] is None:
            _cal["obj"] = make_calendar(cfg, tz)
        return _cal["obj"]

    engine = ConversationEngine(
        db, kn, agent, channel, cal, tz, mode=cfg.agent_mode,
        opening_template=cfg.whatsapp_opening_template, template_lang=cfg.whatsapp_template_lang,
        notify_phone=cfg.notify_phone, debounce_seconds=cfg.debounce_seconds,
    )
    app.extensions["tla"] = {"db": db, "kn": kn, "agent": agent, "cfg": cfg, "engine": engine, "channel": channel}

    @app.context_processor
    def inject():
        return {
            "STATUSES": STATUSES,
            "INTENT_LABELS": INTENT_LABELS,
            "AUTHOR_LABELS": AUTHOR_LABELS,
            "ai_enabled": agent.llm is not None,
            "is_dummy": kn.is_dummy,
            "calendar_name": "Google Calendar" if cfg.calendar_provider == "google" else "יומן דמה (מקומי)",
            "channel_name": channel.name,
            "channel_label": CHANNEL_LABELS.get(channel.name, channel.name),
            "auto_mode": engine.auto,
            "unseen_alerts": db.unseen_alerts_count(),
            "display_phone": display_phone,
        }

    def lead_or_404(lead_id):
        lead = db.get_lead(lead_id)
        if not lead:
            abort(404)
        return lead

    def back(lead_id):
        nxt = request.form.get("next")
        if nxt and nxt.startswith("/"):
            return redirect(nxt)
        return redirect(url_for("lead_page", lead_id=lead_id))

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

    @app.post("/start-new")
    def start_new():
        """Open conversations with all new leads (auto: sends; supervised: prepares drafts)."""
        n = 0
        for lead in db.list_leads("new"):
            if lead["phone"] and not db.messages(lead["id"]):
                engine.start(lead["id"])
                n += 1
        flash((f"נשלחו {n} הודעות פתיחה." if engine.auto else f"הוכנו {n} טיוטות פתיחה לאישור.") if n
              else "אין לידים חדשים עם מספר תקין.", "ok")
        return redirect(url_for("index"))

    # ---- lead page -----------------------------------------------------
    @app.get("/lead/<int:lead_id>")
    def lead_page(lead_id):
        lead = lead_or_404(lead_id)
        msgs = db.messages(lead_id)
        if not lead["pending_suggestion"] and not msgs and lead["status"] == "new":
            s = agent.opening(lead["first_name"], engine.model_for(lead), lead["model_raw"])
            db.set_suggestion(lead_id, suggestion_to_json(s))
            lead = db.get_lead(lead_id)
        sug = json.loads(lead["pending_suggestion"]) if lead["pending_suggestion"] else None
        return render_template(
            "lead.html", lead=lead, msgs=msgs, sug=sug, model=engine.model_for(lead),
            offered=json.loads(lead["offered_slots"]) if lead["offered_slots"] else [],
            meetings=db.meetings(lead_id), wa_base=f"https://wa.me/{lead['phone']}" if lead["phone"] else None,
        )

    @app.post("/lead/<int:lead_id>/send")
    def send(lead_id):
        """A human approved (and maybe edited) the draft."""
        lead = lead_or_404(lead_id)
        text = (request.form.get("text") or "").strip()
        if not text:
            return back(lead_id)
        is_opening = not any(m["direction"] == "out" for m in db.messages(lead_id))
        ok = engine.send_opening(lead_id, text) if is_opening else engine.send(lead_id, text)
        if ok:
            db.set_suggestion(lead_id, None)
            if lead["status"] in ("new", "replied"):
                db.set_status(lead_id, "sent")
        else:
            flash("השליחה נכשלה — ראה התראות.", "error")
        return back(lead_id)

    app.add_url_rule("/lead/<int:lead_id>/sent", "mark_sent", send, methods=["POST"])  # phase-1 name

    @app.post("/lead/<int:lead_id>/incoming")
    def incoming(lead_id):
        """Customer reply pasted by hand (wa.me mode)."""
        lead_or_404(lead_id)
        text = (request.form.get("text") or "").strip()
        if text:
            engine.handle_inbound_for_lead(lead_id, text)
        return back(lead_id)

    @app.post("/lead/<int:lead_id>/regenerate")
    def regenerate(lead_id):
        lead_or_404(lead_id)
        if any(m["direction"] == "in" for m in db.messages(lead_id)):
            engine.process_turn(lead_id, force_draft=True)
        else:
            db.set_suggestion(lead_id, None)
        return back(lead_id)

    @app.post("/lead/<int:lead_id>/slots")
    def offer_slots(lead_id):
        lead = lead_or_404(lead_id)
        try:
            slots = propose_slots(cal(), kn.meetings, tz)
        except Exception as e:
            flash(f"לא הצלחתי לקרוא את היומן: {e}", "error")
            return back(lead_id)
        if not slots:
            flash("אין מועדים פנויים בטווח החיפוש.", "warn")
            return back(lead_id)
        opts = ", ".join(s.label() for s in slots)
        s = Suggestion(f"{lead['first_name']}, יש לי {opts}. מה מתאים לך?", "meeting", slots=slots)
        js = suggestion_to_json(s)
        db.set_suggestion(lead_id, js)
        db.set_offered_slots(lead_id, json.dumps(json.loads(js)["slots"], ensure_ascii=False))
        return back(lead_id)

    @app.post("/lead/<int:lead_id>/book")
    def book(lead_id):
        lead = lead_or_404(lead_id)
        slot = Slot(datetime.fromisoformat(request.form["start"]), datetime.fromisoformat(request.form["end"]))
        try:
            engine.create_meeting(lead, slot)
        except Exception as e:
            flash(f"יצירת האירוע ביומן נכשלה: {e}", "error")
            return back(lead_id)
        db.set_suggestion(lead_id, suggestion_to_json(Suggestion(engine.confirmation_text(lead, slot), "answer")))
        flash(f"הפגישה נוספה ל{('Google Calendar' if cfg.calendar_provider == 'google' else 'יומן הדמה')}.", "ok")
        return back(lead_id)

    @app.post("/lead/<int:lead_id>/status")
    def change_status(lead_id):
        lead_or_404(lead_id)
        db.set_status(lead_id, request.form["status"])
        return redirect(request.referrer or url_for("lead_page", lead_id=lead_id))

    @app.post("/lead/<int:lead_id>/notes")
    def notes(lead_id):
        lead_or_404(lead_id)
        db.set_notes(lead_id, request.form.get("notes", ""))
        return back(lead_id)

    @app.post("/lead/<int:lead_id>/pause")
    def pause(lead_id):
        lead_or_404(lead_id)
        engine.pause(lead_id, "נציג לקח את השיחה")
        return back(lead_id)

    @app.post("/lead/<int:lead_id>/resume")
    def resume(lead_id):
        lead = lead_or_404(lead_id)
        engine.resume(lead_id)
        if lead["status"] == "handoff":
            db.set_status(lead_id, "replied")
        return back(lead_id)

    # ---- simulator -----------------------------------------------------
    @app.get("/simulator")
    def simulator():
        leads = db.list_leads()
        sel = request.args.get("lead", type=int) or (leads[0]["id"] if leads else None)
        lead = db.get_lead(sel) if sel else None
        msgs = db.messages(sel) if lead else []
        sug = json.loads(lead["pending_suggestion"]) if lead and lead["pending_suggestion"] else None
        offered = json.loads(lead["offered_slots"]) if lead and lead["offered_slots"] else []
        return render_template("simulator.html", leads=leads, lead=lead, msgs=msgs, sug=sug, offered=offered,
                               models=kn.models)

    @app.post("/simulator/new")
    def simulator_new():
        name = (request.form.get("name") or "לקוח בדיקה").strip()
        model_raw = request.form.get("model") or ""
        m = kn.find_model(model_raw)
        parts = name.split()
        lead_id, _ = db.upsert_lead(first_name=parts[0], last_name=" ".join(parts[1:]), email="", phone=None,
                                    phone_raw="", model_raw=model_raw, model_id=m.id if m else None,
                                    source="simulator")
        return redirect(url_for("simulator", lead=lead_id))

    @app.post("/simulator/<int:lead_id>/customer")
    def simulator_customer(lead_id):
        lead_or_404(lead_id)
        text = (request.form.get("text") or "").strip()
        if text:
            engine.handle_inbound_for_lead(lead_id, text)
        return redirect(url_for("simulator", lead=lead_id))

    @app.post("/simulator/<int:lead_id>/start")
    def simulator_start(lead_id):
        lead_or_404(lead_id)
        engine.start(lead_id)
        if not engine.auto:
            flash("טיוטת הפתיחה מחכה לאישור.", "ok")
        return redirect(url_for("simulator", lead=lead_id))

    @app.get("/simulator/<int:lead_id>/poll")
    def simulator_poll(lead_id):
        lead = lead_or_404(lead_id)
        return jsonify({"messages": len(db.messages(lead_id)), "draft": bool(lead["pending_suggestion"]),
                        "status": lead["status"], "paused": bool(lead["agent_paused"])})

    # ---- alerts --------------------------------------------------------
    @app.get("/alerts")
    def alerts():
        items = db.alerts()
        db.mark_alerts_seen()
        return render_template("alerts.html", items=items)

    # ---- WhatsApp Cloud API webhook --------------------------------------
    @app.get("/webhook/whatsapp")
    def webhook_verify():
        if (request.args.get("hub.mode") == "subscribe" and cfg.whatsapp_verify_token
                and request.args.get("hub.verify_token") == cfg.whatsapp_verify_token):
            return request.args.get("hub.challenge", ""), 200
        return "forbidden", 403

    @app.post("/webhook/whatsapp")
    def webhook_receive():
        body = request.get_data()
        if not verify_signature(cfg.whatsapp_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            return "bad signature", 403
        try:
            events = parse_webhook(json.loads(body or b"{}"))
        except ValueError:
            return "bad json", 400

        def work():  # answer Meta immediately; the agent may take a few seconds
            for ev in events:
                try:
                    if ev.kind == "customer":
                        engine.handle_inbound(ev.phone, ev.text, ev.wa_id, ev.profile_name)
                    elif ev.kind == "echo":
                        engine.handle_echo(ev.phone, ev.text, ev.wa_id)
                except Exception as e:
                    db.add_alert(None, "error", f"שגיאה בעיבוד הודעה נכנסת: {e}")

        if app.config.get("TESTING"):
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
        return "ok", 200

    app.jinja_env.globals["wa_link"] = wa_link
    return app
