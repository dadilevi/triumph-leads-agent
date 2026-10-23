"""Autonomous conversation engine, channels and the WhatsApp webhook."""
import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app import create_app
from app.agent import Agent, match_slot_choice
from app.calendar_service import MockCalendar, Slot
from app.channels import CloudChannel, SimulatorChannel, parse_webhook, verify_signature
from app.config import ROOT, Config
from app.conversation import ConversationEngine
from app.db import DB
from app.knowledge import Knowledge

TZ = ZoneInfo("Asia/Jerusalem")
KN = Knowledge.load(ROOT / "knowledge/models.dummy.yaml", ROOT / "knowledge/business.dummy.yaml")


def slot(y, mo, d, h, mi=0):
    s = datetime(y, mo, d, h, mi, tzinfo=TZ)
    return Slot(s, s + timedelta(minutes=45))


SLOTS = [slot(2026, 10, 25, 10), slot(2026, 10, 26, 16), slot(2026, 10, 27, 12)]  # Sun, Mon, Tue


class TestSlotChoice(unittest.TestCase):
    def test_by_time(self):
        self.assertEqual(match_slot_choice("16:00 מעולה", SLOTS), SLOTS[1])
        self.assertEqual(match_slot_choice("ב-12 מתאים לי", SLOTS), SLOTS[2])
        self.assertEqual(match_slot_choice("ב10 סבבה", SLOTS), SLOTS[0])

    def test_by_day_and_date(self):
        self.assertEqual(match_slot_choice("יום שני מתאים", SLOTS), SLOTS[1])
        self.assertEqual(match_slot_choice("בשלישי", SLOTS), SLOTS[2])
        self.assertEqual(match_slot_choice("ב-25/10 אני יכול", SLOTS), SLOTS[0])

    def test_date_numbers_are_not_hours(self):
        # "26/10" must not be read as 10:00 (Sunday's slot)
        self.assertEqual(match_slot_choice("26/10", SLOTS), SLOTS[1])

    def test_ordinal(self):
        self.assertEqual(match_slot_choice("השני", SLOTS), SLOTS[1])

    def test_ambiguous_or_none(self):
        self.assertIsNone(match_slot_choice("בשני ב-10", SLOTS))  # conflicting
        self.assertIsNone(match_slot_choice("כמה זה עולה?", SLOTS))
        self.assertIsNone(match_slot_choice("16:00", []))


class Base(unittest.TestCase):
    mode = "auto"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.db = DB(d / "t.db")
        self.cal = MockCalendar(d / "cal.json", TZ)
        self.ch = SimulatorChannel()
        self.engine = ConversationEngine(self.db, KN, Agent(KN), self.ch, lambda: self.cal, TZ, mode=self.mode)
        self.lead_id, _ = self.db.upsert_lead(first_name="יוסי", last_name="כהן", email="", phone="972500000001",
                                              phone_raw="", model_raw="Trident 660", model_id="trident-660")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def say(self, text):
        self.engine.handle_inbound("0500000001", text)
        return self.db.get_lead(self.lead_id)

    def last_out(self):
        return [m for m in self.db.messages(self.lead_id) if m["direction"] == "out"][-1]["text"]


class TestAutoMode(Base):
    def test_full_conversation_books_meeting_by_itself(self):
        self.engine.start(self.lead_id)
        self.assertIn("יוסי", self.last_out())
        self.assertEqual(self.db.get_lead(self.lead_id)["status"], "sent")

        self.say("היי, כמה הוא עולה?")
        self.assertIn("₪00,000 (דמה)", self.last_out())

        lead = self.say("אשמח לבוא לנסיעת מבחן")
        offered = json.loads(lead["offered_slots"])
        self.assertEqual(len(offered), 3)
        self.assertIn(offered[0]["label"], self.last_out())

        pick = datetime.fromisoformat(offered[1]["start"])
        lead = self.say(f"{pick:%H:%M} מתאים לי")
        self.assertEqual(lead["status"], "meeting")
        self.assertIn("סגרנו", self.last_out())
        meetings = self.db.meetings(self.lead_id)
        self.assertEqual(len(meetings), 1)
        self.assertEqual(meetings[0]["start"], offered[1]["start"])
        self.assertEqual(len(self.cal.busy(pick, pick + timedelta(minutes=1))), 1)  # event really created
        self.assertTrue(any(a["kind"] == "meeting" for a in self.db.alerts()))
        self.assertEqual(len(self.ch.sent), 4)  # opening, price answer, slot offer, confirmation

    def test_unknown_price_is_soft_handoff_agent_keeps_talking(self):
        self.db.conn.execute("UPDATE leads SET model_id='street-triple-765', model_raw='Street Triple 765'")
        lead = self.say("כמה עולה?")
        self.assertNotEqual(lead["status"], "handoff")
        self.assertFalse(lead["agent_paused"])
        self.assertNotIn("₪", self.last_out())
        self.assertIn("בודק", self.last_out())
        self.assertTrue(any(a["kind"] == "question" for a in self.db.alerts()))
        n = len(self.ch.sent)
        self.say("בוקר טוב")
        self.assertEqual(len(self.ch.sent), n + 1)  # still talking

    def test_asking_for_human_pauses_and_agent_goes_quiet(self):
        lead = self.say("אפשר שנציג יתקשר אליי?")
        self.assertEqual(lead["status"], "handoff")
        self.assertTrue(lead["agent_paused"])
        n = len(self.ch.sent)
        self.say("הלו?")
        self.assertEqual(len(self.ch.sent), n)
        self.assertGreaterEqual(sum(a["kind"] == "handoff" for a in self.db.alerts()), 2)
        self.engine.resume(self.lead_id)
        self.say("אשמח לנסיעת מבחן")
        self.assertEqual(len(self.ch.sent), n + 1)

    def test_customer_opens_with_greetings_no_repeats(self):
        self.say("שלום")
        first = self.last_out()
        self.assertIn("העוזר הדיגיטלי", first)  # introduces itself, doesn't say "glad you're back"
        self.assertNotIn("שחזרת", first)
        self.say("בוקר טוב")
        self.assertNotEqual(self.last_out(), first)
        outs = [m["text"] for m in self.db.messages(self.lead_id) if m["direction"] == "out"]
        for _ in range(3):
            self.say("אממ")
            outs.append(self.last_out())
        self.assertNotEqual(outs[-1], outs[-2])

    def test_financing_answered_and_salesperson_notified(self):
        lead = self.say("יש מימון?")
        self.assertIn("פתרון שילך לקראתך", self.last_out())
        self.assertFalse(lead["agent_paused"])
        self.assertTrue(any(a["kind"] == "info" and "מימון" in a["text"] for a in self.db.alerts()))

    def test_greeting_and_small_talk_get_replies(self):
        for text in ["בוקר טוב", "ספר לי עליו", "תודה!", "אממ"]:
            lead = self.say(text)
            self.assertFalse(lead["agent_paused"], text)
        self.assertEqual(len(self.ch.sent), 4)

    def test_unknown_number_creates_lead(self):
        lid = self.engine.handle_inbound("972549999999", "היי, מעוניין בטייגר 900", profile_name="רון לוי")
        lead = self.db.get_lead(lid)
        self.assertEqual(lead["first_name"], "רון")
        self.assertEqual(lead["source"], "inbound")
        self.assertTrue(any(a["kind"] == "new_lead" for a in self.db.alerts()))

    def test_duplicate_webhook_delivery_ignored(self):
        self.engine.handle_inbound("972500000001", "כמה עולה?", wa_id="wamid.1")
        n = len(self.ch.sent)
        self.assertIsNone(self.engine.handle_inbound("972500000001", "כמה עולה?", wa_id="wamid.1"))
        self.assertEqual(len(self.ch.sent), n)

    def test_human_reply_from_phone_pauses_agent(self):
        self.engine.handle_echo("972500000001", "היי יוסי, זה דני, אני אמשיך מכאן", wa_id="wamid.echo")
        lead = self.db.get_lead(self.lead_id)
        self.assertTrue(lead["agent_paused"])
        self.assertEqual(self.db.messages(self.lead_id)[-1]["author"], "human")
        n = len(self.ch.sent)
        self.say("מעולה תודה")
        self.assertEqual(len(self.ch.sent), n)

    def test_bot_question_answered_honestly(self):
        self.say("רגע, אתה בוט?")
        self.assertIn("העוזר הדיגיטלי", self.last_out())

    def test_not_interested_closes(self):
        lead = self.say("לא מעוניין, תודה")
        self.assertEqual(lead["status"], "not_relevant")
        n = len(self.ch.sent)
        self.say("?")
        self.assertEqual(len(self.ch.sent), n)


class TestSupervisedMode(Base):
    mode = "supervised"

    def test_nothing_sent_without_approval_and_no_auto_booking(self):
        self.say("אשמח לבוא לנסיעת מבחן")
        self.assertEqual(self.ch.sent, [])
        lead = self.db.get_lead(self.lead_id)
        self.assertEqual(json.loads(lead["pending_suggestion"])["intent"], "meeting")
        offered = json.loads(lead["offered_slots"])
        pick = datetime.fromisoformat(offered[0]["start"])
        lead = self.say(f"{pick:%H:%M}")
        sug = json.loads(lead["pending_suggestion"])
        self.assertEqual(sug["intent"], "book")
        self.assertIn("סגרנו", sug["message"])
        self.assertEqual(self.db.meetings(self.lead_id), [])  # human must click the slot
        self.assertEqual(len(json.loads(lead["offered_slots"])), 1)


class TestAIBooking(Base):
    def test_ai_book_with_invalid_slot_hands_off(self):
        self.engine.agent = Agent(KN, llm=lambda *a: {"message": "סגור!", "intent": "book", "chosen_slot": 9})
        self.say("ביום ראשון")
        self.assertEqual(self.db.meetings(self.lead_id), [])
        self.assertTrue(any(a["kind"] == "question" for a in self.db.alerts()))

    def test_ai_book_valid(self):
        self.engine.agent = Agent(KN, llm=lambda *a: {"message": "סגור!", "intent": "book", "chosen_slot": 1})
        lead = self.say("הראשון")
        self.assertEqual(lead["status"], "meeting")
        self.assertIn("סגרנו", self.last_out())

    def test_prompt_has_rules_and_numbered_slots(self):
        seen = {}

        def llm(system, messages, tool):
            seen["system"], seen["user"] = system, messages[0]["content"]
            return {"message": "היי", "intent": "answer"}

        self.engine.agent = Agent(KN, llm=llm)
        self.say("היי")
        self.assertIn("לעולם אל תטען שאתה בן אדם", seen["system"])
        self.assertIn("1. יום", seen["user"])


class FakeHTTP:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        outer = self

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"messages": [{"id": f"wamid.out{len(outer.requests)}"}]}).encode()

        return R()


class TestCloudChannel(Base):
    def setUp(self):
        super().setUp()
        self.http = FakeHTTP()
        self.engine.channel = CloudChannel("123", "tok", opener=self.http)
        self.engine.opening_template = "lead_opening"

    def test_opening_uses_template(self):
        self.engine.start(self.lead_id)
        req = self.http.requests[0]
        self.assertEqual(req["type"], "template")
        self.assertEqual(req["template"]["name"], "lead_opening")
        self.assertEqual([p["text"] for p in req["template"]["components"][0]["parameters"]], ["יוסי", "Trident 660"])
        self.assertEqual(self.db.messages(self.lead_id)[0]["wa_id"], "wamid.out1")

    def test_free_text_only_inside_24h_window(self):
        self.assertFalse(self.engine.send(self.lead_id, "היי"))  # never wrote to us
        self.assertEqual(self.http.requests, [])
        self.say("כמה עולה?")  # opens the window
        self.assertEqual(self.http.requests[-1]["type"], "text")
        self.db.conn.execute("UPDATE leads SET last_inbound_at = ?", ((datetime.now() - timedelta(hours=25)).isoformat(),))
        self.db.conn.commit()
        self.assertFalse(self.engine.send(self.lead_id, "עדיין מעוניין?"))
        self.assertTrue(any("24" in a["text"] for a in self.db.alerts()))


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def wa_payload(field, items, key):
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": field, "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "123"},
        "contacts": [{"wa_id": "972500000001", "profile": {"name": "יוסי כהן"}}], key: items}}]}]}


class TestWebhook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.http = FakeHTTP()
        cfg = Config(db_path=d / "t.db", channel="cloud", agent_mode="auto", whatsapp_app_secret="s3cret",
                     whatsapp_verify_token="vt", whatsapp_phone_number_id="123", whatsapp_token="tok")
        self.app = create_app(cfg, llm=None, calendar=MockCalendar(d / "cal.json", TZ),
                              channel=CloudChannel("123", "tok", opener=self.http))
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()
        self.db = self.app.extensions["tla"]["db"]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def post(self, payload, secret="s3cret"):
        body = json.dumps(payload).encode()
        return self.c.post("/webhook/whatsapp", data=body, content_type="application/json",
                           headers={"X-Hub-Signature-256": sign(secret, body)})

    def test_verify_handshake(self):
        r = self.c.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42")
        self.assertEqual((r.status_code, r.get_data(as_text=True)), (200, "42"))
        self.assertEqual(self.c.get("/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=no&hub.challenge=1").status_code, 403)

    def test_rejects_bad_signature(self):
        r = self.post(wa_payload("messages", [], "messages"), secret="wrong")
        self.assertEqual(r.status_code, 403)

    def test_customer_message_gets_auto_reply(self):
        msg = {"from": "972500000001", "id": "wamid.in1", "type": "text", "text": {"body": "אפשר לבוא לנסות?"}}
        self.assertEqual(self.post(wa_payload("messages", [msg], "messages")).status_code, 200)
        lead = self.db.find_lead_by_phone("972500000001")
        self.assertEqual(lead["first_name"], "יוסי")
        self.assertEqual(self.http.requests[-1]["to"], "972500000001")
        self.assertIn("מעולה", self.http.requests[-1]["text"]["body"])
        self.post(wa_payload("messages", [msg], "messages"))  # Meta retry
        self.assertEqual(len(self.http.requests), 1)

    def test_echo_pauses(self):
        msg = {"from": "972500000001", "id": "wamid.in1", "type": "text", "text": {"body": "היי"}}
        self.post(wa_payload("messages", [msg], "messages"))
        echo = {"from": "972500000099", "to": "972500000001", "id": "wamid.e1", "type": "text", "text": {"body": "דני כאן"}}
        self.post(wa_payload("smb_message_echoes", [echo], "message_echoes"))
        self.assertTrue(self.db.find_lead_by_phone("972500000001")["agent_paused"])

    def test_parse_non_text(self):
        evs = parse_webhook(wa_payload("messages", [{"from": "9725", "id": "x", "type": "audio"}], "messages"))
        self.assertIn("הודעה קולית", evs[0].text)

    def test_signature_helper(self):
        self.assertFalse(verify_signature("", b"x", sign("", b"x")))  # no secret configured → reject


class TestRealKnowledge(unittest.TestCase):
    kn = Knowledge.load(ROOT / "knowledge/models.yaml", ROOT / "knowledge/business.dummy.yaml")

    def test_answers_from_official_site_data(self):
        agent = Agent(self.kn)
        m = self.kn.find_model("סקרמבלר 400")
        self.assertEqual(m.id, "scrambler-400-x")
        s = agent.reply("דנה", m, [("in", "כמה עולה ומה האחריות?")], [])
        self.assertIn("₪40,990", s.message)
        self.assertIn("שנתיים", s.message)
        s = agent.reply("דנה", self.kn.find_model("ספיד 400"), [("in", "איזה צבעים יש?")], [])
        self.assertEqual(s.intent, "handoff")  # colors unknown for Speed 400 — never invented
        self.assertFalse(s.pause)


class TestSimulatorPages(unittest.TestCase):
    def test_simulator_flow(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = Config(db_path=d / "t.db", channel="simulator", agent_mode="auto")
            app = create_app(cfg, llm=None, calendar=MockCalendar(d / "cal.json", TZ))
            app.config["TESTING"] = True
            c = app.test_client()
            r = c.post("/simulator/new", data={"name": "דנה בדיקה", "model": "Speed 400"})
            lid = int(r.headers["Location"].split("lead=")[1])
            c.post(f"/simulator/{lid}/start")
            c.post(f"/simulator/{lid}/customer", data={"text": "כמה עולה?"})
            page = c.get(f"/simulator?lead={lid}").get_data(as_text=True)
            self.assertIn("Speed 400", page)
            self.assertIn("₪36,990", page)
            self.assertIn("שלח כלקוח", page)
            self.assertEqual(c.get("/alerts").status_code, 200)
            app.extensions["tla"]["db"].conn.close()


class TestMigration(unittest.TestCase):
    def test_old_db_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.db"
            con = sqlite3.connect(p)
            con.executescript("""
                CREATE TABLE leads (id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, email TEXT, phone TEXT,
                  phone_raw TEXT, model_raw TEXT, model_id TEXT, status TEXT, notes TEXT, pending_suggestion TEXT,
                  offered_slots TEXT, created_at TEXT, updated_at TEXT);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, lead_id INTEGER, direction TEXT, text TEXT, created_at TEXT);
                INSERT INTO leads VALUES (1,'a','b','','9725','','','','new','',NULL,NULL,'x','x');
            """)
            con.close()
            db = DB(p)
            self.assertEqual(db.get_lead(1)["agent_paused"], 0)
            db.add_message(1, "in", "hi", wa_id="w1")
            self.assertIsNone(db.add_message(1, "in", "hi", wa_id="w1"))
            db.conn.close()


if __name__ == "__main__":
    unittest.main()
